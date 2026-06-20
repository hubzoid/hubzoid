"""
Access-control POC scenario. The provider is chosen by AUTHZ_PROVIDER (default mock).
The body of this file is identical whether you run on mock or real Permit. That is
the swappability the design guarantees.
"""
import os
import json
from authz import get_authorizer, AccessDenied
from authz.mock_provider import MockAuthorizer
import dispatch


def ornate_sales(store="ALL"):
    return {"store": store, "gross_sales": 4821000, "margin_pct": 23.4}


def stock_lookup(store="BLR-01"):
    return {"store": store, "items_in_stock": 1842}


TOOLS = {"ornate_sales": ornate_sales, "stock_lookup": stock_lookup}


def run(authz, user, tool, **args):
    print(f"\n>>> {user} calls {tool}({args})")
    try:
        result = dispatch.guarded_tool_call(authz, user, tool, TOOLS[tool], args)
        print(f"    ALLOWED -> {result}")
    except AccessDenied as e:
        print(f"    DENIED  -> {e}")


def main():
    if os.path.exists(dispatch.AUDIT_PATH):
        os.remove(dispatch.AUDIT_PATH)

    authz = get_authorizer()
    print("Provider:", os.environ.get("AUTHZ_PROVIDER", "mock"))

    # In mock mode, seed the policy an admin would otherwise set in Permit's web UI.
    # In permit mode the policy already lives in Permit, so there is nothing to seed.
    if isinstance(authz, MockAuthorizer):
        authz.assign_role("priya", "owner")
        authz.assign_role("anjali", "sales_associate")
        authz.grant("owner", "execute", "ornate_sales")
        authz.grant("owner", "execute", "stock_lookup")
        authz.grant("sales_associate", "execute", "stock_lookup")

    run(authz, "priya", "ornate_sales", store="ALL")
    run(authz, "anjali", "ornate_sales", store="BLR-01")
    run(authz, "anjali", "stock_lookup", store="BLR-01")

    print("\n--- access log (audit_log.jsonl) ---")
    with open(dispatch.AUDIT_PATH) as f:
        for line in f:
            e = json.loads(line)
            print(f"  {e['ts']}  {e['user']:7} {e['resource']:13} {e['decision'].upper()}")


if __name__ == "__main__":
    main()
