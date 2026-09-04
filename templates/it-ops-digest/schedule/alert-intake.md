---
on_webhook: alerting          # fires when a POST lands at /webhooks/<hub>/alerting
timeout: 300
max_rounds: 2
max_turns: 20
write: ["output/alerts/"]
---

Handle every alert event that arrived through the alerting webhook.

- Read each new event file under `.inbound/webhooks/alerting/` with
  `list_files` and `read_file`.
- For each event, append one line to `output/alerts/intake-<YYYY-MM-DD>.md`
  (date from `current_time`) using `write_hub_file`: time received, alert
  name, host, severity, and the first runbook step from
  `runbook(alert_name)` when one exists. Read the existing file first so
  you append rather than overwrite.
- Your state file records the event file names already handled, so a
  retried run does not write a line twice.
- Do not acknowledge or silence anything. The intake line is for the
  digest and the person on call.
- You are done when every event under the inbox has a line and is recorded
  in the state file. The runner archives handled events after a successful
  run.
