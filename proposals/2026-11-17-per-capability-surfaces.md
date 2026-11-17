# Surfaces per restricted capability

Status: implemented in the 2.0.9 PR for review. The widening rule below needs
an explicit OK before release.

## Problem
`HUBZOID_RESTRICTED_SURFACES` is one list for the whole hub. A team building an
IT helpdesk agent wanted its password-reset tools on WhatsApp. Adding
`whatsapp` to the list opened WhatsApp to every restricted capability in the
hub at once (and to every connector). There was no way to open one capability
on a new surface and keep the others where they were.

## Why now
It came from a real build on 2.0.8 (the IT helpdesk report, 12 Nov 2026), and
the rest of that report is fixed in the same release.

## What should happen
A restricted capability may name its own surfaces in the existing, optional
`identity/permissions.yaml`:

```yaml
reset:
  label: Password resets
  surfaces: [web, mcp, whatsapp]
```

- The list replaces `HUBZOID_RESTRICTED_SURFACES` for that capability only.
  `[]` means no surface. Grants still decide who may use it.
- One resolver (`access.guard.surfaces_for`) feeds the tool list, the MCP
  listing, cards and the call-time check, so what a person sees and what they
  may call never differ.
- The names are request surfaces (`web`, `api`, `mcp`, `workflow`, `app`,
  `slack-dm`, `whatsapp`, `telegram`, `webhook`, `email`, `channel`, `cli`,
  `owui`, plus any name in `HUBZOID_RESTRICTED_SURFACES`). An unknown name stops
  the agent from loading with a message naming the file. `slack-channel` is
  refused: a shared thread is answered under one person's identity.

None of Agno, Mastra or Eve has a per-channel tool list: each sets the channel
on the run and lets code decide. A list in the capability's metadata is the
declarative version of that, which suits a hub folder.

## Scope and non-goals
- Restricted capabilities only. Connectors keep the hub-wide list (they have
  their own surface gate); a `surfaces:` on a connector entry in `hubzoid.yaml`
  can follow.
- Built-in capabilities (`use_hub`, `curator`, ...) are not changed by this
  file, as before.
- No new required file or field.

## Open questions
- **Widening.** The list can widen access beyond the hub-wide list. Whoever
  can edit `identity/` then holds a power only `.env` had. Agents can't
  write there (`hub_changes` refuses `identity/`), and in every setup we know
  hub authors are operators. The alternative, narrow-only, is safer but
  doesn't solve the reporter's case. This PR implements widening; it needs a
  yes before release.
- Settings shows restricted rows with no surfaces today; showing the list
  there is a small follow-up.

## Definition of done
`tests/test_access.py::test_one_capability_can_open_a_surface_alone`: the
password-reset tools are offered and callable on WhatsApp while every other
restricted tool stays on the default surfaces, and the e2e report shows the
same on a running hub.
