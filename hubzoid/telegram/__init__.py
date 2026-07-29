"""Telegram surface — a plugin on the shared inbound harness.

Telegram can dial out (long-poll) or receive a webhook; we use the webhook so it
reuses the same public inbox as WhatsApp. Its handle is a numeric user id (never
a phone), so a person is bound to their roster row once, via a fixed contact-
share enrollment handled entirely in code (no LLM before verify). This package
provides verify (secret-token header), parse (Update classification), render
(HTML flavor, links kept), send (Bot API), and the enrollment handshake.
"""
