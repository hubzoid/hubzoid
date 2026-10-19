"""A small SAML identity provider for tests and local rehearsals (pysaml2's IdP).

It signs real assertions with a throw-away key, so Hubzoid's SAML sign-in can
be exercised end to end without a cloud tenant. Not for production use.

Tests import ``TestIdP``. A browser rehearsal runs it as a server:

    python tests/fixtures/saml_idp.py --port 8711 \\
        --sp-metadata-url http://localhost:3600/api/auth/saml/okta/metadata

then adds ``http://localhost:8711/metadata`` as the provider's metadata URL
(Hubzoid's own metadata is fetched on the first sign-in).
The IdP's sign-in page lets you pick the email to sign in as.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import shutil
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _keypair(folder: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hubzoid test IdP")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    key_file, cert_file = folder / "idp.key", folder / "idp.crt"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_file, cert_file


class TestIdP:
    __test__ = False  # not a pytest class

    def __init__(self, base_url: str, sp_metadata_xml: str | None, folder: Path | None = None):
        from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT

        self.base_url = base_url.rstrip("/")
        self.folder = Path(folder or tempfile.mkdtemp(prefix="hz-idp-"))
        self.key_file, self.cert_file = _keypair(self.folder)
        self._settings = {
            "entityid": f"{self.base_url}/metadata",
            "service": {"idp": {
                "endpoints": {"single_sign_on_service": [(f"{self.base_url}/sso", BINDING_HTTP_REDIRECT)]},
                "name_id_format": ["urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"],
                "sign_response": False, "sign_assertion": True,
                "policy": {"default": {"lifetime": {"minutes": 5}, "attribute_restrictions": None,
                                       "name_form": "urn:oasis:names:tc:SAML:2.0:attrname-format:uri"}},
            }},
            **({"metadata": {"inline": [sp_metadata_xml]}} if sp_metadata_xml else {}),
            "key_file": str(self.key_file), "cert_file": str(self.cert_file),
            "xmlsec_binary": shutil.which("xmlsec1"),
        }
        self.server = self._server()
        self._post = BINDING_HTTP_POST
        self._redirect = BINDING_HTTP_REDIRECT

    def _server(self):
        from saml2.config import IdPConfig
        from saml2.server import Server

        conf = IdPConfig()
        conf.load(dict(self._settings))
        return Server(config=conf)

    def trust(self, sp_metadata_xml: str) -> None:
        """Trust a service provider added after start (a browser rehearsal).
        The signing key stays the same, so the metadata Hubzoid read holds."""
        self._settings["metadata"] = {"inline": [sp_metadata_xml]}
        self.server = self._server()

    def metadata(self) -> str:
        from saml2.metadata import create_metadata_string

        return create_metadata_string(None, config=self.server.config, valid=None).decode("utf-8")

    def parse_request(self, saml_request: str):
        return self.server.parse_authn_request(saml_request, self._redirect)

    def response(self, request, email: str, *, sign: bool = True) -> tuple[str, str]:
        """(ACS URL, base64 SAMLResponse) answering ``request`` for ``email``."""
        from saml2.saml import NAMEID_FORMAT_EMAILADDRESS, NameID

        args = self.server.response_args(request.message)
        resp = self.server.create_authn_response(
            identity={"mail": [email]}, userid=email,
            name_id=NameID(format=NAMEID_FORMAT_EMAILADDRESS, text=email),
            authn={"class_ref": "urn:oasis:names:tc:SAML:2.0:ac:classes:Password",
                   "authn_auth": self.base_url},
            sign_assertion=sign, **args)
        return args["destination"], base64.b64encode(str(resp).encode("utf-8")).decode("ascii")


def app(idp: TestIdP, sp_metadata_url: str | None = None):
    from fastapi import FastAPI, Form
    from fastapi.responses import HTMLResponse, Response

    web = FastAPI()
    pending: dict[str, object] = {}

    @web.get("/metadata")
    def metadata():
        return Response(idp.metadata(), media_type="application/samlmetadata+xml")

    @web.get("/sso", response_class=HTMLResponse)
    def sso(SAMLRequest: str, RelayState: str = ""):  # noqa: N803
        if sp_metadata_url and not pending.get("_trusted"):
            import httpx

            idp.trust(httpx.get(sp_metadata_url, timeout=10).text)
            pending["_trusted"] = True
        req = idp.parse_request(SAMLRequest)
        pending[req.message.id] = req
        return f"""<!doctype html><meta charset=utf-8><title>Test identity provider</title>
<style>body{{font:15px system-ui;max-width:420px;margin:80px auto;color:#222}}input,button{{font:inherit;padding:8px;width:100%;margin:6px 0}}</style>
<h2>Test identity provider</h2><p>Signing in to Hubzoid. Choose who you are.</p>
<form method=post action="/sso/answer"><input type=hidden name=request_id value="{html.escape(req.message.id)}">
<input type=hidden name=relay value="{html.escape(RelayState)}">
<label>Email<input name=email value="" placeholder="you@example.com" autofocus></label>
<button>Sign in</button></form>"""

    @web.post("/sso/answer", response_class=HTMLResponse)
    def answer(request_id: str = Form(...), relay: str = Form(""), email: str = Form(...)):
        req = pending.pop(request_id)
        acs, body = idp.response(req, email.strip())
        return f"""<!doctype html><meta charset=utf-8><body onload="document.forms[0].submit()">
<form method=post action="{html.escape(acs)}"><input type=hidden name=SAMLResponse value="{body}">
<input type=hidden name=RelayState value="{html.escape(relay)}"><noscript><button>Continue</button></noscript></form>"""

    return web


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8711)
    parser.add_argument("--sp-metadata-url", required=True)
    args = parser.parse_args()
    # Hubzoid's metadata is fetched on the first sign-in, so the IdP can start
    # before the provider is added (the order an administrator follows).
    uvicorn.run(app(TestIdP(f"http://localhost:{args.port}", None), args.sp_metadata_url),
                host="127.0.0.1", port=args.port, log_level="warning")
