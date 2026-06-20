"""
Real Permit.io smoke test, the Python equivalent of the JS quickstart.
Proves the SDK reaches your cloud PDP and returns a real decision.

Run:
    pip install permit
    export PERMIT_API_KEY=<your key>     # then ROTATE this key after testing
    python3 real_check.py

It checks the quickstart's own seeded data (John Doe / read / document),
which is what your Permit workspace already contains.
"""
import os
import asyncio
from permit import Permit

permit = Permit(
    pdp=os.environ.get("PERMIT_PDP_URL", "https://cloudpdp.api.permit.io"),
    token=os.environ["PERMIT_API_KEY"],
)


async def main():
    allowed = await permit.check("John@Doe.com", "read", "document")
    print("John Doe -> read -> document :", "ALLOWED" if allowed else "DENIED")


asyncio.run(main())
