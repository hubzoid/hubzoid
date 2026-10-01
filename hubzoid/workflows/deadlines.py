"""Cooperative run and call deadlines shared by every runtime adapter."""
from __future__ import annotations
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar

_deadline = ContextVar('hubzoid_call_deadline', default=None)


def seconds(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        factors = {'s': 1, 'm': 60, 'h': 3600}
        result = float(value[:-1])*factors[value[-1]] if value[-1:] in factors else float(value)
    else:
        result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError('Timeout must be finite and positive')
    return result


def remaining(default=None):
    deadline = _deadline.get()
    value = deadline-time.time() if deadline is not None else None
    if value is not None and value <= 0:
        raise TimeoutError('Workflow or call deadline exceeded')
    cap = seconds(default)
    return min(value, cap) if value is not None and cap is not None else value if value is not None else cap


@contextmanager
def scope(timeout=None, *, absolute=None, check_exit=True):
    parent = _deadline.get()
    deadline = absolute if absolute is not None else time.time()+seconds(timeout) if timeout is not None else parent
    if parent is not None and deadline is not None:
        deadline = min(parent, deadline)
    token = _deadline.set(deadline)
    try:
        remaining()
        yield
        if check_exit:
            remaining()
    finally:
        _deadline.reset(token)
