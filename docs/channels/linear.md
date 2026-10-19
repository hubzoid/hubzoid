# Linear

Delegate a Linear issue to the hub's app, or mention it, and the work happens
in that Linear agent session: an acknowledgement within seconds, then the
answer. Follow-ups in the session continue the same work, and Stop stops it.
See [Channels](../channels.md) for what every provider shares.

## The file

```yaml
version: 1
id: linear-main
provider: linear
enabled: false
agent: main
run_as: linked_requester
allowed_groups: [engineering]
provider_config:
  organization_id: 11111111-1111-4111-8111-111111111111   # your workspace's UUID
  team_ids: [22222222-2222-4222-8222-222222222222]        # teams the app may work in
  oauth_client_id: 33333333-3333-4333-8333-333333333333
  client_secret_ref: secret:channel/company-hub/linear-main/client-secret
  webhook_secret_ref: secret:channel/company-hub/linear-main/webhook-secret
  uploads: true
  upload_max_bytes: 10485760
  upload_max_count: 5
```

The ids above are placeholders. Replace them with your workspace's.

## Setting it up

> Connect this hub to our Linear workspace. Use agent main, permit only our
> Engineering team and the group engineering. Create the channel file, show
> the diff, guide an administrator through installing our app and verify a
> synthetic delegated issue. Do not change real issues.

```sh
hubzoid channels apply ./company-hub --file channels/linear-main.yaml --expected-revision absent
hubzoid channels linear setup ./company-hub linear-main
```

`setup` prints the exact callback and webhook URLs and the steps in Linear:
Settings, API, Applications, create an OAuth application with your own name
and icon, set the callback URL, turn on webhooks for Agent session events and
Permission changes. An administrator then stores the two secrets (never in
the chat):

```sh
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/linear-main/client-secret --stdin --apply
hubzoid secrets set ./company-hub --ref secret:channel/company-hub/linear-main/webhook-secret --stdin --apply
hubzoid channels linear connect ./company-hub linear-main --wait 300
```

A workspace admin opens the printed link and approves the app. It installs as
an app (`actor=app`) with `read`, `write`, `app:assignable` and
`app:mentionable`. A grant for any other workspace is refused and revoked.
Then link people and check:

```sh
hubzoid channels link ./company-hub linear-main --external-id <linear-user-id> --account ana@example.com
hubzoid channels doctor ./company-hub linear-main
```

## How it behaves

- Only a verified delegation or mention, in an allowed team, by a linked
  person, starts work. The issue's own text is context, never the request.
- Every reply is an activity in the session it came from: a thought to
  acknowledge, then the response, or an error. Each activity gets an id made
  from the reply, so an answer whose outcome was lost is looked up by that id
  (never by matching text) and is never posted twice. Linear's own create
  inputs accept a client id; this is not yet checked against a live
  workspace.
- Stop in Linear cancels the person's running work in that session.
- The plan is posted as a response. Linear's native plan checklist is a
  technology preview and is not used.
- The app token renews itself before it expires, one worker at a time. If
  Linear refuses the renewal, the connection asks to be reconnected. `doctor`
  never renews it: it reports an expired token instead.
- No Linear tool is offered to agents, so unattended work never reads or
  writes Linear.

## Troubleshooting

| Code | What to do |
|---|---|
| `public_url_required` | Set `HUBZOID_PUBLIC_URL` before installing. |
| `oauth_state_invalid` | Run `connect` again: each link works once, for ten minutes. |
| `workspace_mismatch` | Approve the app in the workspace named in the file. |
| `missing_scope` | Reconnect and approve all requested permissions. |
| `clock_skew` | Synchronize the server clock. Deliveries older than 60 seconds are refused. |
| `team_not_allowed` | Add the team to `team_ids`, or delegate from an allowed team. |
| `reauth_required` | Reconnect the app. |
