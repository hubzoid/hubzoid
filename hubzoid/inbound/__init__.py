"""Shared inbound harness for push-webhook surfaces (WhatsApp, Telegram, …).

Every webhook surface converges on the same shape: receive a signed POST,
verify it is authentic, drop duplicate redeliveries, resolve who sent it,
dispatch to the hub's OpenAI-compatible bridge, and send the reply back.

The plumbing (public route, raw body, reply-200-fast, dedup, dispatch, the
roster gate) lives here once; each surface is a small plugin that provides
`verify / parse / render / send`. See `hubzoid.whatsapp` and `hubzoid.telegram`.
"""
