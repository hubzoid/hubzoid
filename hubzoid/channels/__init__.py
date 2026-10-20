"""Channels: work that starts in a provider thread and answers there.

A channel connection (``channels/<id>.yaml``) binds one provider account to
this hub: Slack, GitHub, Linear, email or WhatsApp. The shared contract here
keeps each piece of work tied to the thread it came from:

* ``admission``: verify the provider's signature, normalize, and commit
  before acknowledging. Repeats are recognised; reused ids with different
  bytes are quarantined.
* ``router``: route accepted deliveries to the native chat engine (agent
  connections) or to a durable work item (workflow connections), and post
  finished work back.
* ``replies``: every post is a recorded outside write that is never resent
  on its own when its outcome is unknown.
* ``links``: which hub account a provider account acts as.
* ``config``, ``doctor``, ``routes`` and ``cli``: files, read-only checks,
  ``/api/hubs/{hub_id}/channels`` and ``hubzoid channels``.

Each provider is an adapter (``protocol.Adapter``) in its own subpackage.
"""
