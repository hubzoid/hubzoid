# Access-control POC, provider-swappable

This POC is built so the authorization engine is a swappable adapter behind a
single interface. You commit to an interface, never to a vendor. Today it runs on
a zero-dependency mock; a free / open-source engine (Casbin, OpenFGA) drops in
later as one file, with no change to the calling code.

## The design (why there is no lock-in)

    your code  -->  authz.get_authorizer().check(user, action, resource)
                          |
                          |  picked by the AUTHZ_PROVIDER env var
                          v
            +-------------+--------------+
            |                            |
        mock_provider            (casbin_provider, openfga_provider, ...)
       (no deps, stdlib)          (add later, same interface, one file each)

- `dispatch.py` and your tools depend only on the `Authorizer.check()` interface.
- They never import an engine SDK. Only a `*_provider.py` file ever does.
- So adding or dropping an engine is exactly one file plus one env var.

## Run it now (no account, no install)

    python3 demo.py

The Samarth scenario: Priya (owner) allowed on the Ornate tool, Anjali
(associate) denied on it and allowed on stock, plus the access log written to
`audit_log.jsonl`.

## Add a real engine later (one file, no calling-code change)

1. Create `authz/casbin_provider.py` with a class exposing
   `check(user, action, resource) -> bool`.
2. Add one `elif provider == "casbin":` branch in `authz/__init__.py`.
3. Run with `AUTHZ_PROVIDER=casbin python3 demo.py`.

`dispatch.py`, `demo.py`, your tools, and the audit log are untouched. Every
`check(user, action, resource)` call stays exactly as it is.

## Files

- `authz/__init__.py`        the interface (`Authorizer.check`) + `get_authorizer()`
- `authz/mock_provider.py`   local in-memory authorizer (no dependencies)
- `dispatch.py`              SDK-agnostic enforcement point + audit log
- `demo.py`                  the Samarth scenario
