---
# One markdown file per background job. This example is disabled — flip
# `enabled: true` (and adjust the cron + instructions) to use it.
# Docs: https://github.com/hubzoid/hubzoid/blob/main/docs/schedule.md
schedule: "7 3 * * 1"        # 5-field cron, local time: Mondays at 03:07
enabled: false
# write: ["knowledge/"]      # writable but NOT auto-committed (review by hand)
# commit: ["knowledge/"]     # paths Hubzoid git-commits after a DONE run
# push: true                 # pull --rebase + push after the commit
---

Keep the docs in knowledge/ in step with the source repos under raw_data/.

- Pull every git repo under raw_data/ (run_git "pull").
- Your state file records the last-processed commit SHA per repo. Process
  everything from that SHA to HEAD. On the very first run (no SHA recorded),
  only look at the last 7 days of commits.
- For each new commit, read its diff (run_git "show <sha>") and update the
  relevant knowledge/*.md so they describe the CURRENT state of the system.
  Never write changelog-style "was X, now Y" sentences.
- A commit with no user-visible effect needs no doc change — just record it.
- After processing each repo, write its new SHA to your state file.
- You are done when every repo's recorded SHA equals its HEAD.
