"""End-to-end test for the GoHighLevel AI lead reactivation agent against MOCK APIs.

No real GoHighLevel account, Claude account or Slack workspace is used. The script:
  1. starts mock GoHighLevel, Anthropic and Slack APIs on this machine,
  2. imports and activates a temporary copy of workflow/error-alert-slack.json (n8n only runs an active error
     workflow), then a temporary copy of the reactivation workflow that reports to it (API hosts pointed at the
     mocks, fake keys),
  3. runs the campaign six times (refused calls, dry run, real runs, failures, re-runs, search outage) and plays
     the lead replies: interested, picking a time, questions, not now, STOP, wrong number, outages and repeat
     deliveries. To let an offered time pass without waiting, it edits the workflow's static data through the
     n8n API,
  4. checks every request the workflow made, the node each run ended on, and that every failed run raised
     exactly one error alert linked to it,
  5. imports a copy with broken settings (Run Now must answer 500 and name the problems) and a copy with the
     schedule switched on (it must text a new lead by itself),
  6. prints PASS or FAIL per check and deletes every temporary copy, also after a crash.
Temporary copies are named "TEST ghl-lead-reactivation-agent (temporary)". Copies with exactly that name left by an
interrupted run are deleted at the start. No other workflow is touched.

The mock GoHighLevel API keeps real state (contacts, tags, DND, conversations, calendar, opportunities) and
rejects requests the real v2 API would reject: a missing or wrong token, the wrong Version header for the
endpoint, unknown search fields or operators, page sizes over 500, slots that are not free, duplicate
opportunities, texts to contacts with SMS DND, and so on. The mock Claude returns scripted tool calls but
rejects malformed Messages API requests.

Requirements: Python 3.9+, a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
  SAVE_REQUESTS  optional file path; every request the workflow made to the mocks is saved there as JSON
                 (method, path, query, body and the mock's status code; headers are left out)
Usage: python tests/e2e_mock_test.py
"""
import copy, json, os, re, sys, threading, time, urllib.error, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8787
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
WORKFLOW = Path(__file__).resolve().parent.parent / 'workflow' / 'ghl-lead-reactivation-agent.json'
SLUG = 'ghl-lead-reactivation-agent'

# ----------------------------------------------------------------------------------------------------------------------
# Time zones. Test leads use fixed offset zones (Etc/GMT...), so the quiet hours checks do not depend on the clock.
# ----------------------------------------------------------------------------------------------------------------------
NOW = datetime.now(timezone.utc)


def etc_zone(local_hour):
    """An Etc/GMT zone in which it is local_hour o'clock right now. Note Etc/GMT-4 means UTC+4."""
    offset = ((local_hour - NOW.hour + 12) % 24) - 12
    return 'Etc/GMT' if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def tz_minutes(name):
    m = re.fullmatch(r'Etc/GMT(?:([+-])(\d{1,2}))?', name or '')
    if not m:
        return None
    return 0 if not m.group(1) else (-1 if m.group(1) == '+' else 1) * int(m.group(2)) * 60


IN_TZ = etc_zone(13)    # 1 PM there: inside the 9 AM to 7 PM sending window
OUT_TZ = etc_zone(3)    # 3 AM there: quiet hours
LOC, CAL, PIPE, STAGE_BOOKED = 'loc-mock-1', 'cal-mock-1', 'pipe-reactivation', 'stage-booked'
PIPELINES = {PIPE: {'stage-new', 'stage-contacted', STAGE_BOOKED, 'stage-lost'}}
GHL_KEY = 'pit-mock-key'

ENV = {
    'GHL_API_KEY': GHL_KEY, 'GHL_LOCATION_ID': LOC, 'GHL_CALENDAR_ID': CAL, 'GHL_PIPELINE_ID': PIPE, 'GHL_BOOKED_STAGE_ID': STAGE_BOOKED,
    'GHL_FROM_NUMBER': '', 'GHL_APP_URL': 'https://app.gohighlevel.com',
    'ANTHROPIC_API_KEY': 'mock-anthropic-key', 'ANTHROPIC_MODEL': 'claude-sonnet-5',
    'RUN_NOW_SECRET': 'test-run-secret', 'REPLY_WEBHOOK_SECRET': 'test-reply-secret',
    'BUSINESS_NAME': 'Bright Smile Dental', 'BUSINESS_ABOUT': 'a family dental practice',
    'BUSINESS_FAQ': 'Open Monday to Friday, 8 AM to 5 PM. A cleaning takes about 45 minutes. Free parking behind the building.',
    'REACTIVATION_OFFER': 'your next cleaning', 'REACTIVATION_MESSAGE': '', 'REACTIVATION_TAG': 'reactivate',
    'DORMANT_DAYS': '90', 'EXCLUDE_TAGS': 'customer-active', 'TAG_PREFIX': 'reactivation',
    'BATCH_LIMIT': '4', 'SEARCH_PAGE_SIZE': '3', 'MAX_SEARCH_PAGES': '5', 'SEND_DELAY_SECONDS': '1',
    'SEND_HOUR_START': '9', 'SEND_HOUR_END': '19', 'BUSINESS_TZ': IN_TZ, 'SCHEDULE_ENABLED': 'false',
    'APPOINTMENT_MINUTES': '30', 'SLOT_SEARCH_DAYS': '7', 'MAX_AI_REPLIES': '2', 'DND_ALL_CHANNELS': 'false',
    'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
}
HOSTS = {'https://services.leadconnectorhq.com': MOCK, 'https://api.anthropic.com': MOCK}
SENT, FAILED, REPLIED, INTERESTED, BOOKED = 'reactivation-sent', 'reactivation-send-failed', 'reactivation-replied', 'reactivation-interested', 'reactivation-booked'
NOT_NOW, WRONG, STOPPED, HUMAN = 'reactivation-not-now', 'reactivation-wrong-number', 'reactivation-opted-out', 'reactivation-needs-human'
CAMPAIGN_TEXT = ('Hi {first}, this is Bright Smile Dental. It has been a while! Would you like to book your next cleaning? '
                 'Reply YES and I will send you a few open times. Reply STOP to opt out.')
HOLD_TEXT = 'Thanks for getting back to us, {first}! A team member will text you shortly.'


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def ago(days):
    return iso(NOW - timedelta(days=days))


# ----------------------------------------------------------------------------------------------------------------------
# Mock state
# ----------------------------------------------------------------------------------------------------------------------
LOCK = threading.RLock()
CONTACTS, STALE_TAGS, CONVERSATIONS, MESSAGES = {}, {}, {}, {}
APPOINTMENTS, OPPORTUNITIES, OFFERED_SLOTS, TAKEN = [], {}, set(), set()
FAIL = {'tag': set(), 'sms': set(), 'sms_429_once': set(), 'flat_messages': set(), 'messages': set(), 'put_contact': set(),
        'search': False, 'slots': False, 'no_slots': False, 'opportunity_create': False}
LOG = []          # dict(service, method, path, query, headers, body, status, response, t)
COUNTER = {'n': 0}


def next_id(prefix):
    COUNTER['n'] += 1
    return f'{prefix}{COUNTER["n"]:04d}'


def add_contact(cid, first, last, phone, tz=None, tags=(), last_activity=None, dnd=False, dnd_settings=None, added=400):
    CONTACTS[cid] = {
        'id': cid, 'locationId': LOC, 'firstName': first, 'lastName': last, 'name': f'{first} {last}',
        'email': f'{first.lower()}.{last.lower()}@example.com', 'phone': phone, 'timezone': tz, 'tags': list(tags),
        'dnd': dnd, 'dndSettings': dnd_settings or {}, 'type': 'lead', 'source': 'web form',
        'dateAdded': ago(added), 'dateUpdated': ago(1), 'lastActivity': ago(last_activity) if last_activity is not None else None,
    }
    conv = 'conv-' + cid
    CONVERSATIONS[cid] = conv
    MESSAGES[conv] = []
    return cid


def add_message(cid, direction, body, minutes_ago=0.0):
    conv = CONVERSATIONS[cid]
    mid = next_id('msg-')
    MESSAGES[conv].append({
        'id': mid, 'type': 2, 'messageType': 'TYPE_SMS', 'locationId': LOC, 'contactId': cid, 'conversationId': conv,
        'dateAdded': iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)), 'body': body, 'direction': direction,
        'status': 'delivered', 'contentType': 'text/plain', 'source': 'api' if direction == 'outbound' else None,
    })
    return mid


def search_view(c):
    """Contacts as POST /contacts/search returns them: only lower case names, as the API docs say."""
    v = {k: c[k] for k in ('id', 'locationId', 'email', 'phone', 'timezone', 'tags', 'dnd', 'dndSettings', 'type', 'source',
                           'dateAdded', 'dateUpdated', 'lastActivity')}
    v['firstNameLowerCase'] = c['firstName'].lower()
    v['lastNameLowerCase'] = c['lastName'].lower()
    if c['id'] in STALE_TAGS:
        v['tags'] = list(STALE_TAGS[c['id']])   # the search index is behind the contact record
    return v


# ---- search filter validation and evaluation, per the Contacts Search API documentation ----
ALL6 = {'eq', 'not_eq', 'contains', 'not_contains', 'exists', 'not_exists'}
FIELD_OPS = {
    'id': {'eq', 'not_eq'}, 'contactName': {'eq', 'not_eq', 'exists', 'not_exists'}, 'phone': ALL6, 'tags': ALL6,
    'email': {'eq', 'not_eq', 'exists', 'not_exists'}, 'dnd': {'eq', 'not_eq', 'exists', 'not_exists'},
    'timezone': {'eq', 'not_eq', 'exists', 'not_exists'}, 'type': {'eq', 'not_eq', 'exists', 'not_exists'},
    'source': ALL6, 'firstNameLowerCase': ALL6, 'lastNameLowerCase': ALL6, 'assignedTo': {'eq', 'not_eq', 'exists', 'not_exists'},
    'dateAdded': {'range', 'exists', 'not_exists'}, 'dateUpdated': {'range', 'exists', 'not_exists'},
    'lastActivity': {'range', 'exists', 'not_exists'},
}
SORT_FIELDS = {'firstNameLowerCase', 'lastNameLowerCase', 'businessName', 'dateAdded', 'dateUpdated', 'email', 'dnd', 'source', 'lastActivity'}


def filter_problems(filters, where='filters'):
    p = []
    if not isinstance(filters, list):
        return [f'{where} must be an array']
    for n, f in enumerate(filters):
        at = f'{where}.{n}'
        if not isinstance(f, dict):
            p.append(f'{at} must be an object')
        elif 'group' in f:
            if f['group'] not in ('AND', 'OR'):
                p.append(f'{at}.group must be AND or OR')
            if not f.get('filters'):
                p.append(f'{at}.filters must not be empty')
            p += filter_problems(f.get('filters'), at + '.filters')
        else:
            field, op = f.get('field'), f.get('operator')
            if field not in FIELD_OPS:
                p.append(f'{at}.field {field!r} is not supported')
            elif op not in FIELD_OPS[field]:
                p.append(f'{at}.operator {op!r} is not supported for {field}')
            elif op in ('exists', 'not_exists') and 'value' in f:
                p.append(f'{at}: {op} takes no value')
            elif op == 'range' and (not isinstance(f.get('value'), dict) or not f['value'] or set(f['value']) - {'gt', 'lt'}):
                p.append(f'{at}: range needs an object with gt and/or lt, as in the documented example')
            elif op in ('eq', 'not_eq', 'contains', 'not_contains') and (f.get('value') is None or len(str(f.get('value'))) > 75):
                p.append(f'{at}.value is required (75 characters at most)')
    return p


def match(c, f):
    if 'group' in f:
        results = [match(c, x) for x in f['filters']]
        return all(results) if f['group'] == 'AND' else any(results)
    field, op, value = f['field'], f['operator'], f.get('value')
    have = c.get(field)
    if op == 'exists':
        return have not in (None, '', [])
    if op == 'not_exists':
        return have in (None, '', [])
    if field == 'tags':
        tags = [t.lower() for t in have or []]
        return {'eq': str(value).lower() in tags, 'not_eq': str(value).lower() not in tags,
                'contains': any(str(value).lower() in t for t in tags), 'not_contains': not any(str(value).lower() in t for t in tags)}[op]
    if op == 'range':
        if not have:
            return False
        t = datetime.fromisoformat(have.replace('Z', '+00:00'))
        ok = True
        for k, v in value.items():
            b = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
            ok &= {'gte': t >= b, 'lte': t <= b, 'gt': t > b, 'lt': t < b}[k]
        return ok
    if op == 'eq':
        return have == value
    if op == 'not_eq':
        return have != value
    return False


def free_slots(start_ms, end_ms, tz):
    """Open times: 10:00 and 15:00 local, from tomorrow on (the calendar has a one day minimum notice)."""
    offset = tz_minutes(tz) or 0
    zone = timezone(timedelta(minutes=offset))
    start, end = datetime.fromtimestamp(start_ms / 1000, zone), datetime.fromtimestamp(end_ms / 1000, zone)
    out, day = {}, start.date() + timedelta(days=1)
    while day <= end.date():
        for hour in (10, 15):
            t = datetime(day.year, day.month, day.day, hour, 0, tzinfo=zone)
            instant = t.astimezone(timezone.utc)
            if start <= t <= end and instant not in TAKEN and all(a['instant'] != instant for a in APPOINTMENTS):
                out.setdefault(day.isoformat(), {'slots': []})['slots'].append(t.isoformat())
                OFFERED_SLOTS.add(instant)
        day += timedelta(days=1)
    return out


def label(iso_text):
    """The same label the workflow builds with Intl.DateTimeFormat('en-US'), for example 'Wed, Oct 21, 10:00 AM'."""
    t = datetime.fromisoformat(iso_text)
    return f"{t.strftime('%a')}, {t.strftime('%b')} {t.day}, {(t.hour % 12) or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


# ---- Claude ----
# What the mock Claude answers, keyed by the lead's newest message. A number is an HTTP error.
SCRIPT = {
    "Yes please, I've been meaning to book": dict(intent='interested', reply='Great to hear from you, Jamie! Here are a few open times:'),
    '2 works for me': dict(intent='picked_time', slot_choice=2, reply='Perfect, booking that now.'),
    'Do you take PPO insurance?': dict(intent='question', needs_human=True,
                                                reply='Good question! I will check with the team about PPO plans and text you back.',
                                                summary='Asked about PPO insurance'),
    'Not right now, maybe in the spring': dict(intent='not_now', reply='No problem at all, Jo. We are here whenever you are ready!'),
    "please don't text me anymore": dict(intent='stop', reply=''),
    'Can I get a price for whitening?': 529,
    'Please stop sending these': 529,
    "Wrong number, I don't know any Quinn": dict(intent='wrong_person', reply='So sorry about that! We will update our records.'),
    'yes': dict(intent='interested', reply='Wonderful! Here are a few open times:'),
    '1': dict(intent='picked_time', slot_choice=1, reply=''),
    'sure, why not': dict(intent='interested', reply='Great! Here are a few open times:'),
    'the first one': dict(intent='picked_time', slot_choice=1, reply=''),
    'What are your hours?': dict(intent='question', reply='We are open Monday to Friday, 8 AM to 5 PM. Want me to send a few open times?'),
    'Do you have parking?': dict(intent='question', reply='Yes, there is free parking behind the building. Should I send a few times?'),
    'hmm ok??': 'text-only',
}
CLAUDE_TOOL_FIELDS = {'intent', 'slot_choice', 'reply', 'needs_human', 'summary'}
NO_FORCED_TOOL = re.compile(r'^claude-(fable-5-1|mythos-5-1|opus-5-5)\b')
NO_THINKING_OFF = re.compile(r'^claude-(fable|mythos|opus-5-5)\b')
# Top level fields of a Messages API request. The real API rejects anything else ("Extra inputs are not permitted").
CLAUDE_REQUEST_KEYS = {'model', 'max_tokens', 'messages', 'system', 'tools', 'tool_choice', 'thinking', 'metadata', 'stop_sequences',
                       'stream', 'temperature', 'top_p', 'top_k', 'output_config', 'service_tier', 'container', 'mcp_servers',
                       'context_management', 'inference_geo'}


def claude_problems(headers, body):
    """The checks the real Messages API would fail this request on."""
    p = []
    if headers.get('x-api-key') != ENV['ANTHROPIC_API_KEY']:
        p.append('missing or wrong x-api-key')
    if headers.get('anthropic-version') != '2023-06-01':
        p.append('missing anthropic-version')
    if not str(headers.get('content-type', '')).startswith('application/json'):
        p.append('content-type must be application/json')
    if not isinstance(body.get('model'), str) or not isinstance(body.get('max_tokens'), int) or body.get('max_tokens', 0) < 1:
        p.append('model and max_tokens are required')
    for key in sorted(set(body) - CLAUDE_REQUEST_KEYS):
        p.append(f'{key}: Extra inputs are not permitted')
    for key in ('temperature', 'top_p', 'top_k'):
        if key in body:
            p.append(f'{key} is not supported on this model')
    thinking = body.get('thinking')
    if thinking is not None and thinking not in ({'type': 'disabled'}, {'type': 'adaptive'}):
        p.append('thinking must be disabled or adaptive (budget_tokens is not supported)')
    # Per model rules from Anthropic's model docs: Claude Fable 5.1, Mythos 5.1 and Opus 5.5 refuse a forced tool call,
    # and the Fable and Mythos models and Opus 5.5 refuse thinking turned off.
    forced = isinstance(body.get('tool_choice'), dict) and body['tool_choice'].get('type') in ('tool', 'any')
    if forced and NO_FORCED_TOOL.search(str(body.get('model', ''))):
        p.append('tool_choice: type "tool" and "any" are not supported for this model.')
    if thinking == {'type': 'disabled'} and NO_THINKING_OFF.search(str(body.get('model', ''))):
        p.append('thinking: type "disabled" is not supported for this model.')
    if not isinstance(body.get('system'), str) or not body['system'].strip():
        p.append('system must be a non empty string')
    msgs = body.get('messages') or []
    if not msgs or msgs[0].get('role') != 'user' or msgs[-1].get('role') != 'user':
        p.append('messages must start and end with the user')
    if any(a.get('role') == b.get('role') for a, b in zip(msgs, msgs[1:])):
        p.append('roles must alternate')
    for m in msgs:
        content = m.get('content')
        blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
        if not isinstance(blocks, list) or not blocks or any(b.get('type') != 'text' or not str(b.get('text', '')).strip() for b in blocks):
            p.append('every message needs non empty text content')
    tools = body.get('tools') or [{}]
    schema = tools[0].get('input_schema', {})
    if (schema.get('type') != 'object' or not set(schema.get('required', [])) <= set(schema.get('properties', {}))
            or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', tools[0].get('name', ''))):
        p.append('tool needs a name and an object input_schema whose required fields exist')
    if body.get('tool_choice') != {'type': 'tool', 'name': tools[0].get('name')}:
        p.append('tool must be forced')
    return p


# ----------------------------------------------------------------------------------------------------------------------
# Mock HTTP server
# ----------------------------------------------------------------------------------------------------------------------
GHL_ERR_401 = {'statusCode': 401, 'message': 'Invalid token: access token is invalid', 'error': 'Unauthorized'}


def unprocessable(*messages):
    return 422, {'statusCode': 422, 'message': list(messages), 'error': 'Unprocessable Entity'}


def bad_request(message):
    return 400, {'statusCode': 400, 'message': message}


# Body properties each endpoint accepts, from GoHighLevel's OpenAPI files. Like the real API, the mock answers
# "property X should not exist" for anything else.
UPDATE_CONTACT_KEYS = {'firstName', 'lastName', 'name', 'email', 'phone', 'address1', 'city', 'state', 'postalCode', 'website', 'timezone',
                       'dnd', 'dndSettings', 'inboundDndSettings', 'tags', 'customFields', 'source', 'dateOfBirth', 'country', 'assignedTo'}
SEARCH_KEYS = {'locationId', 'page', 'pageLimit', 'searchAfter', 'filters', 'sort', 'query'}
SEND_MESSAGE_KEYS = {'type', 'subType', 'contactId', 'appointmentId', 'attachments', 'emailFrom', 'emailCc', 'emailBcc', 'html', 'message',
                     'subject', 'replyMessageId', 'templateId', 'threadId', 'scheduledTimestamp', 'conversationProviderId', 'emailTo',
                     'customSubtypeId', 'emailReplyMode', 'fromNumber', 'toNumber', 'forward', 'status', 'usesNativeSchedulingAi', 'optimizationPeriod'}
APPOINTMENT_KEYS = {'title', 'meetingLocationType', 'meetingLocationId', 'overrideLocationConfig', 'appointmentStatus', 'assignedUserId',
                    'description', 'address', 'ignoreDateRange', 'toNotify', 'ignoreFreeSlotValidation', 'rrule', 'calendarId', 'locationId',
                    'contactId', 'startTime', 'endTime'}
CREATE_OPPORTUNITY_KEYS = {'pipelineId', 'locationId', 'name', 'pipelineStageId', 'status', 'contactId', 'monetaryValue', 'assignedTo', 'customFields'}
DND_CHANNELS = {'Call', 'Email', 'SMS', 'WhatsApp', 'GMB', 'FB'}
MESSAGE_TYPES = {'SMS', 'RCS', 'Email', 'WhatsApp', 'IG', 'FB', 'Custom', 'Live_Chat', 'TIKTOK'}


def unknown_keys(body, allowed):
    extra = sorted(set(body) - allowed)
    return unprocessable(*[f'property {k} should not exist' for k in extra]) if extra else None


def ghl(method, path, query, headers, body):
    """Returns (status, json). Validates like the real GoHighLevel API v2 and keeps state."""
    if headers.get('authorization') != 'Bearer ' + GHL_KEY:
        return 401, GHL_ERR_401
    version = '2021-04-15' if path.startswith(('/conversations', '/calendars')) else '2021-07-28'
    if headers.get('version') != version:
        return bad_request(f'Version header must be {version} for this endpoint')
    if method in ('POST', 'PUT', 'DELETE') and body is not None and not str(headers.get('content-type', '')).startswith('application/json'):
        return bad_request('Content-Type must be application/json')
    q = {k: v[0] for k, v in query.items()}

    if method == 'POST' and path == '/contacts/search':
        if FAIL['search']:
            return 500, {'statusCode': 500, 'message': 'Internal server error'}
        if unknown_keys(body, SEARCH_KEYS):
            return unknown_keys(body, SEARCH_KEYS)
        if body.get('locationId') != LOC:
            return 403, {'statusCode': 403, 'message': 'The token does not have access to this location.'}
        size, page = body.get('pageLimit'), body.get('page', 1)
        if not isinstance(size, int) or not 1 <= size <= 500:
            return unprocessable('pageLimit must be a number from 1 to 500')
        if 'searchAfter' in body and 'page' in body:
            return unprocessable('page must not be sent together with searchAfter')
        if not isinstance(page, int) or page < 1 or page * size > 10000:
            return unprocessable('page and pageLimit can reach 10,000 records at most')
        problems = filter_problems(body.get('filters', []))
        sort = body.get('sort', [])
        if len(sort) > 2 or any(s.get('field') not in SORT_FIELDS or s.get('direction') not in ('asc', 'desc') for s in sort):
            problems.append('sort supports up to 2 known fields with asc or desc')
        if problems:
            return unprocessable(*problems)
        rows = [search_view(c) for c in CONTACTS.values()]
        rows = [r for r in rows if all(match(r, f) for f in body.get('filters', []))]
        for s in reversed(sort):
            present = [r for r in rows if r.get(s['field']) not in (None, '')]
            missing = [r for r in rows if r.get(s['field']) in (None, '')]
            present.sort(key=lambda r: r[s['field']], reverse=s['direction'] == 'desc')
            rows = present + missing
        return 200, {'contacts': rows[(page - 1) * size: page * size], 'total': len(rows)}

    m = re.fullmatch(r'/contacts/([^/]+)(/tags)?', path)
    if m:
        c = CONTACTS.get(m.group(1))
        if not c:
            return bad_request('Contact not found')
        if m.group(2):
            if method != 'POST':
                return 404, {'statusCode': 404, 'message': 'Cannot ' + method + ' ' + path}
            tags = body.get('tags')
            if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
                return unprocessable('tags must be a non empty array of strings')
            if c['id'] in FAIL['tag']:
                return 500, {'statusCode': 500, 'message': 'Internal server error'}
            for t in tags:
                if t.lower() not in c['tags']:
                    c['tags'].append(t.lower())
            return 201, {'tags': c['tags']}
        if method == 'GET':
            return 200, {'contact': copy.deepcopy(c)}
        if method == 'PUT':
            if c['id'] in FAIL['put_contact']:
                return 500, {'statusCode': 500, 'message': 'Internal server error'}
            extra = set(body) - UPDATE_CONTACT_KEYS
            if extra:
                return unprocessable(*[f'property {k} should not exist' for k in sorted(extra)])
            if 'dndSettings' in body:
                d = body['dndSettings']
                if not isinstance(d, dict) or set(d) - DND_CHANNELS or any(
                        not isinstance(v, dict) or v.get('status') not in ('active', 'inactive', 'permanent') for v in d.values()):
                    return unprocessable('dndSettings must map channels to { status: active | inactive | permanent }')
                c['dndSettings'] = copy.deepcopy(d)   # replaces the whole object, so callers must merge
            if 'dnd' in body:
                c['dnd'] = bool(body['dnd'])
            return 200, {'succeded': True, 'contact': copy.deepcopy(c)}

    if method == 'POST' and path == '/conversations/messages':
        if unknown_keys(body, SEND_MESSAGE_KEYS):
            return unknown_keys(body, SEND_MESSAGE_KEYS)
        if body.get('type') not in MESSAGE_TYPES:
            return unprocessable('type must be one of ' + ', '.join(sorted(MESSAGE_TYPES)))
        c = CONTACTS.get(body.get('contactId'))
        if not c:
            return bad_request('Contact not found')
        if body['type'] == 'SMS':
            if not isinstance(body.get('message'), str) or not body['message'].strip():
                return unprocessable('message should not be empty')
            if not c.get('phone'):
                return bad_request('Contact has no phone number')
            if c.get('dnd') or (c.get('dndSettings', {}).get('SMS', {}).get('status') in ('active', 'permanent')):
                return bad_request('Cannot send message as DND is active for SMS.')
            if c['id'] in FAIL['sms_429_once']:
                FAIL['sms_429_once'].discard(c['id'])
                return 429, {'statusCode': 429, 'message': 'Too Many Requests'}
            if c['id'] in FAIL['sms']:
                return bad_request('The phone number is not a valid mobile number.')
            mid = add_message(c['id'], 'outbound', body['message'])
            return 200, {'conversationId': CONVERSATIONS[c['id']], 'messageId': mid}

    if method == 'GET' and path == '/conversations/search':
        if q.get('locationId') != LOC:
            return bad_request('locationId is required')
        rows = [{'id': conv, 'contactId': cid, 'locationId': LOC, 'lastMessageBody': (MESSAGES[conv][-1]['body'] if MESSAGES[conv] else ''),
                 'lastMessageType': 'TYPE_SMS', 'type': 'TYPE_PHONE', 'unreadCount': 0, 'fullName': CONTACTS[cid]['name'],
                 'contactName': CONTACTS[cid]['name'], 'email': CONTACTS[cid]['email'], 'phone': CONTACTS[cid]['phone']}
                for cid, conv in CONVERSATIONS.items() if not q.get('contactId') or q['contactId'] == cid]
        return 200, {'conversations': rows[:int(q.get('limit', 20))], 'total': len(rows)}

    m = re.fullmatch(r'/conversations/([^/]+)/messages', path)
    if method == 'GET' and m:
        conv = m.group(1)
        if conv not in MESSAGES:
            return bad_request('Conversation not found')
        if conv in FAIL['messages']:
            return 500, {'statusCode': 500, 'message': 'Internal server error'}
        limit = int(q.get('limit', 20))
        if not 1 <= limit <= 100:
            return unprocessable('limit must be from 1 to 100')
        newest = list(reversed(MESSAGES[conv]))[:limit]
        page = {'lastMessageId': newest[-1]['id'] if newest else '', 'nextPage': len(MESSAGES[conv]) > limit, 'messages': newest}
        return 200, (page if conv in FAIL['flat_messages'] else {'messages': page, 'traceId': 'mock-trace'})

    m = re.fullmatch(r'/calendars/([^/]+)/free-slots', path)
    if method == 'GET' and m:
        if m.group(1) != CAL:
            return bad_request('Calendar not found')
        if not re.fullmatch(r'\d{12,14}', q.get('startDate', '')) or not re.fullmatch(r'\d{12,14}', q.get('endDate', '')):
            return unprocessable('startDate and endDate must be timestamps in milliseconds')
        start, end = int(q['startDate']), int(q['endDate'])
        if end <= start or end - start > 31 * 86400000:
            return bad_request('Date range cannot be more than 31 days')
        if q.get('timezone') and tz_minutes(q['timezone']) is None:
            return unprocessable('timezone is not valid')
        if FAIL['slots']:
            return 503, {'statusCode': 503, 'message': 'Service Unavailable'}
        return 200, {} if FAIL['no_slots'] else free_slots(start, end, q.get('timezone'))

    if method == 'POST' and path == '/calendars/events/appointments':
        if unknown_keys(body, APPOINTMENT_KEYS):
            return unknown_keys(body, APPOINTMENT_KEYS)
        missing = [k for k in ('calendarId', 'locationId', 'contactId', 'startTime') if not body.get(k)]
        if missing:
            return unprocessable(*[f'{k} should not be empty' for k in missing])
        if body['calendarId'] != CAL or body['locationId'] != LOC or body['contactId'] not in CONTACTS:
            return bad_request('Calendar, location or contact not found')
        try:
            start = datetime.fromisoformat(body['startTime'])
            end = datetime.fromisoformat(body['endTime']) if body.get('endTime') else start + timedelta(minutes=30)
        except ValueError:
            return unprocessable('startTime and endTime must be ISO 8601 dates')
        if start.tzinfo is None or end <= start:
            return unprocessable('startTime needs a time zone offset and endTime must be after it')
        if body.get('appointmentStatus') not in (None, 'new', 'confirmed', 'cancelled', 'showed', 'noshow', 'invalid'):
            return unprocessable('appointmentStatus is not valid')
        instant = start.astimezone(timezone.utc)
        if instant not in OFFERED_SLOTS or instant in TAKEN or any(a['instant'] == instant for a in APPOINTMENTS):
            return bad_request('The slot you have selected is no longer available.')
        appt = {'id': next_id('appt-'), 'calendarId': CAL, 'locationId': LOC, 'contactId': body['contactId'], 'startTime': body['startTime'],
                'endTime': body.get('endTime'), 'title': body.get('title', ''), 'appointmentStatus': body.get('appointmentStatus', 'new'),
                'isRecurring': False, 'instant': instant}
        APPOINTMENTS.append(appt)
        return 200, {k: v for k, v in appt.items() if k != 'instant'}

    if method == 'GET' and path == '/opportunities/search':
        if q.get('location_id') != LOC:
            return unprocessable('location_id should not be empty')
        if q.get('status', 'all') not in ('open', 'won', 'lost', 'abandoned', 'all'):
            return unprocessable('status must be one of open, won, lost, abandoned, all')
        if not re.fullmatch(r'\d{1,3}', q.get('limit', '20')) or not 1 <= int(q.get('limit', '20')) <= 100:
            return unprocessable('limit must be a number from 1 to 100')
        rows = [o for o in OPPORTUNITIES.values() if (not q.get('contact_id') or o['contactId'] == q['contact_id'])
                and (not q.get('pipeline_id') or o['pipelineId'] == q['pipeline_id']) and q.get('status', 'all') in ('all', o['status'])]
        return 200, {'opportunities': copy.deepcopy(rows[:int(q.get('limit', 20))]),
                     'meta': {'total': len(rows), 'currentPage': 1, 'nextPage': None, 'prevPage': None}, 'aggregations': {}}

    m = re.fullmatch(r'/opportunities/([^/]+)', path)
    if method == 'PUT' and m:
        o = OPPORTUNITIES.get(m.group(1))
        if not o:
            return bad_request('Opportunity not found')
        extra = set(body) - {'pipelineId', 'name', 'pipelineStageId', 'status', 'monetaryValue', 'assignedTo', 'customFields'}
        if extra:
            return unprocessable(*[f'property {k} should not exist' for k in sorted(extra)])
        pipe = body.get('pipelineId', o['pipelineId'])
        if pipe not in PIPELINES or body.get('pipelineStageId', o['pipelineStageId']) not in PIPELINES[pipe]:
            return bad_request('Pipeline stage does not belong to the pipeline')
        if body.get('status', 'open') not in ('open', 'won', 'lost', 'abandoned'):
            return unprocessable('status is not valid')
        o.update({k: v for k, v in body.items()})
        return 200, {'opportunity': copy.deepcopy(o)}

    if method == 'POST' and path == '/opportunities/':
        if FAIL['opportunity_create']:
            return 500, {'statusCode': 500, 'message': 'Internal server error'}
        if unknown_keys(body, CREATE_OPPORTUNITY_KEYS):
            return unknown_keys(body, CREATE_OPPORTUNITY_KEYS)
        missing = [k for k in ('pipelineId', 'locationId', 'name', 'status', 'contactId') if not body.get(k)]
        if missing:
            return unprocessable(*[f'{k} should not be empty' for k in missing])
        if body['pipelineId'] not in PIPELINES or body.get('pipelineStageId') not in PIPELINES[body['pipelineId']]:
            return bad_request('Pipeline stage does not belong to the pipeline')
        # GoHighLevel's default: one opportunity per contact per pipeline, whatever its status ("Allow Multiple
        # Opportunities per Contact" is off unless the sub-account turns it on).
        if any(o['contactId'] == body['contactId'] and o['pipelineId'] == body['pipelineId'] for o in OPPORTUNITIES.values()):
            return bad_request('Can not create duplicate opportunity for the contact.')
        oid = next_id('opp-')
        OPPORTUNITIES[oid] = {'id': oid, 'name': body['name'], 'pipelineId': body['pipelineId'], 'pipelineStageId': body['pipelineStageId'],
                              'status': body['status'], 'contactId': body['contactId'], 'locationId': LOC, 'monetaryValue': body.get('monetaryValue', 0)}
        return 201, {'opportunity': copy.deepcopy(OPPORTUNITIES[oid])}

    return 404, {'statusCode': 404, 'message': f'Cannot {method} {path}'}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return self._send(400, {'statusCode': 400, 'message': 'Body is not valid JSON'})
        path = url.path
        # /slack/hook is the reactivation workflow's own Slack messages, /slack/errors the error alert workflow
        service = ('claude' if path == '/v1/messages' else 'alert' if path.startswith('/slack/errors')
                   else 'slack' if path.startswith('/slack') else 'ghl')
        with LOCK:
            if service == 'claude':
                status, resp = self._claude(headers, body or {})
            elif service in ('slack', 'alert'):
                ok = method == 'POST' and isinstance(body, dict) and isinstance(body.get('text'), str) and body['text'].strip()
                status, resp = (200, 'ok') if ok else (400, 'invalid_payload')
            else:
                status, resp = ghl(method, path, parse_qs(url.query), headers, body)
            LOG.append({'service': service, 'method': method, 'path': path, 'query': {k: v[0] for k, v in parse_qs(url.query).items()},
                        'headers': headers, 'body': body, 'status': status, 'response': resp, 't': time.time()})
        return self._send(status, resp)

    def _claude(self, headers, body):
        problems = claude_problems(headers, body)
        if problems:
            return 400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '; '.join(problems)}}
        newest = body['messages'][-1]['content'][-1]['text'].split('Newest message from the lead:\n', 1)[-1]
        answer = SCRIPT.get(newest)
        if isinstance(answer, int):
            return answer, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}
        if answer is None or answer == 'text-only':
            return 200, {'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': body['model'],
                         'content': [{'type': 'text', 'text': 'I am not sure what to say.'}], 'stop_reason': 'end_turn',
                         'usage': {'input_tokens': 900, 'output_tokens': 10}}
        tool_input = {'intent': 'question', 'slot_choice': 0, 'reply': '', 'needs_human': False, 'summary': 'Replied', **answer}
        assert set(tool_input) == CLAUDE_TOOL_FIELDS
        return 200, {'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': body['model'],
                     'content': [{'type': 'tool_use', 'id': 'toolu_mock', 'name': body['tools'][0]['name'], 'input': tool_input}],
                     'stop_reason': 'tool_use', 'usage': {'input_tokens': 1100, 'output_tokens': 90}}

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_DELETE(self):
        self._handle('DELETE')


CONNECTION_ERRORS = []


class MockServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        CONNECTION_ERRORS.append(repr(sys.exc_info()[1]))


# ----------------------------------------------------------------------------------------------------------------------
# n8n helpers
# ----------------------------------------------------------------------------------------------------------------------
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(env, suffix=''):
    """The workflow with $env values replaced by test values, API hosts pointed at the mocks and test webhook paths."""
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if 'webhookId' in node:
            node['webhookId'] = str(uuid.uuid4())
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = f"{SLUG}-e2e-{'run' if node['name'] == 'Run Now' else 'reply'}{suffix}"
        if node['type'] == 'n8n-nodes-base.scheduleTrigger':
            node['parameters']['rule'] = {'interval': [{'field': 'minutes', 'minutesInterval': 1}]}   # so the test does not wait an hour
    return wf


TEMP_NAME = f'TEST {SLUG} (temporary)'


def import_active(env, suffix, error_wid):
    """Imports and activates a test copy that reports failures to the temporary error alert workflow."""
    wf = test_copy(env, suffix)
    settings = {**wf.get('settings', {}), 'errorWorkflow': error_wid}
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': settings})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    time.sleep(2)
    return wid


def import_error_workflow():
    """The repo's error alert workflow, posting to the mock Slack. n8n only runs an error workflow that is active."""
    wf = json.loads((WORKFLOW.parent / 'error-alert-slack.json').read_text(encoding='utf-8'))
    for node in wf['nodes']:
        node['parameters'] = json.loads(json.dumps(node.get('parameters', {})).replace('$env.SLACK_WEBHOOK_URL', json.dumps(MOCK + '/slack/errors').replace('"', '\\"')))
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': wf.get('settings', {})})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    return wid


def remove(wid):
    try:
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
    finally:
        api('DELETE', f'/api/v1/workflows/{wid}')


def remove_leftovers():
    """Deletes temporary copies left behind by an earlier, interrupted run of this test (exact name match only)."""
    cursor, found = None, []
    while True:
        page = api('GET', '/api/v1/workflows?limit=250' + (f'&cursor={cursor}' if cursor else ''))
        found += [w['id'] for w in page.get('data', []) if w.get('name') == TEMP_NAME]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    for wid in found:
        remove(wid)
    return len(found)


def alerts_for(ids, timeout=90):
    """Error workflow alerts that name these executions. Alerts arrive after the failed run has finished."""
    end = time.time() + timeout
    while True:
        got = [e for e in LOG if e['service'] == 'alert']
        linked = {i: [e for e in got if re.search(rf'/executions/{i}(?!\d)', e['body']['text'])] for i in ids}
        if all(linked.values()) or time.time() > end:
            return linked, got
        time.sleep(1)


class Runner:
    def __init__(self, wid, suffix=''):
        self.wid, self.suffix, self.sent, self.results, self.mark = wid, suffix, 0, [], 0
        self.ids = {}   # execution ids of runs whose error alert is checked by content

    def executions(self):
        return api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', [])

    def wait(self, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.executions() if e.get('mode') == 'webhook' and e.get('stoppedAt')]
            if len(done) >= self.sent:
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def post(self, which, payload, headers=None):
        """POST to the run or reply webhook. Returns (status, response body, finished execution)."""
        self.mark = len(LOG)
        req = urllib.request.Request(f'{N8N}/webhook/{SLUG}-e2e-{which}{self.suffix}', method='POST', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        self.sent += 1
        return status, text, self.wait()

    def run_campaign(self, dry=False, secret=ENV['RUN_NOW_SECRET']):
        return self.post('run', {'dryRun': True} if dry else {}, {'X-Run-Secret': secret} if secret is not None else {})

    def stopped_at(self, ex):
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def new(self, service=None, method=None, path=None, contact=None):
        """Requests to the mocks since the last webhook was sent. Error workflow alerts are left out (they arrive
        late, after the run that failed) and are checked on their own with alerts_for()."""
        out = []
        for e in LOG[self.mark:]:
            if e['service'] == 'alert' and service != 'alert':
                continue
            if service and e['service'] != service or method and e['method'] != method:
                continue
            if path and not re.fullmatch(path, e['path']):
                continue
            if contact and contact not in json.dumps([e['path'], e['body'], e['query']]):
                continue
            out.append(e)
        return out

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))


def sms(entries):
    return [e for e in entries if e['service'] == 'ghl' and e['path'] == '/conversations/messages' and e['method'] == 'POST']


def delivered(entries):
    return [e for e in sms(entries) if e['status'] == 200]


def tag_calls(entries, cid=None):
    return [e for e in entries if re.fullmatch(r'/contacts/[^/]+/tags', e['path']) and (cid is None or f'/contacts/{cid}/' in e['path'])]


def inbound_payload(cid, text, mid):
    c = CONTACTS[cid]
    return {'type': 'InboundMessage', 'locationId': LOC, 'attachments': [], 'body': text, 'contactId': cid, 'contentType': 'text/plain',
            'conversationId': CONVERSATIONS[cid], 'dateAdded': iso(datetime.now(timezone.utc)), 'direction': 'inbound', 'messageType': 'SMS',
            'messageId': mid, 'status': 'delivered', 'from': c['phone'], 'to': '+15550109999', 'messageTypeId': 2, 'messageTypeString': 'TYPE_SMS'}


def workflow_payload(cid, text):
    """What the standard Webhook action of a GoHighLevel workflow sends: contact fields, message, customData."""
    c = CONTACTS[cid]
    return {'contact_id': cid, 'first_name': c['firstName'], 'last_name': c['lastName'], 'full_name': c['name'], 'phone': c['phone'],
            'email': c['email'], 'tags': ','.join(c['tags']), 'location': {'id': LOC, 'name': 'Bright Smile Dental'},
            'message': {'type': 2, 'body': text}, 'workflow': {'id': 'wf-mock', 'name': 'Reactivation replies'},
            'customData': {'secret': ENV['REPLY_WEBHOOK_SECRET']}}


SECRET_HEADER = {'X-Reactivation-Secret': ENV['REPLY_WEBHOOK_SECRET']}


def lead_says(r, cid, text, shape='inbound', duplicate_of=None, stored=True):
    """The lead texts back: GoHighLevel stores the message, then the GoHighLevel workflow calls the reply webhook.
    stored=False plays a webhook that arrives a moment before the message shows up in the conversation history."""
    if duplicate_of:
        payload, headers = duplicate_of
    else:
        mid = add_message(cid, 'inbound', text) if stored else next_id('msg-')
        payload = inbound_payload(cid, text, mid) if shape == 'inbound' else workflow_payload(cid, text)
        headers = SECRET_HEADER if shape == 'inbound' else {}
    status, body, ex = r.post('reply', payload, headers)
    return (payload, headers), status, ex


def claude_calls(r):
    return [e for e in r.new('claude') if e['status'] == 200]


# ----------------------------------------------------------------------------------------------------------------------
# Test data (all made up: 555 numbers, example.com emails, a made up dental practice)
# ----------------------------------------------------------------------------------------------------------------------
def seed():
    add_contact('c-lee', 'Lee', 'Park', '+15550100001', IN_TZ, [], 10)                                    # active recently
    add_contact('c-dana', 'Dana', 'Ortiz', '+15550100002', IN_TZ, ['reactivate'], 3, dnd=True)            # DND
    add_contact('c-gale', 'Gale', 'Kim', None, IN_TZ, ['reactivate'], 3)                                   # no phone
    add_contact('c-hana', 'Hana', 'Ito', '+15550100004', IN_TZ, ['reactivate', 'customer-active'], 3)     # excluded tag
    add_contact('c-erin', 'Erin', 'Moss', '+15550100005', IN_TZ, ['reactivate'], 8, dnd_settings={'SMS': {'status': 'active'}})
    add_contact('c-fran', 'Fran', 'Lopez', '+15550100006', OUT_TZ, ['reactivate'], 9)                     # 3 AM there
    add_contact('c-mo', 'Mo', 'Reyes', '+15550100007', IN_TZ, ['reactivate', SENT], 1)                    # already texted
    STALE_TAGS['c-mo'] = ['reactivate']                                                                    # ...but the index lags
    add_contact('c-jamie', 'Jamie', 'Rivera', '+15550100008', IN_TZ, ['reactivate'], 2)
    add_contact('c-ivan', 'Ivan', 'Diaz', '+15550100009', 'Mars/Olympus', ['reactivate'], 3)              # invalid time zone
    add_contact('c-jo', 'Jo', 'Banks', '+15550100010', None, ['reactivate'], 4)                            # no time zone
    add_contact('c-tess', 'Tess', 'Gray', '+15550100011', IN_TZ, ['reactivate'], 5)
    add_contact('c-kai', 'Kai', 'Noor', '+15550100012', IN_TZ, ['reactivate'], 6, dnd_settings={'Email': {'status': 'active', 'message': 'asked by email'}})
    add_contact('c-nia', 'Nia', 'Brooks', '+15550100013', IN_TZ, ['reactivate'], 7)
    add_contact('c-sam', 'Sam', 'Cole', '+15550100014', IN_TZ, [], 200)                                    # dormant by activity
    # Leads texted by an earlier campaign run, used for the reply scenarios
    for cid, first, last, phone in (('c-pat', 'Pat', 'Lee', '+15550100015'), ('c-quinn', 'Quinn', 'Hale', '+15550100016'),
                                    ('c-rae', 'Rae', 'Kim', '+15550100017'), ('c-sol', 'Sol', 'Ortiz', '+15550100018'),
                                    ('c-tia', 'Tia', 'Ward', '+15550100019'), ('c-wes', 'Wes', 'Hart', '+15550100020'),
                                    ('c-ana', 'Ana', 'Cruz', '+15550100023'), ('c-ben', 'Ben', 'Ford', '+15550100024'),
                                    ('c-cy', 'Cy', 'Young', '+15550100025'), ('c-dee', 'Dee', 'Ross', '+15550100026'),
                                    ('c-eve', 'Eve', 'Lam', '+15550100027'), ('c-fay', 'Fay', 'Dunn', '+15550100028'),
                                    ('c-gil', 'Gil', 'Shaw', '+15550100031'), ('c-hal', 'Hal', 'Bower', '+15550100032'),
                                    ('c-ida', 'Ida', 'Lund', '+15550100033'), ('c-kim', 'Kim', 'Vale', '+15550100034')):
        add_contact(cid, first, last, phone, IN_TZ, ['reactivate', SENT], 30)
        add_message(cid, 'outbound', CAMPAIGN_TEXT.format(first=first), minutes_ago=600)
    add_contact('c-uma', 'Uma', 'Shah', '+15550100021', IN_TZ, [], 1)                                      # never part of the campaign
    add_message('c-uma', 'outbound', 'Your appointment reminder for Friday.', minutes_ago=300)
    OPPORTUNITIES['opp-sol-old'] = {'id': 'opp-sol-old', 'name': 'Sol Ortiz', 'pipelineId': PIPE, 'pipelineStageId': 'stage-lost',
                                    'status': 'lost', 'contactId': 'c-sol', 'locationId': LOC, 'monetaryValue': 0}
    # A past sale: must never be reopened
    OPPORTUNITIES['opp-fay-won'] = {'id': 'opp-fay-won', 'name': 'Fay Dunn', 'pipelineId': PIPE, 'pipelineStageId': 'stage-contacted',
                                    'status': 'won', 'contactId': 'c-fay', 'locationId': LOC, 'monetaryValue': 180}


# Runs in the main copy that must end as failed (so the error workflow alerts): 2 refused Run Now calls, campaign run 2,
# search down, 2 refused reply webhooks, Claude down (twice), Claude without the tool call, slot taken, contact not
# found, history not loaded, calendar down, opportunity not saved, won opportunity left alone, DND not saved, reply SMS
# rejected.
EXPECTED_FAILED_RUNS = 17


def edit_static(r, change):
    """Changes the workflow's static data through the n8n API (PUT keeps the workflow active). Used to let time pass:
    the test cannot wait for an offered time to come and go."""
    wf = api('GET', f'/api/v1/workflows/{r.wid}')
    data = wf.get('staticData') or {}
    data = json.loads(data) if isinstance(data, str) else data
    change(data.setdefault('global', {}))
    settings = {k: v for k, v in (wf.get('settings') or {}).items() if k in ('executionOrder', 'errorWorkflow')}
    api('PUT', f'/api/v1/workflows/{r.wid}', {'name': wf['name'], 'nodes': wf['nodes'], 'connections': wf['connections'],
                                             'settings': settings, 'staticData': data})
    time.sleep(2)


def offered_to(r, cid):
    """The times the workflow remembers offering this lead, as saved in its static data."""
    static = (api('GET', f'/api/v1/workflows/{r.wid}').get('staticData') or {})
    static = json.loads(static) if isinstance(static, str) else static
    return ((static.get('global') or {}).get('leads') or {}).get(cid, {}).get('offered') or []


def first_time_passed(g, cid):
    """Moves the first offered time into the past, as if the lead answered after it."""
    g['leads'][cid]['offered'][0]['start'] = iso(datetime.now(timezone.utc) - timedelta(hours=2))

EXPECTED_SEARCH_FILTERS = [
    {'field': 'phone', 'operator': 'exists'},
    {'field': 'dnd', 'operator': 'not_eq', 'value': True},
    {'field': 'tags', 'operator': 'not_eq', 'value': SENT},
    {'field': 'tags', 'operator': 'not_eq', 'value': 'customer-active'},
    {'group': 'OR', 'filters': [{'field': 'tags', 'operator': 'eq', 'value': 'reactivate'}, {'field': 'lastActivity', 'operator': 'range'}]},
]


def run(r):
    # ---------------------------------------------------------------- campaign
    status, body, ex = r.run_campaign(secret='wrong-secret')
    node, _ = r.stopped_at(ex)
    r.check('Run Now, wrong secret', 'refused with 401, nothing searched or sent',
            status == 401 and not r.new() and node == 'Stop: Bad Run Secret', f'{status}, ended at {node}')
    status, body, ex = r.run_campaign(secret=None)
    node, _ = r.stopped_at(ex)
    r.check('Run Now, no secret', 'refused with 401', status == 401 and not r.new() and node == 'Stop: Bad Run Secret', f'{status}')

    status, body, ex = r.run_campaign(dry=True)
    searches = r.new('ghl', 'POST', '/contacts/search')
    slack = [e['body']['text'] for e in r.new('slack')]
    r.check('Dry run', 'answers 202 at once, then only searches: no tags, no texts',
            status == 202 and json.loads(body) == {'started': True, 'dryRun': True} and searches and not tag_calls(r.new()) and not sms(r.new())
            and not r.new('ghl', 'GET'), f'{status} {body}')
    first = searches[0]['body'] if searches else {}
    filters = copy.deepcopy(first.get('filters', []))
    value = filters[-1]['filters'][1].pop('value', {}) if len(filters) == 5 and filters[-1].get('filters') else {}
    lt = value.get('lt', '') if isinstance(value, dict) and set(value) == {'lt'} else ''
    cutoff_ok = bool(lt) and abs(datetime.fromisoformat(lt.replace('Z', '+00:00')) - (datetime.now(timezone.utc) - timedelta(days=90))) < timedelta(minutes=5)
    r.check('Dry run', 'search body uses the documented filters (tag OR lastActivity range lt 90 days ago, no DND, not texted, not excluded)',
            filters == EXPECTED_SEARCH_FILTERS and cutoff_ok and first.get('locationId') == LOC and first.get('pageLimit') == 3
            and first.get('sort') == [{'field': 'lastActivity', 'direction': 'desc'}], json.dumps(first)[:160])
    r.check('Dry run', 'pages through the search until a short page (pages 1, 2, 3, 4)',
            [e['body'].get('page') for e in searches] == [1, 2, 3, 4] and all(e['status'] == 200 for e in searches), f'{len(searches)} pages')
    r.check('Dry run', 'Slack lists the 4 leads it would text (batch limit 4) and the skip reasons',
            len(slack) == 1 and 'dry run' in slack[0] and 'Would text 4: Mo Reyes, Jamie Rivera, Ivan Diaz, Jo Banks' in slack[0]
            and 'Skipped: 6 (4 over the batch limit, 1 SMS DND, 1 quiet hours)' in slack[0], slack[0].splitlines()[1] if slack else 'no alert')

    FAIL['sms_429_once'].add('c-jamie')
    status, body, ex = r.run_campaign()
    node, _ = r.stopped_at(ex)
    texts = delivered(r.new())
    r.check('Campaign run 1', 'texts the 3 eligible leads, the stale search result is caught by the fresh contact check',
            [e['body']['contactId'] for e in texts] == ['c-jamie', 'c-ivan', 'c-jo'] and not sms(r.new(contact='c-mo')) and node == 'Any Sends Failed?',
            f'texted {[e["body"]["contactId"] for e in texts]}, ended at {node}')
    r.check('Campaign run 1', 'exact SMS body: type SMS, contact id, first name, offer and STOP line',
            all(e['body'] == {'type': 'SMS', 'contactId': e['body']['contactId'], 'message': CAMPAIGN_TEXT.format(first=CONTACTS[e['body']['contactId']]['firstName'])}
                for e in texts) and len(texts) == 3, texts[0]['body']['message'][:60] if texts else '')
    order_ok = True
    for cid in ('c-jamie', 'c-ivan', 'c-jo'):
        seq = [('tag' if e['path'].endswith('/tags') else 'sms') for e in r.new('ghl', 'POST', contact=cid)
               if e['path'].endswith('/tags') or e['path'] == '/conversations/messages']
        order_ok &= seq[0] == 'tag' and tag_calls(r.new(), cid)[0]['body'] == {'tags': [SENT]}
    r.check('Campaign run 1', 'each lead is tagged reactivation-sent before the text goes out', order_ok, 'tag, then SMS')
    gaps = [b['t'] - a['t'] for a, b in zip(texts, texts[1:])]
    r.check('Campaign run 1', 'throttled: at least SEND_DELAY_SECONDS between texts', len(gaps) == 2 and min(gaps) >= 0.9, f'gaps {[round(g, 1) for g in gaps]}s')
    jamie = sms(r.new(contact='c-jamie'))
    r.check('Campaign run 1', 'GoHighLevel rate limit (429) is retried and the lead gets exactly one text',
            [e['status'] for e in jamie] == [429, 200], f"statuses {[e['status'] for e in jamie]}")
    r.check('Campaign run 1', 'invalid and missing time zones fall back to BUSINESS_TZ (Ivan and Jo were texted)',
            {'c-ivan', 'c-jo'} <= {e['body']['contactId'] for e in texts}, 'Mars/Olympus and no time zone')
    summary = [e['body']['text'] for e in r.new('slack')]
    r.check('Campaign run 1', 'Slack summary with the names and skip reasons',
            len(summary) == 1 and 'Texted: 3 (Jamie Rivera, Ivan Diaz, Jo Banks)' in summary[0]
            and 'Skipped: 7 (4 over the batch limit, 1 SMS DND, 1 quiet hours, 1 already texted)' in summary[0],
            summary[0].splitlines()[1] if summary else 'no summary')

    FAIL['tag'].add('c-kai')
    FAIL['sms'].add('c-nia')
    status, body, ex = r.run_campaign()
    r.ids['sends_failed'] = ex['id']
    node, error = r.stopped_at(ex)
    texts = delivered(r.new())
    r.check('Campaign run 2 (failures)', 'tag call fails: retried 3 times and the lead is NOT texted',
            len(tag_calls(r.new(), 'c-kai')) == 3 and not sms(r.new(contact='c-kai')), f"{len(tag_calls(r.new(), 'c-kai'))} tag attempts")
    nia = sms(r.new(contact='c-nia'))
    r.check('Campaign run 2 (failures)', 'SMS rejected: retried 3 times, lead tagged reactivation-send-failed',
            len(nia) == 3 and all(e['status'] == 400 for e in nia) and FAILED in CONTACTS['c-nia']['tags'], f'{len(nia)} attempts')
    r.check('Campaign run 2 (failures)', 'the other lead is still texted and the run ends as failed with both names',
            [e['body']['contactId'] for e in texts] == ['c-tess'] and node == 'Stop: Sends Failed' and 'Kai Noor' in error and 'Nia Brooks' in error
            and 'tag failed, so no SMS was sent' in error and 'SMS not sent' in error, f'ended at {node}')

    FAIL['tag'].discard('c-kai')
    r.run_campaign()
    texts = delivered(r.new())
    r.check('Campaign run 3', 'only leads never texted before: Kai (tag works again) and Sam (dormant 200 days, no tag)',
            [e['body']['contactId'] for e in texts] == ['c-kai', 'c-sam'], f"texted {[e['body']['contactId'] for e in texts]}")
    status, body, ex = r.run_campaign()
    node, _ = r.stopped_at(ex)
    summary = [e['body']['text'] for e in r.new('slack')]
    r.check('Campaign run 4 (re-run)', 'nothing left to send: no texts, no tags, summary says Texted: 0',
            not sms(r.new()) and not tag_calls(r.new()) and summary and 'Texted: 0' in summary[0] and node == 'Any Sends Failed?', f'ended at {node}')
    FAIL['search'] = True
    status, body, ex = r.run_campaign()
    node, error = r.stopped_at(ex)
    FAIL['search'] = False
    r.check('GoHighLevel search is down', 'retried 3 times, nobody texted, run ends as failed for the alert',
            len(r.new('ghl', 'POST', '/contacts/search')) == 3 and not sms(r.new()) and not tag_calls(r.new()) and node == 'Stop: Search Failed'
            and 'contact search failed, so nobody was texted: 500' in error and 'Internal server error' in error, f'ended at {node}')
    all_texts = [e for e in delivered(LOG) if e['body']['message'].startswith('Hi ')]
    per_lead = {}
    for e in all_texts:
        per_lead[e['body']['contactId']] = per_lead.get(e['body']['contactId'], 0) + 1
    r.check('Campaign, all runs', 'every eligible lead got exactly one campaign text; DND, quiet hours, excluded and active leads none',
            per_lead == {'c-jamie': 1, 'c-ivan': 1, 'c-jo': 1, 'c-kai': 1, 'c-tess': 1, 'c-sam': 1}, json.dumps(per_lead))

    # ---------------------------------------------------------------- replies
    jamie_conv_text = CAMPAIGN_TEXT.format(first='Jamie')
    status, body, ex = r.post('reply', inbound_payload('c-jamie', 'hello', 'msg-forged'), {'X-Reactivation-Secret': 'nope'})
    node, _ = r.stopped_at(ex)
    r.check('Reply webhook, wrong secret', 'refused with 401, nothing loaded or sent', status == 401 and not r.new() and node == 'Stop: Bad Reply Secret', f'{status}')
    status, body, ex = r.post('reply', inbound_payload('c-jamie', 'hello', 'msg-forged2'))
    node, _ = r.stopped_at(ex)
    r.check('Reply webhook, no secret', 'refused with 401', status == 401 and not r.new() and node == 'Stop: Bad Reply Secret', f'{status}')

    dup, status, ex = lead_says(r, 'c-jamie', "Yes please, I've been meaning to book")
    calls, slots = claude_calls(r), r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
    req = calls[0]['body'] if calls else {}
    r.check('Lead is interested', 'Claude request: model, forced tool, thinking off, business facts, history with the campaign text',
            status == 200 and len(calls) == 1 and req['model'] == 'claude-sonnet-5' and req['tool_choice'] == {'type': 'tool', 'name': 'handle_reply'}
            and req['thinking'] == {'type': 'disabled'} and 'Bright Smile Dental' in req['system'] and 'Free parking' in req['system']
            and jamie_conv_text in req['messages'][0]['content'][0]['text']
            and req['messages'][0]['content'][1]['text'] == "Newest message from the lead:\nYes please, I've been meaning to book", f'{len(calls)} call(s)')
    q = slots[0]['query'] if slots else {}
    r.check('Lead is interested', 'free slots asked for the next 7 days in the lead time zone (ms timestamps, Version 2021-04-15)',
            len(slots) == 1 and slots[0]['status'] == 200 and q.get('timezone') == IN_TZ and int(q['endDate']) - int(q['startDate']) == 7 * 86400000
            and slots[0]['headers'].get('version') == '2021-04-15', json.dumps(q))
    offered = []
    if slots:
        for day in sorted(slots[0]['response']):
            offered.append(slots[0]['response'][day]['slots'][0])
    offered = offered[:3]
    expected = 'Great to hear from you, Jamie! Here are a few open times:\n' + '\n'.join(f'{n + 1}) {label(s)}' for n, s in enumerate(offered)) + '\nReply 1, 2 or 3 to book.'
    reply = delivered(r.new())
    r.check('Lead is interested', 'SMS offers 3 real open times, one per day, then tags replied and interested',
            len(reply) == 1 and reply[0]['body']['message'] == expected and len(offered) == 3
            and tag_calls(r.new(), 'c-jamie')[0]['body'] == {'tags': [REPLIED, INTERESTED]}, reply[0]['body']['message'].replace('\n', ' | ') if reply else 'no SMS')

    _, status, ex = lead_says(r, 'c-jamie', '', duplicate_of=dup)
    node, _ = r.stopped_at(ex)
    r.check('Same reply delivered twice', 'second copy ignored: no Claude call, no SMS', not r.new('claude') and not sms(r.new()) and node == 'Route Reply', f'ended at {node}')

    lead_says(r, 'c-jamie', '2 works for me')
    calls = claude_calls(r)
    appts = [e for e in r.new('ghl', 'POST', '/calendars/events/appointments')]
    a = appts[0]['body'] if appts else {}
    start2 = datetime.fromisoformat(offered[1]) if len(offered) > 1 else None
    r.check('Lead picks a time', 'Claude sees the 3 offered times in the prompt',
            calls and all(f'{n + 1}) {label(s)}' in calls[0]['body']['system'] for n, s in enumerate(offered)), 'offered times in the system prompt')
    r.check('Lead picks a time', 'appointment booked for option 2: calendar, contact, start, end (+30 min, same offset), confirmed',
            len(appts) == 1 and appts[0]['status'] == 200 and a.get('calendarId') == CAL and a.get('locationId') == LOC and a.get('contactId') == 'c-jamie'
            and a.get('startTime') == offered[1] and a.get('endTime') == (start2 + timedelta(minutes=30)).isoformat()
            and a.get('appointmentStatus') == 'confirmed' and a.get('title') == 'Reactivation: Jamie Rivera', json.dumps(a)[:150])
    opp_search = r.new('ghl', 'GET', '/opportunities/search')
    created = r.new('ghl', 'POST', '/opportunities/')
    r.check('Lead picks a time', 'no opportunity yet, so one is created in the booked stage',
            len(opp_search) == 1 and opp_search[0]['query'] == {'location_id': LOC, 'contact_id': 'c-jamie', 'pipeline_id': PIPE, 'status': 'all', 'limit': '20'}
            and len(created) == 1 and created[0]['status'] == 201 and created[0]['body'] == {'pipelineId': PIPE, 'locationId': LOC, 'name': 'Jamie Rivera (reactivation)',
                                                                                          'pipelineStageId': STAGE_BOOKED, 'status': 'open', 'contactId': 'c-jamie'},
            json.dumps(created[0]['body']) if created else 'not created')
    reply, slack = delivered(r.new()), [e['body']['text'] for e in r.new('slack')]
    r.check('Lead picks a time', 'confirmation SMS, booked tag and a Slack alert with the time',
            len(reply) == 1 and reply[0]['body']['message'] == f'You are booked for {label(offered[1])}. See you then! If anything changes, just reply here.'
            and tag_calls(r.new(), 'c-jamie')[0]['body'] == {'tags': [REPLIED, BOOKED]} and len(slack) == 1 and 'Booked from reactivation' in slack[0]
            and label(offered[1]) in slack[0], reply[0]['body']['message'] if reply else 'no SMS')
    _, status, ex = lead_says(r, 'c-jamie', 'Thanks!')
    node, _ = r.stopped_at(ex)
    r.check('Booked lead writes again', 'the assistant stays quiet, the team takes over: no Claude call, no SMS, no tags',
            not r.new('claude') and not sms(r.new()) and not tag_calls(r.new()) and node == 'Route Reply', f'ended at {node}')
    _, status, ex = lead_says(r, 'c-jamie', 'Cancel')
    node, _ = r.stopped_at(ex)
    puts, slack = r.new('ghl', 'PUT', '/contacts/c-jamie'), [e['body']['text'] for e in r.new('slack')]
    r.check('Booked lead texts CANCEL', 'honored as an opt out (SMS DND, opted-out tag, no reply), and Slack says the appointment is still booked',
            not r.new('claude') and not sms(r.new()) and len(puts) == 1 and puts[0]['status'] == 200
            and CONTACTS['c-jamie']['dndSettings'].get('SMS', {}).get('status') == 'active'
            and tag_calls(r.new(), 'c-jamie')[0]['body'] == {'tags': [REPLIED, STOPPED]} and len(slack) == 1
            and slack[0].startswith('*Booked lead opted out of texts*: Jamie Rivera (+15550100008)')
            and f'Their appointment ({label(offered[1])}) is still on the calendar' in slack[0] and len(APPOINTMENTS) == 1
            and node == 'Fail Run?' and ex['status'] == 'success', slack[0].splitlines()[1][:90] if slack else 'no alert')

    FAIL['flat_messages'].add(CONVERSATIONS['c-ivan'])
    lead_says(r, 'c-ivan', 'Do you take PPO insurance?')
    calls, reply, slack = claude_calls(r), delivered(r.new()), [e['body']['text'] for e in r.new('slack')]
    r.check('Question Claude cannot answer', 'reply sent, needs-human tag, Slack alert (history read from the flat response shape)',
            calls and CAMPAIGN_TEXT.format(first='Ivan') in calls[0]['body']['messages'][0]['content'][0]['text']
            and len(reply) == 1 and reply[0]['body']['message'] == SCRIPT['Do you take PPO insurance?']['reply']
            and tag_calls(r.new(), 'c-ivan')[0]['body'] == {'tags': [REPLIED, HUMAN]} and len(slack) == 1 and 'needs a person' in slack[0]
            and 'PPO insurance' in slack[0], slack[0].splitlines()[0] if slack else 'no alert')

    FAIL['sms_429_once'].add('c-jo')
    dup, status, ex = lead_says(r, 'c-jo', 'Not right now, maybe in the spring', shape='workflow')
    conv_search = r.new('ghl', 'GET', '/conversations/search')
    reply = sms(r.new())
    r.check('Standard Webhook action payload', 'secret from customData accepted, conversation looked up by contact, not now reply and tag',
            status == 200 and len(conv_search) == 1 and conv_search[0]['query'] == {'locationId': LOC, 'contactId': 'c-jo', 'limit': '1'}
            and [e['status'] for e in reply] == [429, 200] and reply[-1]['body']['message'] == SCRIPT['Not right now, maybe in the spring']['reply']
            and tag_calls(r.new(), 'c-jo')[0]['body'] == {'tags': [REPLIED, NOT_NOW]}, f"SMS statuses {[e['status'] for e in reply]}")
    _, status, ex = lead_says(r, 'c-jo', '', duplicate_of=dup)
    node, _ = r.stopped_at(ex)
    r.check('Standard payload delivered twice', 'no message id in the payload, the id from the history still stops the repeat',
            not r.new('claude') and not sms(r.new()) and node == 'Route Reply', f'ended at {node}')

    lead_says(r, 'c-kai', 'STOP')
    puts = r.new('ghl', 'PUT', '/contacts/c-kai')
    r.check('Lead texts STOP', 'no Claude call, no reply; SMS DND set and the existing Email DND kept',
            not r.new('claude') and not sms(r.new()) and len(puts) == 1 and puts[0]['status'] == 200
            and puts[0]['body'] == {'dndSettings': {'Email': {'status': 'active', 'message': 'asked by email'},
                                                    'SMS': {'status': 'active', 'message': 'Opted out by text reply to the reactivation campaign'}}}
            and CONTACTS['c-kai']['dndSettings']['Email']['status'] == 'active', json.dumps(puts[0]['body']) if puts else 'no DND update')
    r.check('Lead texts STOP', 'tagged replied and opted-out', tag_calls(r.new(), 'c-kai') and tag_calls(r.new(), 'c-kai')[0]['body'] == {'tags': [REPLIED, STOPPED]}, '')
    _, status, ex = lead_says(r, 'c-kai', 'hello?')
    node, _ = r.stopped_at(ex)
    r.check('Opted out lead writes again', 'nothing is ever sent again: no Claude call, no SMS, no tags',
            not r.new('claude') and not sms(r.new()) and not tag_calls(r.new()) and node == 'Route Reply', f'ended at {node}')

    lead_says(r, 'c-sam', "please don't text me anymore")
    r.check('Opt out in plain words', 'Claude reads it as stop: SMS DND set, opted-out tag, no reply',
            len(claude_calls(r)) == 1 and not sms(r.new()) and len(r.new('ghl', 'PUT', '/contacts/c-sam')) == 1
            and CONTACTS['c-sam']['dndSettings'].get('SMS', {}).get('status') == 'active'
            and tag_calls(r.new(), 'c-sam') and tag_calls(r.new(), 'c-sam')[0]['body'] == {'tags': [REPLIED, STOPPED]}, '')

    _, status, ex = lead_says(r, 'c-pat', 'Can I get a price for whitening?')
    node, error = r.stopped_at(ex)
    reply = delivered(r.new())
    r.check('Claude is down (529)', 'retried 3 times, holding reply sent, needs-human tag, run flagged for the alert',
            len(r.new('claude')) == 3 and len(reply) == 1 and reply[0]['body']['message'] == HOLD_TEXT.format(first='Pat')
            and tag_calls(r.new(), 'c-pat')[0]['body'] == {'tags': [REPLIED, HUMAN]} and node == 'Stop: Reply Needs Attention'
            and 'Claude did not give a usable answer for Pat Lee (+15550100015). They got a holding reply.' in error
            and 'They wrote "Can I get a price for whitening?"' in error, f"{len(r.new('claude'))} attempts, ended at {node}")
    _, status, ex = lead_says(r, 'c-kim', 'Please stop sending these')
    node, error = r.stopped_at(ex)
    r.check('Claude is down and the message may be an opt out', 'no holding reply to someone who may have asked to stop: nothing sent, needs-human tag, run flagged with their words',
            len(r.new('claude')) == 3 and not sms(r.new()) and tag_calls(r.new(), 'c-kim')[0]['body'] == {'tags': [REPLIED, HUMAN]}
            and node == 'Stop: Reply Needs Attention' and 'Nothing was sent, because the message may ask us to stop texting.' in error
            and 'They wrote "Please stop sending these"' in error, f'ended at {node}')
    _, status, ex = lead_says(r, 'c-wes', 'hmm ok??')
    node, error = r.stopped_at(ex)
    reply = delivered(r.new())
    r.check('Claude answers without the tool call', 'treated as a failure: holding reply, needs-human tag, run flagged',
            len(r.new('claude')) == 1 and len(reply) == 1 and reply[0]['body']['message'] == HOLD_TEXT.format(first='Wes')
            and node == 'Stop: Reply Needs Attention' and 'no usable decision' in error, f'ended at {node}')

    lead_says(r, 'c-quinn', "Wrong number, I don't know any Quinn")
    reply = delivered(r.new())
    r.check('Wrong person', 'apology sent, wrong-number tag',
            len(reply) == 1 and reply[0]['body']['message'] == SCRIPT["Wrong number, I don't know any Quinn"]['reply']
            and tag_calls(r.new(), 'c-quinn')[0]['body'] == {'tags': [REPLIED, WRONG]}, '')
    _, status, ex = lead_says(r, 'c-quinn', 'hello')
    node, _ = r.stopped_at(ex)
    r.check('Wrong person writes again', 'the assistant stays quiet: no Claude call, no SMS, no tags',
            not r.new('claude') and not sms(r.new()) and not tag_calls(r.new()) and node == 'Route Reply', f'ended at {node}')

    lead_says(r, 'c-rae', 'yes')
    rae_slots = r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
    first_option = sorted(rae_slots[0]['response'].items())[0][1]['slots'][0] if rae_slots else ''
    if first_option:
        TAKEN.add(datetime.fromisoformat(first_option).astimezone(timezone.utc))   # someone else books it in the meantime
    _, status, ex = lead_says(r, 'c-rae', '1')
    node, error = r.stopped_at(ex)
    reply = delivered(r.new())
    r.check('Picked time was just taken', 'booking refused by GoHighLevel: honest SMS, needs-human tag, no opportunity change, run flagged',
            len(r.new('ghl', 'POST', '/calendars/events/appointments')) == 1 and len(reply) == 1
            and reply[0]['body']['message'] == 'Sorry Rae, I could not lock in that time. A team member will text you shortly to confirm one.'
            and not r.new('ghl', path='/opportunities/.*') and HUMAN in CONTACTS['c-rae']['tags'] and node == 'Stop: Reply Needs Attention'
            and 'Booking failed' in error, f'ended at {node}')

    lead_says(r, 'c-sol', 'sure, why not')
    lead_says(r, 'c-sol', 'the first one')
    puts, created = r.new('ghl', 'PUT', '/opportunities/.*'), r.new('ghl', 'POST', '/opportunities/')
    r.check('Existing opportunity', 'the lost opportunity is reopened and moved to the booked stage, no duplicate created',
            len(puts) == 1 and puts[0]['path'] == '/opportunities/opp-sol-old' and puts[0]['body'] == {'pipelineId': PIPE, 'pipelineStageId': STAGE_BOOKED, 'status': 'open'}
            and not created and OPPORTUNITIES['opp-sol-old']['status'] == 'open'
            and [o['id'] for o in OPPORTUNITIES.values() if o['contactId'] == 'c-sol'] == ['opp-sol-old'], json.dumps(puts[0]['body']) if puts else 'no update')

    lead_says(r, 'c-fay', 'yes')
    won_before = copy.deepcopy(OPPORTUNITIES['opp-fay-won'])
    _, status, ex = lead_says(r, 'c-fay', '1')
    node, error = r.stopped_at(ex)
    created, reply = r.new('ghl', 'POST', '/opportunities/'), delivered(r.new())
    r.check('Old opportunity is a past sale (won)', 'never reopened: a new one is tried, GoHighLevel refuses the duplicate, the lead is still confirmed, run flagged',
            not r.new('ghl', 'PUT', '/opportunities/.*') and len(created) == 1 and created[0]['status'] == 400
            and OPPORTUNITIES['opp-fay-won'] == won_before and len(r.new('ghl', 'POST', '/calendars/events/appointments')) == 1
            and len(reply) == 1 and reply[0]['body']['message'].startswith('You are booked for ') and BOOKED in CONTACTS['c-fay']['tags']
            and node == 'Stop: Reply Needs Attention' and 'opportunity was not saved in the booked stage' in error and 'Fay Dunn' in error,
            f'ended at {node}')

    # Gil was offered 3 times, then answers after the first one has passed. "2" must still mean the second time in
    # the text he got, not the second of the times that are left.
    lead_says(r, 'c-gil', 'yes')
    gil = offered_to(r, 'c-gil')
    edit_static(r, lambda g: first_time_passed(g, 'c-gil'))
    _, status, ex = lead_says(r, 'c-gil', '2 works for me')
    node, _ = r.stopped_at(ex)
    calls, appts = claude_calls(r), r.new('ghl', 'POST', '/calendars/events/appointments')
    system = calls[0]['body']['system'] if calls else ''
    r.check('An offered time has passed', 'the numbers stay as the lead saw them: Claude sees 1) marked as passed, 2) and 3) unchanged',
            len(gil) == 3 and f'1) {gil[0]["label"]} (has passed, no longer available)\n2) {gil[1]["label"]}\n3) {gil[2]["label"]}' in system,
            'offered times in the system prompt')
    r.check('An offered time has passed', '"2" books the second time from his text (not the third), with its exact start and end',
            len(gil) == 3 and len(appts) == 1 and appts[0]['status'] == 200 and appts[0]['body']['startTime'] == gil[1]['start']
            and appts[0]['body']['endTime'] == gil[1]['end'] and BOOKED in CONTACTS['c-gil']['tags'] and node == 'Fail Run?',
            appts[0]['body']['startTime'] if appts else 'no booking')

    # Hal picks the time that has passed: nothing is booked and he gets new times.
    lead_says(r, 'c-hal', 'yes')
    hal = offered_to(r, 'c-hal')
    edit_static(r, lambda g: first_time_passed(g, 'c-hal'))
    _, status, ex = lead_says(r, 'c-hal', 'the first one')
    node, _ = r.stopped_at(ex)
    reply, slots = delivered(r.new()), r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')
    new_times = offered_to(r, 'c-hal')
    expected = ('Sorry, that time has already passed. Here are the next open times:\n'
                + '\n'.join(f'{n + 1}) {t["label"]}' for n, t in enumerate(new_times)) + '\nReply 1, 2 or 3 to book.')
    r.check('Lead picks a time that has passed', 'nothing booked; the lead is told and gets 3 new times they have not seen yet',
            len(hal) == 3 and not r.new('ghl', 'POST', '/calendars/events/appointments') and len(slots) == 1 and len(new_times) == 3
            and not {t['start'] for t in new_times} & {t['start'] for t in hal} and len(reply) == 1 and reply[0]['body']['message'] == expected
            and tag_calls(r.new(), 'c-hal')[0]['body'] == {'tags': [REPLIED, INTERESTED]} and node == 'Fail Run?',
            reply[0]['body']['message'].splitlines()[0] if reply else 'no SMS')

    (uma, _), status, ex = lead_says(r, 'c-uma', 'Hi, is this Bright Smile?')
    uma_message = uma['messageId']
    node, _ = r.stopped_at(ex)
    r.check('Reply from a contact outside the campaign', 'ignored: no Claude call, no SMS, no tags',
            not r.new('claude') and not sms(r.new()) and not tag_calls(r.new()) and node == 'Route Reply', f'ended at {node}')
    email = inbound_payload('c-tia', 'Sent from my email', 'msg-email-1')
    email['messageType'] = 'Email'
    status, body, ex = r.post('reply', email, SECRET_HEADER)
    node, _ = r.stopped_at(ex)
    r.check('Email reply', 'not an SMS: ignored before any GoHighLevel call', not r.new() and node == 'Valid Reply?', f'ended at {node}')
    other = inbound_payload('c-tia', 'hello from elsewhere', 'msg-other-1')
    other['locationId'] = 'loc-somebody-else'
    status, body, ex = r.post('reply', other, SECRET_HEADER)
    node, _ = r.stopped_at(ex)
    r.check('Reply for another location', 'ignored before any GoHighLevel call', not r.new() and node == 'Valid Reply?', f'ended at {node}')

    lead_says(r, 'c-tia', 'What are your hours?')
    lead_says(r, 'c-tia', 'Do you have parking?')
    lead_says(r, 'c-tia', 'Ok and one more thing <!here>')
    slack = [e['body']['text'] for e in r.new('slack')]
    r.check('Reply limit (MAX_AI_REPLIES=2)', 'third message goes to a person: no Claude call, no SMS, needs-human tag, Slack alert',
            not r.new('claude') and not sms(r.new()) and tag_calls(r.new(), 'c-tia')[0]['body'] == {'tags': [REPLIED, HUMAN]}
            and len(slack) == 1 and 'stopped replying' in slack[0], slack[0].splitlines()[2] if slack else 'no alert')
    r.check('Reply limit (MAX_AI_REPLIES=2)', "the lead's words are escaped for Slack, so a text with <!here> cannot ping the channel",
            len(slack) == 1 and 'They said: "Ok and one more thing &lt;!here&gt;"' in slack[0] and '<!' not in slack[0],
            slack[0].splitlines()[1] if slack else 'no alert')

    # ---------------------------------------------------------------- reply failures
    ghost = inbound_payload('c-jamie', 'Is this still available? <!channel>', 'msg-ghost-1')
    ghost.update({'contactId': 'c-ghost', 'conversationId': 'conv-c-ghost'})
    status, body, ex = r.post('reply', ghost, SECRET_HEADER)
    node, error = r.stopped_at(ex)
    r.check('Contact cannot be loaded', 'retried 3 times, run ends as failed with the contact id and their message (escaped for Slack), nothing sent',
            len(r.new('ghl', 'GET', '/contacts/c-ghost')) == 3 and not r.new('claude') and not sms(r.new()) and node == 'Stop: Lead Lookup Failed'
            and 'c-ghost' in error and 'after they replied "Is this still available? &lt;!channel&gt;"' in error and '<!' not in error,
            f'ended at {node}')

    FAIL['messages'].add(CONVERSATIONS['c-ida'])
    _, status, ex = lead_says(r, 'c-ida', 'Do you have parking?')
    FAIL['messages'].discard(CONVERSATIONS['c-ida'])
    node, error = r.stopped_at(ex)
    calls, reply = claude_calls(r), delivered(r.new())
    r.check('Conversation history cannot be loaded', 'retried 3 times, the lead still gets an answer, and the run is flagged (no silent loss of context)',
            len(r.new('ghl', 'GET', '/conversations/conv-c-ida/messages')) == 3 and len(calls) == 1
            and calls[0]['body']['messages'][0]['content'][0]['text'] == 'Conversation so far, oldest first:\n(no earlier messages found)'
            and len(reply) == 1 and reply[0]['body']['message'] == SCRIPT['Do you have parking?']['reply']
            and node == 'Stop: Reply Needs Attention' and 'Handled the reply from Ida Lund (+15550100033) without the conversation history' in error
            and 'conversations/message.readonly' in error, f'ended at {node}')

    FAIL['slots'] = True
    _, status, ex = lead_says(r, 'c-ana', 'yes')
    FAIL['slots'] = False
    node, error = r.stopped_at(ex)
    reply = delivered(r.new())
    r.check('Calendar is down', 'retried 3 times, honest holding SMS, interested and needs-human tags, run flagged',
            len(r.new('ghl', 'GET', r'/calendars/[^/]+/free-slots')) == 3 and len(reply) == 1
            and reply[0]['body']['message'] == 'Thanks Ana! I am checking the calendar, and a team member will text you a few open times shortly.'
            and tag_calls(r.new(), 'c-ana')[0]['body'] == {'tags': [REPLIED, INTERESTED, HUMAN]} and node == 'Stop: Reply Needs Attention'
            and 'Could not load free slots' in error, f'ended at {node}')
    FAIL['no_slots'] = True
    _, status, ex = lead_says(r, 'c-ben', 'sure, why not')
    FAIL['no_slots'] = False
    node, _ = r.stopped_at(ex)
    reply, slack = delivered(r.new()), [e['body']['text'] for e in r.new('slack')]
    r.check('No open times in the calendar', 'same holding SMS and a Slack alert for the team, but not a failed run',
            len(reply) == 1 and reply[0]['body']['message'].startswith('Thanks Ben! I am checking the calendar')
            and HUMAN in CONTACTS['c-ben']['tags'] and len(slack) == 1 and 'no open times' in slack[0] and node == 'Fail Run?', f'ended at {node}')

    lead_says(r, 'c-cy', 'yes')
    FAIL['opportunity_create'] = True
    _, status, ex = lead_says(r, 'c-cy', '1')
    FAIL['opportunity_create'] = False
    node, error = r.stopped_at(ex)
    reply = delivered(r.new())
    r.check('Opportunity update fails after booking', 'the lead still gets the confirmation and the booked tag; the run is flagged',
            len(r.new('ghl', 'POST', '/calendars/events/appointments')) == 1 and len(r.new('ghl', 'POST', '/opportunities/')) == 1
            and len(reply) == 1 and reply[0]['body']['message'].startswith('You are booked for ') and BOOKED in CONTACTS['c-cy']['tags']
            and node == 'Stop: Reply Needs Attention' and 'opportunity was not saved in the booked stage' in error, f'ended at {node}')

    FAIL['put_contact'].add('c-dee')
    _, status, ex = lead_says(r, 'c-dee', 'Stop')
    FAIL['put_contact'].discard('c-dee')
    r.ids['dnd_failed'] = ex['id']
    node, error = r.stopped_at(ex)
    r.check('DND update fails after STOP', 'retried 3 times, nothing texted, urgent alert to set DND by hand',
            len(r.new('ghl', 'PUT', '/contacts/c-dee')) == 3 and not sms(r.new()) and node == 'Stop: Opt Out Failed' and 'URGENT' in error
            and 'Dee Ross' in error, f'ended at {node}')

    FAIL['sms'].add('c-eve')
    dup, status, ex = lead_says(r, 'c-eve', 'What are your hours?')
    FAIL['sms'].discard('c-eve')
    node, error = r.stopped_at(ex)
    r.check('Reply SMS rejected', 'retried 3 times, run flagged with the text to send by hand',
            len(sms(r.new())) == 3 and node == 'Stop: Reply Needs Attention' and 'was NOT sent' in error
            and SCRIPT['What are your hours?']['reply'] in error, f'ended at {node}')
    # The run above failed, and Eve has no tag that would stop the assistant. Only the saved message id can stop this repeat.
    _, status, ex = lead_says(r, 'c-eve', '', duplicate_of=dup)
    node, _ = r.stopped_at(ex)
    r.check('Repeat delivery after a failed run', 'the message id was saved even though the run failed: no second Claude call or SMS',
            not r.new('claude') and not sms(r.new()) and node == 'Route Reply' and ex['status'] == 'success', f'ended at {node}')
    # Two days later Eve sends the same words again, through the standard Webhook action (no message id), and the
    # webhook arrives before GoHighLevel shows the new message in the history. The old message with the same words
    # must not make this look like a repeat delivery.
    for m in MESSAGES[CONVERSATIONS['c-eve']]:
        if m['direction'] == 'inbound':
            m['dateAdded'] = iso(datetime.now(timezone.utc) - timedelta(days=2))
    _, status, ex = lead_says(r, 'c-eve', 'What are your hours?', shape='workflow', stored=False)
    node, _ = r.stopped_at(ex)
    reply = delivered(r.new())
    r.check('Same words again two days later', 'answered as a new message, not ignored as a repeat: one Claude call, one SMS',
            len(claude_calls(r)) == 1 and len(reply) == 1 and reply[0]['body']['message'] == SCRIPT['What are your hours?']['reply']
            and node == 'Fail Run?' and ex['status'] == 'success', f'ended at {node}')

    static = (api('GET', f'/api/v1/workflows/{r.wid}').get('staticData') or {}).get('global', {})
    leads = static.get('leads', {})
    r.check('Workflow memory', 'only reply state is kept (booked, offered times, reply counts); campaign runs and ignored messages write nothing',
            'sent' not in static and bool(leads.get('c-jamie', {}).get('bookedAt')) and leads.get('c-tia', {}).get('replies') == 2
            and len(leads.get('c-sol', {}).get('offeredBefore', [])) == 3 and uma_message not in static.get('seen', {}) and 'c-uma' not in leads,
            f"{len(leads)} leads, {len(static.get('seen', {}))} message ids")

    # ---------------------------------------------------------------- schedule, switched off
    end = time.time() + 75
    while time.time() < end:
        timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
        if timed:
            break
        time.sleep(2)
    ends = [r.stopped_at(e) for e in timed]
    r.check('Schedule with SCHEDULE_ENABLED=false', 'scheduled runs stop at "Schedule On?" and do nothing',
            timed and all(n == 'Schedule On?' for n, _ in ends) and all(e['status'] == 'success' for e in timed), f'{len(timed)} scheduled run(s)')

    # ---------------------------------------------------------------- error alert workflow (imported and active)
    failed = sorted((e['id'] for e in r.executions() if e.get('status') == 'error'), key=int)
    linked, got = alerts_for(failed)
    counts = [len(v) for v in linked.values()]
    r.check('Error alert workflow', 'every failed run raised exactly one Slack alert with its execution link, and no other alerts',
            len(failed) == EXPECTED_FAILED_RUNS and counts == [1] * len(failed) and len(got) == len(failed), f'{len(failed)} failed runs, {len(got)} alerts')
    sends = (linked.get(r.ids.get('sends_failed')) or [{'body': {'text': ''}}])[0]['body']['text']
    r.check('Error alert workflow', 'the alert carries the node and the whole reason (who failed and why)',
            f'*{TEMP_NAME}* failed at *Stop: Sends Failed*' in sends and 'Kai Noor (tag failed, so no SMS was sent: ' in sends
            and 'Nia Brooks (SMS not sent: ' in sends, sends.splitlines()[0] if sends else 'no alert')
    dnd = (linked.get(r.ids.get('dnd_failed')) or [{'body': {'text': ''}}])[0]['body']['text']
    r.check('Error alert workflow', 'an opt out GoHighLevel did not save raises an URGENT alert with the name and phone',
            'failed at *Stop: Opt Out Failed*' in dnd and 'URGENT: Dee Ross (+15550100026) asked to stop texts' in dnd
            and 'Set DND for SMS by hand now' in dnd, dnd.splitlines()[1][:90] if dnd.count('\n') else 'no alert')


def run_schedule(results, error_wid):
    """A second copy with the schedule on: a new lead added to GoHighLevel gets texted without anyone pressing Run Now.
    Its BUSINESS_TZ is a zone where it is 3 AM, so two new leads without a usable time zone of their own must wait."""
    add_contact('c-vera', 'Vera', 'Lane', '+15550100022', IN_TZ, ['reactivate'], 2)
    add_contact('c-wren', 'Wren', 'Holt', '+15550100029', None, ['reactivate'], 2)              # no time zone
    add_contact('c-yuri', 'Yuri', 'Sato', '+15550100030', 'Mars/Olympus', ['reactivate'], 2)    # invalid time zone
    mark = len(LOG)
    wid = import_active({**ENV, 'SCHEDULE_ENABLED': 'true', 'BUSINESS_TZ': OUT_TZ}, '-2', error_wid)
    r = Runner(wid, '-2')
    try:
        end = time.time() + 150
        timed = []
        while time.time() < end:
            timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
            if timed:
                break
            time.sleep(2)
        node, _ = r.stopped_at(timed[0]) if timed else ('none', '')
        vera = [e for e in delivered(LOG[mark:]) if e['body']['contactId'] == 'c-vera']
        others = [e for e in delivered(LOG[mark:]) if e['body']['contactId'] != 'c-vera']
        results.append(('Schedule with SCHEDULE_ENABLED=true', 'the scheduled run texts the new lead once and nobody else',
                        len(timed) >= 1 and len(vera) == 1 and not others and SENT in CONTACTS['c-vera']['tags'] and node == 'Any Sends Failed?',
                        f'{len(timed)} scheduled run(s), ended at {node}'))
        summary = [e['body']['text'] for e in LOG[mark:] if e['service'] == 'slack']
        results.append(('Schedule with SCHEDULE_ENABLED=true', 'leads with no or an invalid time zone use BUSINESS_TZ (3 AM there): skipped as quiet hours, not texted',
                        bool(summary) and summary[0].startswith('*Reactivation run* (schedule)') and 'Texted: 1 (Vera Lane)' in summary[0]
                        and '3 quiet hours' in summary[0] and not tag_calls(LOG[mark:], 'c-wren') and not tag_calls(LOG[mark:], 'c-yuri'),
                        summary[0].splitlines()[2] if summary and summary[0].count('\n') >= 2 else 'no summary'))
    finally:
        remove(wid)


def run_bad_settings(results, error_wid):
    """A third copy with broken settings: Run Now must refuse with a 500 that names every problem, before any API call."""
    env = {**ENV, 'BUSINESS_TZ': 'Mars/Olympus', 'REPLY_WEBHOOK_SECRET': '', 'GHL_CALENDAR_ID': '', 'ANTHROPIC_MODEL': 'claude-opus-5-5'}
    wid = import_active(env, '-3', error_wid)
    r = Runner(wid, '-3')
    try:
        status, body, ex = r.run_campaign()
        node, _ = r.stopped_at(ex)
        try:
            answer = json.loads(body)
        except ValueError:
            answer = body
        problems = ['BUSINESS_TZ "Mars/Olympus" is not a valid time zone', 'REPLY_WEBHOOK_SECRET is empty, so replies could not be handled',
                    'GHL_CALENDAR_ID is empty, so replies could not be handled',
                    'ANTHROPIC_MODEL claude-opus-5-5 does not accept the forced tool call with thinking off that replies use, '
                    'so replies could not be handled (use claude-sonnet-5, claude-opus-5 or claude-haiku-4-5)']
        results.append(('Run Now with broken settings', 'answered 500 with the exact list of problems, no GoHighLevel call, run ends as failed',
                        status == 500 and answer == {'started': False, 'error': 'settings', 'problems': problems} and not r.new()
                        and node == 'Stop: Bad Settings' and ex['status'] == 'error', f'{status}, ended at {node}'))
        linked, _ = alerts_for([ex['id']])
        text = (linked[ex['id']] or [{'body': {'text': ''}}])[0]['body']['text']
        results.append(('Run Now with broken settings', 'the error alert names every problem',
                        len(linked[ex['id']]) == 1 and 'failed at *Stop: Bad Settings*' in text
                        and 'Campaign settings are not valid, so nobody was texted. ' + '. '.join(problems) + '.' in text,
                        text.splitlines()[1][:90] if text.count('\n') else 'no alert'))
    finally:
        remove(wid)


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    leftovers = remove_leftovers()
    if leftovers:
        print(f'Removed {leftovers} temporary workflow(s) left by an interrupted run.')
    seed()
    server = MockServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    try:
        error_wid = import_error_workflow()
        try:
            wid = import_active(ENV, '', error_wid)
            r = Runner(wid)
            try:
                run(r)
            finally:
                results = r.results
                remove(wid)
            run_bad_settings(results, error_wid)
            run_schedule(results, error_wid)
        finally:
            remove(error_wid)
    finally:
        server.shutdown()
        if os.environ.get('SAVE_REQUESTS'):
            saved = [{k: e[k] for k in ('service', 'method', 'path', 'query', 'body', 'status')} for e in LOG]
            Path(os.environ['SAVE_REQUESTS']).write_text(json.dumps(saved, indent=1), encoding='utf-8')

    passed = sum(ok for *_, ok, _ in results)
    last = None
    for scenario, label_text, ok, detail in results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label_text}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(results)} checks passed')
    if CONNECTION_ERRORS:
        # A request that breaks between n8n and the mock never reaches the handler, so an exact request count can be off by one.
        print(f'Note: {len(CONNECTION_ERRORS)} connection(s) to the mock broke before a response: ' + '; '.join(CONNECTION_ERRORS[:5]))
    sys.exit(0 if results and passed == len(results) else 1)


if __name__ == '__main__':
    main()
