# Contributing to the storefront

- Python 3.12, Flask blueprints, one blueprint per area under `app/`.
- Every route under `/orders` and `/admin` is for staff and uses
  `@login_required` from `app/auth.py`.
- Database access goes through `get_db()` in `app/db.py`, with `?`
  parameters. Never format values into SQL.
- Settings and secrets come from the environment through `app/config.py`.
- Calls to suppliers and the payments API have a timeout. A failure is
  logged and the caller decides what to do; nothing retries forever.
- Add or update a test in `tests/` for every behaviour change.
