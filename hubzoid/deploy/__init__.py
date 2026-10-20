"""`hubzoid deploy`: a hub's files for a hosting provider, and a check of a
deployment from outside.

    hubzoid deploy init railway|maritime [HUB]   Dockerfile, .dockerignore and
                                                 deploy/<provider>/ (steps, variables)
    hubzoid deploy verify URL                    HTTPS, health, sign-in, public URL

Like Agno's per-provider starters and Mastra's deployers: one released image,
and only the provider's files and steps differ. The hub's own files are baked
into the image from Git; its state lives on a durable volume at /data
(HUBZOID_DATA_DIR).
"""
from __future__ import annotations

PROVIDERS = ("railway", "maritime")
