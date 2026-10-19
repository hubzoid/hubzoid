"""Channel errors: one exception carrying the conventions error body
(``{"detail": {"code", "message", ...}}``)."""
from __future__ import annotations


class ChannelError(Exception):
    """A refusal with a stable lowercase code and a plain-English next step."""

    def __init__(self, code: str, message: str, *, status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}

    def http(self):
        from fastapi import HTTPException

        headers = {"Retry-After": str(self.extra["retry_after"])} if "retry_after" in self.extra else None
        return HTTPException(self.status, detail=self.detail(), headers=headers)


def not_found(what: str = "channel") -> ChannelError:
    return ChannelError("not_found", f"No {what} with that ID here, or you cannot see it.", status=404)


def revision_conflict(current: int | None) -> ChannelError:
    return ChannelError("revision_conflict",
                        "Someone changed this since you loaded it. Export the current configuration, "
                        "compare it and apply again against its revision.",
                        status=409, current_revision=current)
