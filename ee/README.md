# Hubzoid Enterprise (`ee/`)

This directory holds Hubzoid's **Enterprise Edition**. It is **not** MIT. It is
licensed under the [Hubzoid Enterprise License](LICENSE): source-available, free
to read and modify for development and testing, but **production use requires a
paid license key**. See the top-level [`LICENSING.md`](../LICENSING.md).

Features here are gated at runtime by [`hubzoid/licensing.py`](../hubzoid/licensing.py):

```python
from hubzoid import licensing

lic = licensing.load_license()          # reads $LICENSE_KEY
if lic.has_feature("scheduling"):
    ...                                  # enterprise path
```

Inspect the active license with `hubzoid license`.

Status: structural placeholder. The enterprise features (multi-role and
multi-tenant access management, managed and fleet-scale scheduling, the
multi-tenant control plane, audit and cost governance) move here as they are
built. Gating an existing feature (for example the scheduler) behind
`has_feature(...)` is a deliberate behavior change and is done per feature, not
implicitly.
