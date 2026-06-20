"""
Authorization port + provider factory.

The rest of the codebase depends ONLY on this package's `Authorizer.check()`
interface and `get_authorizer()`. It never imports a vendor SDK directly.
Swapping or removing a provider is a one-file, one-env-var change.
"""
from __future__ import annotations
import os
from typing import Protocol


class Authorizer(Protocol):
    """The single interface every provider implements."""
    def check(self, user: str, action: str, resource: str) -> bool: ...


class AccessDenied(Exception):
    pass


def get_authorizer() -> "Authorizer":
    """Pick the provider by env. Default: mock (no dependencies)."""
    provider = os.environ.get("AUTHZ_PROVIDER", "mock").lower()
    if provider == "permit":
        from .permit_provider import PermitAuthorizer
        return PermitAuthorizer()
    # To add Casbin / OpenFGA later: create authz/<name>_provider.py with the
    # same .check(user, action, resource) method and one elif here. One file each.
    from .mock_provider import MockAuthorizer
    return MockAuthorizer()
