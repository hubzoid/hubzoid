"""Authenticated, size-bounded code-webhook routes on the existing bridge."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from fastapi import APIRouter, HTTPException, Request
from . import events
from .ownership import readiness

log = logging.getLogger(__name__)
MAX_BODY = 256 * 1024


def field(body, path):
    for part in path.split('.'):
        if not isinstance(body, dict) or part not in body:
            return None
        body = body[part]
    return body


def key_for(body, headers, spec):
    template = spec.get('event_key')
    if template:
        paths = re.findall(r'\{([^{}]+)\}', template)
        values = [field(body, p) for p in paths]
        if paths and all(v is not None and v != '' for v in values):
            # Encode components separately: delimiters inside values cannot collide.
            return json.dumps([template, values], sort_keys=True, separators=(',', ':'))
        log.warning('Webhook event key fields missing; using canonical body hash')
    from ..inbound.webhook import DELIVERY_ID_HEADERS
    for name in DELIVERY_ID_HEADERS:
        if headers.get(name):
            return 'id:' + headers[name]
    log.warning('Webhook has no usable sender event key; deduplicating by body hash')
    return 'body:' + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify(raw, headers, name, spec):
    secret = os.environ.get('WEBHOOK_SECRET_' + name.upper().replace('-', '_'), '')
    if not secret:
        return False
    carrier = headers.get(spec.get('signature_header', spec.get('header', 'X-Hook-Secret')).lower(), '')
    if spec.get('verify', 'header') == 'header':
        return hmac.compare_digest(carrier.encode(), secret.encode())
    stamp = headers.get(spec['timestamp_header'].lower(), '')
    try:
        import math
        if not math.isfinite(float(stamp)) or abs(time.time() - float(stamp)) > 300:
            return False
    except (ValueError, OverflowError):
        return False
    expected = hmac.new(secret.encode(), stamp.encode()+b'.'+raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(carrier.removeprefix('sha256=').encode(), expected.encode())


def build_router(hub_dir):
    router = APIRouter()
    limit = asyncio.Semaphore(16)
    from ..inbound.routing import hub_slug
    slug = hub_slug(hub_dir, os.environ)

    @router.post('/webhooks/' + slug + '/{name}')
    async def receive(name: str, request: Request):
        try:
            specs = events.declarations(hub_dir)
        except Exception:
            # Settings edited into an invalid state since start: the sender retries.
            log.exception('Invalid workflow settings; webhook not accepted')
            raise HTTPException(503, 'Workflow settings are invalid') from None
        spec = specs.get(name)
        if spec is None:
            raise HTTPException(404, 'Unknown webhook')
        length = request.headers.get('content-length')
        if length:
            try:
                if int(length) > MAX_BODY or int(length) < 0:
                    raise HTTPException(413, 'Webhook body is too large')
            except ValueError:
                raise HTTPException(400, 'Invalid content length') from None
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > MAX_BODY:
                raise HTTPException(413, 'Webhook body is too large')
        raw = bytes(chunks)
        if not verify(raw, request.headers, name, spec):
            raise HTTPException(401, 'Invalid webhook authentication')
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Expected JSON') from None
        if not isinstance(body, dict):
            raise HTTPException(422, 'Expected a JSON object')
        ready = await asyncio.to_thread(readiness, hub_dir, hub_dir.name)
        workflow = (ready or {}).get('webhooks', {}).get(name)
        if not workflow:
            raise HTTPException(503, 'Workflow engine is not ready')
        key = key_for(body, request.headers, spec)
        payload = {'body': body, 'headers': {k: v for k, v in request.headers.items() if k in ('content-type', 'x-delivery-id', 'x-github-delivery')}}
        try:
            async with asyncio.timeout(5):
                async with limit:
                    eid = await asyncio.to_thread(events.admit, hub_dir, hub_dir.name, name, workflow, key, payload)
        except ValueError:
            raise HTTPException(409, 'Event key content mismatch') from None
        except Exception:
            log.exception('Webhook admission failed')
            raise HTTPException(503, 'Could not durably accept event; retry delivery') from None
        return {'status': 'accepted', 'event_id': eid}
    return router
