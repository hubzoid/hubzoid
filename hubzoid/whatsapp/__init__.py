"""WhatsApp surface — a plugin on the shared inbound harness.

WhatsApp (Meta Cloud API) has no dial-out: it only POSTs inbound messages to a
public URL, and replies go back via a separate Cloud API send. This package
provides the four surface functions the harness needs — verify (GET handshake +
HMAC over the raw body), parse (Meta payload), render (WhatsApp flavor), send
(Cloud API text + approved template) — plus token validation.
"""
