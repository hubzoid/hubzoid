"""Console administration facade: ``admin.yaml`` settings, the settings
domain registry, read-only diagnostics and support bundles (``/api/admin``).

Each settings domain stays the source of truth for its own values: this
package describes them and delegates validate, plan and apply to their
owners. It adds no second store for anything another domain owns.
"""
