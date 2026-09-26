# Who a workflow runs as

Every scheduled run acts as an **ordinary Hubzoid account**: a person, or an
ordinary account your team creates for shared automation (for example
`reports@company.com`). There is no separate service-account type.

The account a run acts as decides:

- which tools and hubs it may use (that account's grants, checked at every call);
- whose personal connections it uses (never the workflow author's, never an
  administrator's);
- whose `hub.state`, scratch folder, published reports and email it gets.

## Choosing the account

The first match wins:

1. `run_as` on the declaration.
2. `HUBZOID_WORKFLOW_USER` in the hub's configuration (`<hub>/.env`, or a hub
   secret).
3. `HUBZOID_WORKFLOW_USER` in the deployment's configuration (the gateway's
   environment, or the deployment secret).
4. The **setup default**: the owner account recorded when Hubzoid first set up
   the deployment.

```python
# workflows/daily_report/main.py
from hubzoid import hub, workflow

@workflow(schedule="every day at 8am", timezone="Asia/Kolkata",
          run_as="priya@company.com")
def daily_report():
    ...
```

```markdown
---
schedule: "0 8 * * 1-5"
run_as: priya@company.com
---
Summarise yesterday's orders for me.
```

Omit `run_as` and the run uses the configured default. `hubzoid schedule list
<hub>` shows, for every workflow and task, who it runs as, or why it cannot run.

### The setup default

- **Local quickstart** (`hubzoid run` with authentication off): the one local
  account, `admin@localhost`.
- **Shared deployment**: the configured initial owner. That is the account in
  `HUBZOID_GATEWAY_ADMIN_EMAIL` (or `WEBUI_ADMIN_EMAIL`). The first time that
  owner signs in, Hubzoid provisions it and records it as the workflow default.
  `admin@company.com` in examples is only an example, and Hubzoid never creates
  it.

Adding people, administrators or hubs later never changes the default. Hubzoid
never picks an arbitrary administrator or the last person to sign in. If a hub
started as a local quickstart and later turned authentication on, the recorded
default is still `admin@localhost`; set `HUBZOID_WORKFLOW_USER` to a real account
(email to `admin@localhost` is refused).

## The account must be usable

An account is usable when all of these hold:

- it has signed in at least once (so it is bound to a real chat account);
- it is not awaiting approval, blocked or replaced;
- on a Console-managed hub, it holds **Use this agent**.

Otherwise the run fails, and its error in the Console and `hubzoid schedule
status` names the fix. Hubzoid **never falls back** to another account, whatever
the reason.

A run resolves its account once, as its first recorded step. After a restart or
a retry, the run keeps that account even if the configuration changed in the
meantime. The account is checked again before every model or agent call, tool
call, publish, email and connection, so a run whose account is blocked stops at
its next call.

## What `run_as` is not

`run_as` chooses whose permissions a run uses. It grants nothing. The run can do
exactly what that account can do at that moment.

`run_as` and `HUBZOID_WORKFLOW_USER` are read only from files and operator
configuration. The operators who can edit `workflows/`, `schedule/` and `.env`
are trusted to choose them. No API, agent tool or model argument can set them.
Any future way to start a workflow from chat or the Console runs as the person
asking, unless an explicit, authorized delegation exists.

## Personal state, files and connections

| | Belongs to |
|---|---|
| `hub.state[...]` | The run's account. The same workflow run for someone else starts empty. |
| `hub.shared_state[...]` | Everyone who runs the workflow. Keep personal data out of it. |
| `hub.run_dir` | This run only. A private folder under `.hubzoid/runs/` that agent file tools cannot read. |
| `workflows/settings.yaml` (`hub.setting`) | The hub. Shared configuration, as before. |
| A markdown task's scratch folder | The first account that runs the task after the upgrade keeps `.hubzoid/schedule/<task>/`. A different account gets `.hubzoid/schedule/<task>@<person>/`. |
| `hub.publish_artifact(...)` | The run's account. See [reports-and-email.md](reports-and-email.md). |
| `hub.send_email(...)` | Goes only to the run's account. |
| Personal connections (Open WebUI native MCP) | That account's own, used through `hub.call_agent`. |

State written before this release is adopted by the first account that runs the
workflow afterwards, and by no one else.

A personal connection (for example Gmail through the chat app's native MCP
support) is used by the agent: `hub.call_agent(...)` acts as the run's account,
so its tools use that person's connection, never the workflow author's or an
administrator's. If the person has not connected, or their connection expired
or was revoked, the agent has no such tool and the call says so; the person
reconnects from chat.

Step return values are kept in the run history, which the hub's managers can
see. Keep personal data inside steps, and in the report you publish.

## Legacy hubs and `workflow:*` grants

Before this release a Python workflow acted as `workflow:<name>` and a markdown
task as `workflow:md:<task>`.

- **Those grants stay, but no run uses them.** Hubzoid does not copy them to
  anyone. When a run's account lacks a permission the old subject held, the run
  log and the server log name it. To keep the behaviour, grant that permission
  to the account the workflow runs as. You can also point `run_as` at an
  account that already holds it. Then remove the old grant.
- **Legacy hubs with nothing configured keep running unchanged.** A legacy hub
  is one whose access is still managed in the chat app. If it has no `run_as`,
  no `HUBZOID_WORKFLOW_USER` and no provisioned owner, its runs keep their old
  service identity, and a warning names the fix. On a legacy hub that identity
  holds no restricted access, exactly as before.
- **Publishing, email and personal connections always need a real account.**
- **Restricted tools in scheduled runs need a Console-managed hub.** On a legacy
  hub a run's account carries no chat-app groups.

## Boundaries

This is access control inside Hubzoid, not an operating-system sandbox. Workflow
Python, `run:` scripts and restricted tools run in the bridge process as the
same operating-system user. Anyone who can edit a hub's code or configuration
can read what that process can read. The rules above keep people's data apart
across chat, agents, the report viewer, email and the Console.

A `run:` script finds the account it runs as in `HUBZOID_RUN_AS`. This is for
information only.
