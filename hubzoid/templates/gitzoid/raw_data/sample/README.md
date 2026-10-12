# Sample data (fictional)

`kestrel-oak/storefront` is a made-up shop. Every person, pull request, commit
and line of code here is fictional. The OSV and CISA KEV records in `osv/` are
trimmed copies of public records, captured in October 2026.

- `storefront/`: the default branch.
- `pulls/<n>-<slug>/`: open pull requests. `pull.yaml` holds the title,
  author and comments; `head/` holds the changed files at the pull request's
  head. GitZoid computes the diffs.
- `sample.yaml`: the repo, people and their GitHub permission, a linked issue
  and about three months of merged pull requests for the digest and reports.

Planted on purpose: #101 retries forever and prints, #102 exports customer data
without a sign-in check, #103 builds SQL from the search text, #105 pins a
gunicorn release with known request-smuggling advisories, and the default
branch pins PyYAML 5.3.1 (critical advisory, reachable from `app/catalog.py`).
#104 is a clean control and #106 a draft.
