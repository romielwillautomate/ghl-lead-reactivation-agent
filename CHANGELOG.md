# Changelog

## v1 (2026-10-21)

First build: a GoHighLevel lead reactivation campaign and a Claude reply assistant that books appointments, built in n8n, with an error alert workflow and an end to end mock test (50 scenarios, 73 checks).

### Campaign

- Run Now webhook protected by the `X-Run-Secret` header, with a dry run mode, plus an optional hourly schedule (`SCHEDULE_ENABLED`). Both share the same logic.
- Settings are checked before anything else. Run Now answers 500 with the list of problems, and nobody is texted while the reply side (reply secret, Claude key, calendar, pipeline, booked stage) is not set up.
- Dormant leads come from `POST /contacts/search`: the reactivation tag, or no activity for `DORMANT_DAYS`, with a phone, no DND, not texted before and not excluded. Results are paged and every rule is checked again in code, including SMS DND and quiet hours in the lead's own time zone (`BUSINESS_TZ` when the contact has none or an invalid one).
- One lead per pass: the contact is reloaded right before sending (the search index can lag behind tag and DND changes), tagged `reactivation-sent`, texted through `POST /conversations/messages` (type SMS), then the run waits `SEND_DELAY_SECONDS`. `BATCH_LIMIT` caps each run.
- A Slack summary of who was texted, who failed and why the rest were skipped. Failed texts end the run as failed.

### Replies

- A reply webhook for GoHighLevel's Customer Replied workflow, checked with a shared secret sent as a header (Custom Webhook action) or as `customData.secret` (standard Webhook action). Both payload shapes are read, and the `InboundMessage` format works too.
- Repeat deliveries are skipped by message id, or by the id found in the conversation when the payload has none.
- STOP and the other opt out words set SMS DND in code before any AI call, keeping the contact's other DND channels.
- Claude (`ANTHROPIC_MODEL`, default `claude-sonnet-5`) picks one of six intents through a forced tool call and writes the reply, using only the facts in `BUSINESS_FAQ`.
- Interested leads get 3 open times from the GoHighLevel free slots endpoint, one per day where possible. A picked time is booked with `POST /calendars/events/appointments` and the opportunity is moved to the booked stage, or created when there is none.
- Hand offs to a person with the `needs-human` tag and a Slack alert: questions the facts do not cover, upset leads, `MAX_AI_REPLIES` reached, Claude failures (with a holding reply, unless the message may be an opt out), the calendar down or full.
- Every failure ends the run as failed with the reason, for the error alert workflow.

### Found and fixed while testing

- **The search date filter used an undocumented key.** The `lastActivity` filter sent `range` with `lte`. GoHighLevel's documented example for `range` uses `gt` and `lt`, so it now sends `lt`, and the mock only accepts the documented keys.
- **Campaign runs could wipe reply state.** An early version kept a send log in workflow static data. A campaign run takes minutes, and saving its copy of the static data at the end overwrote the offered times and reply counts that replies had saved in the meantime. The `reactivation-sent` tag on the contact is now the only record of a send, and campaign runs never write static data.
- **Settings errors were hard to read.** A missing setting made a Code node throw after Run Now had already answered 202, and n8n shortens Code node errors to the text after the last colon. Settings problems now go to a Stop and Error node that keeps the full list, and Run Now answers 500 with the same list.
- **A campaign could start without a working reply side**, so leads who answered would hear nothing back. Those settings are now required before anyone is texted.
- **Error alerts depend on the error workflow being active.** n8n 2.x skips an inactive error workflow without any alert. The setup guide now says to activate it, and the test imports and activates it and checks that every failed run raised exactly one alert linked to that run.
- **The Slack summary counts read like settings** ("over the batch limit 4"). They now read "4 over the batch limit".

### Found and fixed in review

- **A won opportunity could be reopened.** After a booking, the workflow moved the lead's open opportunity or, if there was none, the first one it found, which could be a won sale; moving it set it back to open and rewrote the sales history. Now only an open opportunity, or else a lost or abandoned one, is moved. With only a won one, a new opportunity is created, and when GoHighLevel refuses a second opportunity in the pipeline (its default), the lead is still confirmed and the alert says to add it by hand.
- **A new message could be taken for a repeat delivery.** Without a message id in the payload, the reply was matched to the newest message in the history with the same words. If the webhook arrived before GoHighLevel showed the new message, an older "yes" or "1" from days before matched, and the new message was ignored. Only a match from the last 24 hours counts now.
- **Lead text went into Slack unescaped.** Slack reads `<!channel>`, `<!here>` and `<url|label>` in message text, so a lead who texted `<!channel>` would have pinged the whole channel from the hand off alert (and from the error alert when their contact could not be loaded). Lead names, lead messages and Claude's replies are now escaped (`&`, `<`, `>`) everywhere they reach Slack.
- **The time zone fallback was not really tested.** The leads with no or an invalid time zone were texted, but the business time zone was inside the texting window, so the check also passed if the code had used the wrong zone. The schedule test now sets `BUSINESS_TZ` to a zone where it is 3 AM and checks that two such leads are skipped for quiet hours.
- **The mock was looser than the real API.** It now refuses unknown body properties (as GoHighLevel does with "property X should not exist" and Anthropic with "Extra inputs are not permitted"), a second opportunity for a contact in the same pipeline whatever its status, and an invalid `status` or `limit` on the opportunity search. Checks for leads who write again after booking, opting out or a wrong number now also require that no tags were written and that the run ended at "Route Reply".
- **Docs.** Added the limits that were missing: overlapping campaign runs can race, leads skipped on every run can fill the search window, and the dry run works from search data. Added the API call that lists calendar IDs, the opportunity setting, and two troubleshooting rows.

### Found and fixed in the second review

- **A lead's "2" could book the wrong time.** Offered times that had passed were dropped from the list Claude saw, and the rest were renumbered. A lead who answered the next day, after the first time had passed, and texted "2" was booked into the third time from their text, and a lead who picked the passed time was booked into another one. Times now keep the numbers the lead saw, a passed time is marked as passed, and picking it gets an honest "that time has already passed" with new times. The test moves an offered time into the past through the workflow's static data to prove both cases.
- **The holding reply could go to someone who asked to stop.** When Claude was down, a message like "Please stop sending these" (an opt out in a sentence, not a bare keyword) still got "a team member will text you shortly". Now a message with an opt out word in it gets no holding reply while Claude is down; it gets the needs-human tag and the alert says to set DND by hand if it is an opt out. Claude failure alerts now also quote what the lead wrote.
- **A missing conversation history went unnoticed.** If GoHighLevel did not return the conversation (a missing scope, an outage), Claude answered from the newest message alone and the run looked fine. The lead is still answered, but the run is now flagged and the alert names the scopes to check.
- **A booked lead's CANCEL left the team in the dark.** CANCEL is a carrier opt out word and is honored as one, but the lead may have meant the appointment, which the workflow never cancels. Slack now says the booking is still on the calendar.
- **Some Claude models could never work, but a campaign would still start.** The docs warned about `claude-opus-5-5` and `claude-fable-5-1` only. Per Anthropic's model docs, `claude-fable-5` and the Mythos models also refuse `thinking` turned off (the free token counter does not check that setting, so it passed there). A campaign run now refuses to start with any of these models, and the docs list them.
- **The mock Claude** now applies the per model rules for forced tool calls and `thinking`, as listed in Anthropic's model docs.
- **Docs.** The README diagrams now show the dry run, the failed-run alert and the early "not an SMS" check. Setup notes GoHighLevel's own calendar notifications (`toNotify`) and the calendar type to use. Two troubleshooting rows were added.
