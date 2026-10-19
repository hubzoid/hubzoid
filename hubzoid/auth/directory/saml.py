"""SAML 2.0 sign-in through pysaml2 (Hubzoid is the service provider).

What is checked, by pysaml2 and here:

  * the assertion is signed by the identity provider's certificate (from its
    metadata), checked with ``xmlsec1``; unsigned or tampered responses fail;
  * it answers a request this browser started (``InResponseTo``, with the
    request id held in an encrypted, short-lived cookie): an unsolicited
    response is never accepted directly, it starts a fresh sign-in instead;
  * audience, recipient, issuer and validity window;
  * an assertion id is accepted once (``hz_saml_replay``);
  * the NameID must match one SCIM-provisioned, active person of this provider.

The identity provider's metadata comes from a pasted XML document or its URL
(fetched over https, bounded in size and time). Requests from Hubzoid are not
signed and assertions are not encrypted, so Hubzoid holds no SAML private key.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import httpx

log = logging.getLogger("hubzoid.auth.saml")

METADATA_MAX = 512 * 1024
REQUEST_SECONDS = 600


class SamlRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def available() -> tuple[bool, str]:
    """Whether SAML can run here: pysaml2 installed and ``xmlsec1`` on the path."""
    try:
        import saml2  # noqa: F401
    except ImportError:
        return False, "Install SAML support: pip install 'hubzoid[saml]'."
    if not shutil.which("xmlsec1"):
        return False, ("SAML needs the xmlsec1 program: brew install libxmlsec1 (macOS) or "
                       "apt-get install xmlsec1 (Debian, Ubuntu).")
    return True, ""


def fetch_metadata(url: str) -> str:
    if not url.startswith("https://") and not url.startswith("http://localhost") \
            and not url.startswith("http://127.0.0.1"):
        raise SamlRefused("metadata_url", "Give the metadata URL over https.")
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        raise SamlRefused("metadata_unreachable", f"Could not fetch the metadata ({type(exc).__name__}).")
    if r.status_code != 200 or len(r.content) > METADATA_MAX:
        raise SamlRefused("metadata_unreachable", f"The metadata URL answered HTTP {r.status_code}.")
    return r.text


def idp_summary(metadata_xml: str) -> dict:
    """The identity provider's entity id, sign-in URL and certificate count,
    from its metadata (the XML is parsed with defusedxml)."""
    from defusedxml import ElementTree

    if not metadata_xml or len(metadata_xml) > METADATA_MAX:
        raise SamlRefused("metadata_invalid", "Paste the identity provider's metadata XML.")
    try:
        root = ElementTree.fromstring(metadata_xml)
    except Exception as exc:  # noqa: BLE001
        raise SamlRefused("metadata_invalid", "That isn't valid SAML metadata XML.") from exc
    md = "{urn:oasis:names:tc:SAML:2.0:metadata}"
    ds = "{http://www.w3.org/2000/09/xmldsig#}"
    entity = root if root.tag == md + "EntityDescriptor" else root.find(f".//{md}EntityDescriptor")
    if entity is None:
        raise SamlRefused("metadata_invalid", "No EntityDescriptor in the metadata.")
    idp = entity.find(f"{md}IDPSSODescriptor")
    if idp is None:
        raise SamlRefused("metadata_invalid", "The metadata has no identity provider section.")
    sso = [e.get("Location") for e in idp.findall(f"{md}SingleSignOnService")
           if (e.get("Binding") or "").endswith("HTTP-Redirect")]
    certs = idp.findall(f".//{ds}X509Certificate")
    if not sso or not certs:
        raise SamlRefused("metadata_invalid", "The metadata needs a redirect sign-in URL and a signing certificate.")
    return {"entity_id": entity.get("entityID") or "", "sso_url": sso[0], "certificates": len(certs)}


def sp_values(base_url: str, provider_id: str) -> dict:
    """What the administrator pastes into Okta or Entra."""
    root = f"{base_url.rstrip('/')}/api/auth/saml/{provider_id}"
    return {"entity_id": f"{root}/metadata", "acs_url": f"{root}/acs",
            "metadata_url": f"{root}/metadata", "name_id": "the user's email or userName",
            "scim_url": f"{base_url.rstrip('/')}/scim/v2/{provider_id}"}


def _client(base_url: str, provider: dict):
    from saml2 import BINDING_HTTP_POST
    from saml2.client import Saml2Client
    from saml2.config import Config

    values = sp_values(base_url, provider["id"])
    conf = Config()
    conf.load({
        "entityid": values["entity_id"],
        "service": {"sp": {
            "endpoints": {"assertion_consumer_service": [(values["acs_url"], BINDING_HTTP_POST)]},
            "allow_unsolicited": False,
            "authn_requests_signed": False,
            "want_assertions_signed": True,
            "want_response_signed": False,
            "only_use_keys_in_metadata": True,
        }},
        **({"metadata": {"inline": [provider["config"]["metadata_xml"]]}}
           if (provider.get("config") or {}).get("metadata_xml") else {}),
        "xmlsec_binary": shutil.which("xmlsec1"),
        "allow_unknown_attributes": True,
        "accepted_time_diff": 60,
    })
    return Saml2Client(config=conf)


def sp_metadata(base_url: str, provider: dict) -> str:
    from saml2.metadata import create_metadata_string

    client = _client(base_url, provider)
    return create_metadata_string(None, config=client.config, valid=None).decode("utf-8")


def start(base_url: str, provider: dict, *, force: bool = False) -> tuple[str, str]:
    """(request id, the URL to send the browser to)."""
    from saml2 import BINDING_HTTP_REDIRECT

    client = _client(base_url, provider)
    entity = provider["config"]["idp_entity_id"]
    request_id, info = client.prepare_for_authenticate(
        entityid=entity, binding=BINDING_HTTP_REDIRECT, force_authn="true" if force else None)
    location = dict(info["headers"]).get("Location")
    if not location:
        raise SamlRefused("provider_error", "Could not build the sign-in request.")
    return request_id, location


def finish(base_url: str, provider: dict, saml_response: str, request_id: str | None) -> dict:
    """Check the identity provider's answer. Returns
    ``{"name_id", "assertion_id", "expires_at", "attributes"}``."""
    from saml2 import BINDING_HTTP_POST
    from saml2.response import StatusError, UnsolicitedResponse

    if not saml_response or len(saml_response) > 1024 * 1024:
        raise SamlRefused("invalid_response")
    if not request_id:
        raise SamlRefused("unsolicited")
    client = _client(base_url, provider)
    try:
        resp = client.parse_authn_request_response(saml_response, BINDING_HTTP_POST,
                                                   outstanding={request_id: "/"})
    except UnsolicitedResponse as exc:
        raise SamlRefused("unsolicited") from exc
    except StatusError as exc:
        raise SamlRefused("provider_error", "The identity provider refused the sign-in.") from exc
    except Exception as exc:  # noqa: BLE001 - signature, audience, time, format
        log.warning("saml: %s response refused (%s)", provider["id"], type(exc).__name__)
        raise SamlRefused("invalid_response") from exc
    if resp is None or getattr(resp, "in_response_to", None) != request_id:
        raise SamlRefused("unsolicited")
    subject = resp.get_subject()
    name_id = (getattr(subject, "text", "") or "").strip()
    if not name_id:
        raise SamlRefused("no_subject")
    assertion = resp.assertion
    expires = 0.0
    try:
        from saml2.time_util import str_to_time

        cond = assertion.conditions
        if cond is not None and cond.not_on_or_after:
            import calendar

            expires = float(calendar.timegm(str_to_time(cond.not_on_or_after)))
    except Exception:  # noqa: BLE001
        expires = 0.0
    return {"name_id": name_id, "assertion_id": assertion.id, "expires_at": expires,
            "attributes": {k: list(v) for k, v in (resp.ava or {}).items()}}
