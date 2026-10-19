"""Put a run's answer where its owner reads it: a new private chat."""
from __future__ import annotations

import time
from pathlib import Path


def to_private_chat(prompt: str, answer: str) -> str | None:
    """A new conversation owned by the run's person, holding the request and
    the answer. Returns its id, or None for a run that acts as no person."""
    from ...chat import store as store_mod
    from ...chat.engine import Actor, ChatEngine
    from ...settings import load as load_settings
    from ..context import _ctx, _identity

    ctx = _ctx()
    ident = _identity(ctx)
    if not ident.is_person or not ident.account_id:
        return None
    hub_dir = Path(ctx.hub_dir)
    store = store_mod.for_hub(hub_dir)
    engine = ChatEngine(hub_dir, store, hub_dir.name.lower())
    actor = Actor(id=ident.account_id, email=(ident.email or ident.subject or "").lower())
    settings = load_settings(hub_dir)
    agent = getattr(settings, "model_label", None) or hub_dir.name
    title = (prompt.strip().splitlines() or ["Repeated request"])[0][:70]
    stamp = time.strftime("%d %b %H:%M", time.gmtime())
    import hashlib

    # One conversation per run: a run recovered after a crash finds it instead of making another.
    cid = "c_" + hashlib.sha256(f"{ctx.hub}:{ctx.run_id}".encode()).hexdigest()[:22]
    return engine.deliver(actor, agent=agent, title=f"{title} · {stamp}", prompt=prompt, answer=answer,
                          conversation_id=cid)
