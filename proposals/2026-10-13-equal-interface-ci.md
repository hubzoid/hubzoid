# Equal interface validation within the existing CI budget

## Problem
Native chat and optional Open WebUI are permanently and equally supported,
but release CI does not run native chat's browser journeys or a paired live
interface smoke check. Some explicitly selected regressions run twice.

## Why now
On 13 October the user requested this CI correction for 1.3.2, with unchanged
elapsed time and no unnecessary test growth, together with the support-policy
documentation. Implementation is authorized in that request.

## What should happen
Use the existing two Python lanes and browser job. Run a bounded, model-free
live smoke check for each interface, with real sign-in, streamed chat and
restart persistence. Keep existing security, workflow and recovery checks.
Run native chat's existing browser tests in the existing browser job. Remove
duplicate executions from the general suite, not the underlying tests.

The successful 1.3.1 run 38347956989 took 14m34s in Python lane 2 and 9m07s in
lane 1; its critical workflow step took 4m07s. Move that step to lane 1 and
put the paired smoke check in lane 2. Keep job counts and timeouts unchanged.
Compare candidate timings with that baseline; do not infer unchanged elapsed
time from unchanged timeouts. Runner and dependency-download variance remains.

## Scope and non-goals
No additional test matrix, dependency upgrades, provider calls, packaging
extras or deletion of distinct regression coverage. This smoke check validates
the HTTP integration; it does not replace real-provider or full browser
acceptance of each interface. Deployment-specific validation stays documented.

## Definition of done
Both live checks pass without skips in release CI. Existing native browser
journeys run. Focused regressions are not repeated in the general shards.
The candidate's longest validation job stays within the prior 14m34s baseline,
with any timing or acceptance gap recorded honestly before merge.

## Local validation

- Both live interface checks passed with `CI=true` in a fresh environment with
  the declared Open WebUI extra: native 17.70s, Open WebUI 68.68s, total 86.81s.
  Each starts real services, signs in, streams a scripted response, saves chat
  and verifies it after restart. No skips or live-provider calls.
- All 43 existing native browser journeys and stream/router/image contracts
  passed through `npm run test:app`.
- Focused regression across chat API, session dispatch, WebUI configuration,
  CLI mode selection, gateway selection, Docker configuration and packaging:
  185 passed, 3 slow tests deselected by the default marker, in 13.54s.
- Workflow YAML and embedded shell syntax pass. Job keys, matrices and job
  timeouts match the prior workflow. `git diff --check` passes.

Combined candidate CI and its elapsed-time comparison remain required before
merge; these local results do not establish the GitHub runner timing.
