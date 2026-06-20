"""
Local in-memory authorizer. No dependencies. The standard RBAC model:
  users -> role, role -> set of (action, resource) it may perform.
Used so the POC (and your test suite) runs with no account and no network.
A real engine (Casbin, OpenFGA, ...) drops in behind the same .check() interface.
"""
from __future__ import annotations


class MockAuthorizer:
    def __init__(self):
        self._user_roles = {}
        self._role_perms = {}

    def assign_role(self, user, role):
        self._user_roles[user] = role
        self._role_perms.setdefault(role, set())

    def grant(self, role, action, resource):
        self._role_perms.setdefault(role, set()).add((action, resource))

    def check(self, user, action, resource) -> bool:
        role = self._user_roles.get(user)
        if role is None:
            return False  # unknown user: deny by default
        return (action, resource) in self._role_perms.get(role, set())
