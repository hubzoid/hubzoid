---
title: Staff routes need sign-in
paths: ["app/**"]
severity: high
---

Every route under `/orders` and `/admin` is for staff and must use
`@login_required` from `app/auth.py`. A missing check exposes customer emails
and addresses.

Bad:

    @bp.get("/admin/export")
    def export(): ...

Good:

    @bp.get("/admin/export")
    @login_required
    def export(): ...
