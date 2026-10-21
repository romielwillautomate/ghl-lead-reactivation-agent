# Setup: GoHighLevel, Claude, Slack and n8n

You need self-hosted n8n reachable over HTTPS, a GoHighLevel sub-account with a phone number that can send SMS, a calendar and a pipeline, an Anthropic API key and, optionally, a Slack incoming webhook. Plan about 30 minutes.

## 1. GoHighLevel

### Private Integration token

1. Go to **Settings → Private Integrations → Create new integration**.
2. Give it these scopes:

   | Scope | Used for |
   |---|---|
   | `contacts.readonly` | Searching for dormant leads and loading a contact |
   | `contacts.write` | Adding tags and setting DND after an opt out |
   | `conversations.readonly` | Finding the lead's conversation |
   | `conversations/message.readonly` | Reading the last messages for Claude |
   | `conversations/message.write` | Sending the SMS |
   | `calendars.readonly` | Free slots |
   | `calendars/events.write` | Booking the appointment |
   | `opportunities.readonly` | Finding the lead's opportunity |
   | `opportunities.write` | Moving or creating the opportunity |

3. Copy the token (it starts with `pit-`) into `GHL_API_KEY`.

### IDs

- **Location ID** (`GHL_LOCATION_ID`): **Settings → Business Profile**.
- **Calendar ID** (`GHL_CALENDAR_ID`): open the calendar's settings; the ID is in the page URL. Or list the calendars with your token (calendar calls use `Version: 2021-04-15`) and copy the `id`:

  ```bash
  curl "https://services.leadconnectorhq.com/calendars/?locationId=YOUR_LOCATION_ID" \
    -H "Authorization: Bearer pit-your-token" -H "Version: 2021-04-15"
  ```

  The workflow books with `toNotify` on, so the calendar's own notifications and automations run as for any booking. If the calendar sends its own SMS confirmation, leads get that one and the assistant's; turn the calendar's SMS confirmation off if one is enough. Use a calendar that assigns the team member by itself (a personal calendar or round robin), because the workflow does not pick one.

- **Pipeline and stage IDs** (`GHL_PIPELINE_ID`, `GHL_BOOKED_STAGE_ID`): the quickest way is one API call with your token:

  ```bash
  curl "https://services.leadconnectorhq.com/opportunities/pipelines?locationId=YOUR_LOCATION_ID" \
    -H "Authorization: Bearer pit-your-token" -H "Version: 2021-07-28"
  ```

  Pick your pipeline's `id` and the `id` of the stage a booked lead should land in (for example "Appointment booked").

  When a lead books, the workflow moves their open opportunity in this pipeline, or else a lost or abandoned one, to that stage. A won opportunity is a past sale and is never reopened; the workflow creates a new opportunity instead. That only works if **Allow Multiple Opportunities per Contact** is turned on for the sub-account (**Settings → Objects → Opportunities**). If it is off, GoHighLevel refuses the new opportunity, the lead is still booked and confirmed, and the alert asks you to add the opportunity by hand.

### Pick the leads

Add the tag `reactivate` (or whatever you set in `REACTIVATION_TAG`) to the old leads you want to wake up. A bulk action on a smart list is the fastest way. With `DORMANT_DAYS` above 0, leads with no activity for that many days are included even without the tag. Contacts with no recorded activity at all are only included when tagged, so an imported cold list is never texted by accident.

Only text people who gave you permission to text them. In the US, sending through a GoHighLevel number also needs A2P 10DLC registration.

## 2. Anthropic

Create a key at **console.anthropic.com → API keys** and put it in `ANTHROPIC_API_KEY`. The default model is `claude-sonnet-5`. `claude-haiku-4-5` and `claude-opus-5` also work. The reply request forces a tool call with thinking turned off, so the Claude Fable and Mythos models and `claude-opus-5-5` do not work: `claude-opus-5-5` and `claude-fable-5-1` refuse the forced tool call, and the Fable and Mythos models and `claude-opus-5-5` refuse thinking turned off. A campaign run refuses to start with one of them and names the problem.

## 3. n8n

1. Copy `.env.example` into your n8n environment, fill in the values and restart n8n. The nodes read them with `$env`, so self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. (n8n Cloud does not allow `$env`. There, replace `$env.` with `$vars.` and create the same names under Variables.)
2. Import `workflow/error-alert-slack.json` and **activate it**. n8n 2.x only runs an error workflow that is active; an inactive one is skipped without any alert.
3. Import `workflow/ghl-lead-reactivation-agent.json`. In its settings, choose the error alert workflow as its **Error Workflow**.
4. Activate the reactivation workflow.

It now listens on two production URLs:

- `https://YOUR-N8N-DOMAIN/webhook/ghl-reactivation-run` starts a campaign run
- `https://YOUR-N8N-DOMAIN/webhook/ghl-reactivation-reply` receives replies

## 4. Forward replies from GoHighLevel

Create a GoHighLevel workflow (**Automation → Workflows → Create workflow**):

1. **Trigger: Customer Replied.** Set **Reply channel** to SMS. Adding the filter **Has tag `reactivation-sent`** is optional (the n8n workflow ignores everyone else anyway) but saves calls.
2. **Action: Custom Webhook** (recommended, because it can send a header):
   - Method `POST`, URL `https://YOUR-N8N-DOMAIN/webhook/ghl-reactivation-reply`
   - Header `X-Reactivation-Secret` with the value of `REPLY_WEBHOOK_SECRET`
   - Content type `application/json`, with these body fields:

     | Key | Value |
     |---|---|
     | `type` | `InboundMessage` |
     | `locationId` | `{{location.id}}` |
     | `contactId` | `{{contact.id}}` |
     | `body` | `{{message.body}}` |
     | `messageType` | `SMS` |
     | `direction` | `inbound` |

     Key and value fields let GoHighLevel build the JSON itself. If you type a raw JSON body instead, send yourself a test reply with a double quote in it and check that it still arrives.

   Custom Webhook is one of GoHighLevel's LC Premium actions: each sub-account gets a number of free executions, after that it is billed per execution.

   **Or the standard Webhook action**, which is free but cannot set headers: point it at the same URL and add one Custom Data item with the key `secret` and your `REPLY_WEBHOOK_SECRET` as the value. Its payload already carries `contact_id` and `message.body`, and the secret arrives as `customData.secret`.
3. Publish the workflow.

Either way, treat `REPLY_WEBHOOK_SECRET` like a password: anyone who has it can make the assistant text your leads. If it leaks, change it in n8n and in the GoHighLevel workflow.

The n8n webhook answers at once and does the work afterwards, so GoHighLevel never waits. Payloads in GoHighLevel's `InboundMessage` webhook format are read too and include the message id. A marketplace app does not send your shared secret, though, so such payloads need to come through your own relay that adds the `X-Reactivation-Secret` header.

Turn off anything else that auto-replies to these leads, such as a Conversation AI bot, or they will get two answers.

## 5. First run

Start with a dry run. It searches and checks everything but sends nothing:

```bash
curl -X POST "https://YOUR-N8N-DOMAIN/webhook/ghl-reactivation-run" \
  -H "X-Run-Secret: YOUR_RUN_NOW_SECRET" -H "Content-Type: application/json" \
  -d '{"dryRun": true}'
```

You get `{"started":true,"dryRun":true}` right away, and Slack shows who would be texted and why others were skipped. The dry run works from the search results; a real run reloads each lead just before texting, so it can skip a few more (for example a lead tagged a moment ago). Then:

1. Tag your own contact with `reactivate`, set `BATCH_LIMIT=1` and send the same request without `dryRun`.
2. Reply to the text from your phone with "yes". You should get three open times. Reply "2" and the appointment appears in the calendar, the opportunity moves to the booked stage and Slack gets a booking alert.
3. Reply "STOP" from a second test contact and check that its SMS DND is on.

When everything looks right, set `BATCH_LIMIT` back, and set `SCHEDULE_ENABLED=true` if you want hourly runs. The schedule runs at five past every hour; change it in the **Schedule: Every Hour** node. Quiet hours are enforced per lead either way, so a run at 3 AM simply skips everyone who is asleep.

Run one campaign at a time. A run takes about `BATCH_LIMIT` x `SEND_DELAY_SECONDS` (about 7 minutes with the defaults), and two runs that overlap could, in a rare race, text the same lead twice. So either use the schedule or start runs by hand, not both at once, and keep `BATCH_LIMIT` x `SEND_DELAY_SECONDS` well under an hour when the schedule is on.

## 6. Tags the workflow adds

With the default `TAG_PREFIX`:

| Tag | Meaning |
|---|---|
| `reactivation-sent` | Got the campaign text. Never texted by the campaign again |
| `reactivation-send-failed` | The text was rejected. Fix the number, then remove both tags to retry |
| `reactivation-replied` | Replied at least once |
| `reactivation-interested` | Asked for times |
| `reactivation-booked` | Booked. The assistant stops replying and your team takes over |
| `reactivation-not-now` | Not interested right now |
| `reactivation-wrong-number` | Wrong person. The assistant stops replying |
| `reactivation-opted-out` | Replied STOP or asked to stop. SMS DND is on |
| `reactivation-needs-human` | Needs a person. The assistant stops replying. Remove the tag to hand the lead back |

## Troubleshooting

Open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| Run stopped at "Stop: Bad Run Secret" or "Stop: Bad Reply Secret" | The header or `customData.secret` does not match `RUN_NOW_SECRET` / `REPLY_WEBHOOK_SECRET` |
| Run Now answered 500, run stopped at "Stop: Bad Settings" | A setting is missing or invalid. The response and the alert list every problem, for example an empty `GHL_CALENDAR_ID` or a wrong `BUSINESS_TZ` |
| A run failed but Slack stayed quiet | The error alert workflow is not active, is not chosen as the Error Workflow, or `SLACK_WEBHOOK_URL` is empty |
| Run stopped at "Stop: Search Failed" | `GHL_API_KEY` scopes (`contacts.readonly`) or `GHL_LOCATION_ID` |
| Everyone skipped with "quiet hours" | Check `SEND_HOUR_START`, `SEND_HOUR_END` and `BUSINESS_TZ` |
| Run stopped at "Stop: Sends Failed" | The error lists each lead and the reason, for example an invalid number |
| Reply run ended at "Route Reply" | The reply was ignored on purpose. The item on the `ignore` output says why (not in the campaign, duplicate, booked, opted out, DND) |
| Run stopped at "Stop: Reply Needs Attention" | Read the message. It says what the lead got (a reply, a holding reply, or nothing when their message may be an opt out) and which step to finish by hand |
| "without the conversation history" | The lead was answered, but the conversation could not be loaded. Check that `GHL_API_KEY` has the `conversations.readonly` and `conversations/message.readonly` scopes |
| Slack: "Booked lead opted out of texts" | A booked lead texted STOP, CANCEL or another opt out word. Their SMS DND is on. Call or email them to check whether they also meant to cancel the appointment |
| Run stopped at "Stop: Opt Out Failed" | GoHighLevel did not accept the DND update. Set DND for SMS on that contact by hand right away |
| "the opportunity was not saved in the booked stage" | The lead is booked and was told so. Their only opportunity in the pipeline is probably won (it is never reopened) and multiple opportunities per contact are off. Add or move the opportunity by hand |
| Runs text nobody while tagged leads are left (a Run Now summary shows only skips) | The first `SEARCH_PAGE_SIZE` x `MAX_SEARCH_PAGES` search results are all leads that are skipped every time (SMS DND, invalid number). Remove the reactivation tag from them or raise `MAX_SEARCH_PAGES` |
