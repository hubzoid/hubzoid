# One run mode: every deployment is a gateway, even of one hub

## Problem

Hubzoid starts in two ways that have drifted apart:

- `hubzoid run <hub>` starts one bridge and the web app for that hub. Its
  settings live in the hub's `.env`, its store is the hub's own
  `.hubzoid/hub.db`, and its routes have no `/b/<slug>` prefix.
- `hubzoid gateway <hub>...` starts an edge, one bridge per hub, a shared
  operational store and a manifest. Deployment settings live in the gateway's
  environment, and hub `.env` files are read again for compatibility.

Each mode has its own rules, so a fix in one leaves the other behind. The
2.0 review on the Isha hubs found five bugs that only exist because there are
two modes:

- **B2 / F10.** Behind a gateway, `/v1/chat/completions` always answers as
  the first hub. `model: "gpms-hub"` returned an IRS answer with no error, and
  `/b/<hub>/v1` isn't routed. A personal API key limited to any other agent can
  never be used. In single-hub mode `/v1` works.
- **F16.** Approval and "Waiting for you" links are built from
  `HUBZOID_PUBLIC_URL`, which behind a gateway holds `/b/<hub>`, so the links
  point at a path the edge doesn't route.
- **U17.** `hubzoid admin create --link` printed `http://127.0.0.1:3080/…`
  for a gateway on port 3750 with a public URL, because the CLI didn't know it
  was in a gateway. (Fixed for this case; the same question, "which mode am I
  in?", is asked in many places.)
- **W48.** The Brand tab said "Using Hubzoid branding" while the gateway
  showed the first hub's `branding/` folder. The gateway and Settings each had
  their own idea of the brand.
- **U9.** Every bridge printed the same bridge-key warning, which is only
  meaningful for a hub run on its own.

Operators also meet two configuration models. Sign-in, the owner and the
public URL are per hub in one mode and per deployment in the other. The
gateway reads hub `.env` files for these settings and stops when they
disagree. Isha has eleven hubs and has been bitten by this (the runbook's
"last hub's WEBUI_NAME wins").

## Why now

2.0 already removed one interface (Open WebUI), and the edge, the shared
store and the manifest are what every 2.0 feature builds on: accounts, API
keys, approvals, hub changes, workflows. Single-hub mode is the odd one out.
Isha's move to 2.0 is the next big deployment, and it runs as a gateway; the
first-use path for new builders runs as a single hub. Both should be the same
code before more features add their own "if gateway" branches.

## What should happen

There is one command and one shape:

- `hubzoid run <hub> [<hub>...]` always starts an edge, one bridge per hub,
  one shared operational store and a manifest. One hub is a deployment of one.
- Every hub is reachable at `/b/<slug>/…`, including `/b/<slug>/v1` for
  personal API keys, MCP and downloads. The first hub (or a chosen default)
  also answers at the root, so single-hub URLs and bookmarks keep working.
- Deployment settings (sign-in, owner, public URL, name, branding) live in
  one place: the deployment's environment or a `deployment.env` next to the
  manifest. A hub's `.env` holds only that hub's settings (model, tools,
  connectors, channel tokens). For a one-hub deployment, the hub's `.env` is
  still read for deployment settings, so `hubzoid init` then `hubzoid run .`
  keeps working with one file.
- Links (set-password, approvals, waiting-for-you, shares) are built from the
  deployment's public origin, never from a hub's public path.
- Branding comes from Settings → Brand. Until a brand is published, the
  manifest records which folder is shown, and Settings says so.
- Warnings that concern the deployment are printed once, by the supervisor,
  naming the hubs they concern.

**Isha and systemd.** Isha restarts hubs independently today. Keep that:
`hubzoid run --no-bridges <hubs>` runs the edge for bridges started as their
own units, and `hubzoid bridge <hub>` (today `hubzoid run <hub> --no-ui`
behind a gateway) runs one hub's bridge under its own unit. A broken hub
fails its own unit and the edge answers "this agent is unavailable" for its
routes, instead of the whole deployment stopping. The runbook gets one unit
template per hub and one for the edge.

**Upgrade path.**

- An existing gateway is already this shape. `hubzoid gateway …` keeps
  working as an alias for one release and says to use `hubzoid run`.
- An existing single hub gets a manifest on its first 2.0 start, in its own
  `.hubzoid/` folder, naming its existing `.hubzoid/hub.db` as the shared
  store. No data moves; `hubzoid backup` covers it as before.
- A hub that was run alone and is later added to a multi-hub deployment
  needs its accounts and chats merged into the deployment's store. That is a
  separate, explicit command (`hubzoid deployment add <hub>`), never automatic.

## Scope and non-goals

- No change to the hub folder contract (`AGENTS.md`, `skills/`, `.env`).
- No hosting-provider work (Railway, Maritime); this makes it simpler later.
- No new UI. Settings already shows deployment-wide sections.
- Not a change to sign-in, access or the operational schema.
- Not merging stores of hubs that were run apart (that is the separate command
  above, designed on its own).

## Open questions

- Where a multi-hub deployment keeps its manifest and store by default
  (`./.hubzoid-deployment` as the gateway does today, or a named folder).
- Bridge ports: keep `BRIDGE_PORT` per hub, or assign them from a range and
  record them in the manifest (simpler for operators, needs care with
  `--no-bridges`).
- Whether the root keeps serving the first hub's agent for a multi-hub
  deployment, or only for a one-hub deployment.
- The cost of an extra edge process for a one-hub laptop run (start time and
  memory on an 8 GB machine). Measure before deciding.
- Windows process supervision for the bridges (the gateway's process-group
  handling already covers it; confirm on a native Windows run).
- Which settings move from a hub `.env` to the deployment, listed one by one,
  with what happens when both are set.

## Definition of done

- `hubzoid run ./hub` and `hubzoid run ./a ./b` start the same processes in
  the same shape; `hubzoid gateway` is an alias with a notice.
- A personal API key limited to the second hub gets that hub's answer at
  `/b/<slug>/v1/chat/completions`; a key for the wrong hub is refused with a
  plain reason.
- Set-password, approval and waiting-for-you links open on a deployment whose
  hubs have `/b/<hub>` public paths.
- Settings → Brand describes what people see, before and after publishing.
- Isha's runbook has per-hub bridge units and an edge unit; stopping one
  bridge leaves the other agents answering.
- An existing single hub upgrades with no data move and keeps its chats,
  accounts and URLs; an existing gateway upgrades by changing the command.
- The five bugs above have regression tests that run against one code path.
