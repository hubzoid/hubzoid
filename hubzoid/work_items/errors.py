"""Errors with a stable code, an HTTP status and a message that says what to do.

The code means the same thing on the CLI, the API and in workflow code. The CLI
maps the status to its exit codes (2 invalid, 3 denied, 4 conflict, 5
unavailable, 6 waiting or uncertain).
"""
from __future__ import annotations


class WorkItemError(Exception):
    """A refused or failed work-item action. `fields` are extra details, such
    as `current_revision` or the existing item id, safe to show the caller."""

    def __init__(self, code: str, message: str, *, status: int = 400, **fields):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.fields = {k: v for k, v in fields.items() if v is not None}

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.fields}

    def __reduce__(self):
        # The workflow engine stores a failed step's exception and raises it
        # again on replay, so it must survive pickling with its code.
        return (_rebuild, (type(self), self.code, self.message, self.status, self.fields))


def _rebuild(cls, code, message, status, fields):
    obj = cls.__new__(cls)
    Exception.__init__(obj, message)
    obj.code, obj.message, obj.status, obj.fields = code, message, status, dict(fields)
    for key, value in fields.items():
        if not hasattr(obj, key):
            setattr(obj, key, value)
    return obj


def not_found(what: str = "work item") -> WorkItemError:
    return WorkItemError("not_found", f"No such {what} here, or you can't see it.", status=404)


def revision_conflict(current: int) -> WorkItemError:
    return WorkItemError(
        "revision_conflict",
        "Someone changed this since you loaded it. Reload, review the change and try again.",
        status=409, current_revision=current)


def denied(message: str, code: str = "permission_denied") -> WorkItemError:
    return WorkItemError(code, message, status=403)


class Parked(BaseException):
    """Raised inside a run when work must wait for a person: an approval of an
    exact action, or a decision about an operation whose outcome is unknown.

    A BaseException, so hub code catching `Exception` cannot swallow it. The
    workflow wrapper records the wait and ends the execution cleanly. Nothing
    holds a thread while the person decides. When they answer, the work item
    continues from its last saved step."""

    def __init__(self, kind: str, operation_id: str, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.kind = kind                # "approval" | "ambiguous"
        self.operation_id = operation_id
        self.message = message
        self.reason = reason


class NotSent(Exception):
    """Raise from a dispatch function when the request provably never left:
    no connection, a validation error before sending. It is safe to try again."""


class Rejected(Exception):
    """Raise from a dispatch function when the other system definitely refused
    the request. It was not applied and will not be retried automatically."""
