"""Changes to a hub's own files, proposed from chat and applied after review.

A person (or an agent acting for them) proposes exact edits to the hub's
instructions, knowledge, skills or shared prompts. Someone who manages the hub
reviews the diff and applies it: Hubzoid writes exactly those bytes, only if
each file still has the bytes it had when the change was proposed, and
commits them with trailers that name the change. "Undo" is a new proposal
that needs its own review. See ``service``.
"""
