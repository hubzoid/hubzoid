"""
Permit.io adapter. THE ONLY FILE IN THE CODEBASE THAT IMPORTS THE PERMIT SDK.

To drop Permit entirely: delete this file, remove `permit` from requirements.txt,
and stop selecting AUTHZ_PROVIDER=permit. Nothing else in the codebase changes.
"""
from __future__ import annotations
import os
import asyncio


class PermitAuthorizer:
    def __init__(self, token=None, pdp=None):
        from permit import Permit  # lazy import: mock mode needs nothing installed
        self._permit = Permit(
            # The cloud PDP works for RBAC with no Docker. Use the local PDP
            # container for ABAC / ReBAC or air-gapped deployments.
            pdp=pdp or os.environ.get("PERMIT_PDP_URL", "https://cloudpdp.api.permit.io"),
            token=token or os.environ["PERMIT_API_KEY"],
        )

    def check(self, user, action, resource) -> bool:
        # Permit's SDK is async; wrap it so the rest of the app stays synchronous.
        return asyncio.run(self._permit.check(user, action, resource))
