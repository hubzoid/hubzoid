"""Socket Mode for a Slack connection: events arrive over a websocket the hub
opens with the app-level token, so no public URL is needed.

Each envelope is verified (app and workspace), normalized and committed
before it is acknowledged. If the commit fails the envelope is left
unacknowledged and Slack delivers it again. ``slack_sdk.socket_mode`` gives
us that control. Bolt's listener, which acknowledges first, is not used.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("hubzoid.channels.slack.socket")


class SocketTransport:
    def __init__(self, conn, *, client_factory=None):
        self.hub_dir = conn.hub_dir
        self.cid = conn.id
        self.app_token = conn.secret("app_token_ref")
        self._factory = client_factory
        self.client = None

    def start(self) -> None:
        from slack_sdk.socket_mode import SocketModeClient

        from .. import config
        from .adapter import client

        conn = config.active(self.hub_dir, self.cid)
        if self._factory is not None:
            self.client = self._factory(self.app_token, client(conn))
        else:
            self.client = SocketModeClient(app_token=self.app_token, web_client=client(conn))
        self.client.socket_mode_request_listeners.append(self.on_request)
        self.client.connect()
        log.info("channel %s: Slack Socket Mode connected", self.cid)

    def on_request(self, sm_client, req) -> None:
        """Commit first, then acknowledge. No acknowledgement on a storage
        failure, so Slack redelivers."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        from .. import admission, config
        from ..errors import ChannelError

        ack = lambda: sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))  # noqa: E731
        if req.type not in ("events_api", "interactive"):
            ack()
            return
        payload = req.payload or {}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        try:
            conn = config.active(self.hub_dir, self.cid)
            adapter = conn.adapter
            verified = adapter._verified(conn, payload, raw, method="socket_mode")
            events = adapter.parse(conn, verified)
            admission.admit(conn, verified, events, raw=raw)
        except ChannelError as exc:
            if exc.status >= 500:
                log.warning("channel %s: envelope not saved (%s); Slack will redeliver", self.cid, exc.code)
                return
            log.info("channel %s: envelope refused (%s)", self.cid, exc.code)
        except Exception:  # noqa: BLE001 - not saved, so not acknowledged
            log.exception("channel %s: envelope could not be saved; Slack will redeliver", self.cid)
            return
        ack()

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None
