"""Reading a small JSON body: a browser that leaves mid-request is a client
error, never a server error with a traceback in the log."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from hubzoid.chat.engine_routes import read_json


class _Gone:
    headers: dict = {}

    async def stream(self):
        raise ClientDisconnect()
        yield b""  # pragma: no cover - makes this an async generator


class _Body:
    headers = {"content-length": "11"}

    async def stream(self):
        yield b'{"a": true}'


def test_a_disconnected_client_is_a_client_error():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_json(_Gone()))
    assert exc.value.status_code == 400


def test_a_body_is_read():
    assert asyncio.run(read_json(_Body())) == {"a": True}
