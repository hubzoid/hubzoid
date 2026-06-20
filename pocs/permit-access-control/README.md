# Access-control POC, provider-swappable (Permit today, anything tomorrow)

This POC is built so the authorization provider is a swappable adapter behind a
single interface. Permit.io is ONE adapter in ONE file. If you decide not to use
Permit, you change one env var and delete one file. Nothing else moves. You are
committing to an interface, not to a vendor.

## The design (why there is no lock-in)

    your code  -->  authz.get_authorizer().check(user, action, resource)
                          |
                          |  picked by the AUTHZ_PROVIDER env var
                          v
            +-------------+--------------+
            |             |              |
        mock_provider  permit_provider   (casbin_provider, openfga_provider, ...)
       (no deps)       (the ONLY file     (add later, same interface,
                        importing permit)   one file each)

- `dispatch.py` and your tools depend only on the `Authorizer.check()` interface.
- They never import the Permit SDK. Only `authz/permit_provider.py` does.
- So the blast radius of "drop Permit" is exactly one file plus one dependency line.

## Run it now (mock, no account, no install)

    python3 demo.py

Same Samarth scenario: Priya (owner) allowed on the Ornate tool, Anjali
(associate) denied on it and allowed on stock, plus the access log.

## Switch to real Permit (one env var, no code change)

    pip install permit
    export AUTHZ_PROVIDER=permit
    export PERMIT_API_KEY=<your key>
    python3 demo.py

For RBAC the cloud PDP (https://cloudpdp.api.permit.io) is the default, so you do
not need Docker. Run the local PDP container only for ABAC / ReBAC or air-gap.

`real_check.py` is a tiny smoke test mirroring the Permit quickstart (John Doe /
read / document), to confirm the Python SDK reaches your workspace.

## To DROP Permit entirely (the "no cleanup" promise)

1. Set `AUTHZ_PROVIDER=mock` (or `casbin`, once you add that adapter).
2. Delete `authz/permit_provider.py`.
3. Remove `permit` from `requirements.txt`.

`dispatch.py`, `demo.py`, your tools, and the audit log are untouched. Every
`check(user, action, resource)` call stays exactly as it is.

## Files

- `authz/__init__.py`        the interface (`Authorizer.check`) + `get_authorizer()`
- `authz/mock_provider.py`   local mock (no dependencies)
- `authz/permit_provider.py` Permit adapter (the only file that imports the SDK)
- `dispatch.py`              SDK-agnostic enforcement point + audit log
- `demo.py`                  the Samarth scenario
- `real_check.py`            real Permit smoke test (quickstart parity)
