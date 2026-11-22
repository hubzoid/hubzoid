"""SAML metadata validation and response refusals through the SDK seams."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid.auth.directory import saml

pytestmark = pytest.mark.skipif(not saml.available()[0], reason="pysaml2 or xmlsec1 missing")
MD = "urn:oasis:names:tc:SAML:2.0:metadata"
XML = f'''<EntityDescriptor xmlns="{MD}" entityID="https://idp.example.com">
<IDPSSODescriptor><SingleSignOnService Binding="urn:HTTP-Redirect" Location="https://idp.example.com/sso"/>
<KeyDescriptor><X509Certificate xmlns="http://www.w3.org/2000/09/xmldsig#">certificate</X509Certificate>
</KeyDescriptor></IDPSSODescriptor></EntityDescriptor>'''


@pytest.mark.parametrize("xml,message", [("", "Paste"), ("x" * (saml.METADATA_MAX + 1), "Paste"),
    ("<broken", "valid SAML"), ("<root/>", "EntityDescriptor"),
    (f'<EntityDescriptor xmlns="{MD}"/>', "identity provider section"),
    (XML.replace("HTTP-Redirect", "HTTP-POST"), "redirect sign-in"),
    (XML.replace("X509Certificate", "Other"), "signing certificate")])
def test_metadata_refusals(xml, message):
    with pytest.raises(saml.SamlRefused, match=message) as exc:
        saml.idp_summary(xml)
    assert exc.value.code == "metadata_invalid"


def test_metadata_summary_nested_and_fetch(monkeypatch):
    assert saml.idp_summary("<root>" + XML + "</root>") == {
        "entity_id": "https://idp.example.com", "sso_url": "https://idp.example.com/sso", "certificates": 1}
    from hubzoid.chat import retrieval
    calls = []
    def fetch(url, limits):
        calls.append((url, limits))
        return None, None, XML.encode()
    monkeypatch.setattr(retrieval, "safe_fetch", fetch)
    assert saml.fetch_metadata("https://idp.example.com/metadata") == XML
    assert calls == [("https://idp.example.com/metadata", {"timeout_seconds": 10, "redirects": 3,
                                                          "bytes": saml.METADATA_MAX})]
    def broken(*args):
        raise ValueError("private detail")
    monkeypatch.setattr(retrieval, "safe_fetch", broken)
    with pytest.raises(saml.SamlRefused) as exc:
        saml.fetch_metadata("https://idp.example.com/metadata")
    assert exc.value.code == "metadata_unreachable" and "ValueError" in str(exc.value)
    with pytest.raises(saml.SamlRefused) as exc:
        saml.fetch_metadata("http://idp.example.com")
    assert exc.value.code == "metadata_url"


def test_start_requires_redirect_location(monkeypatch):
    calls = []
    def prepare(**kw):
        calls.append(kw)
        return "request-1", {"headers": [("Location", "https://idp.example.com/sso")]}
    fake = SimpleNamespace(prepare_for_authenticate=prepare)
    monkeypatch.setattr(saml, "_client", lambda *a: fake)
    provider = {"id": "okta", "config": {"idp_entity_id": "entity"}}
    assert saml.start("https://app.example.com", provider, force=True) == ("request-1", "https://idp.example.com/sso")
    assert calls[0]["force_authn"] == "true" and calls[0]["entityid"] == "entity"
    fake.prepare_for_authenticate = lambda **kw: ("request-1", {"headers": []})
    with pytest.raises(saml.SamlRefused) as exc:
        saml.start("https://app.example.com", provider)
    assert exc.value.code == "provider_error"


@pytest.mark.parametrize("response,rid,code", [("", "r", "invalid_response"),
    ("x" * (1024 * 1024 + 1), "r", "invalid_response"), ("answer", None, "unsolicited")])
def test_response_input_limits(response, rid, code):
    with pytest.raises(saml.SamlRefused) as exc:
        saml.finish("https://app.example.com", {"id": "okta"}, response, rid)
    assert exc.value.code == code


@pytest.mark.parametrize("kind,code", [("unsolicited", "unsolicited"), ("status", "provider_error"),
    ("invalid", "invalid_response"), ("none", "unsolicited"), ("mismatch", "unsolicited"),
    ("empty", "no_subject"), ("ok", None), ("bad-time", None)])
def test_sdk_response_translation(monkeypatch, kind, code):
    from saml2.response import StatusError, UnsolicitedResponse
    def parse(*a, **kw):
        assert kw["outstanding"] == {"r": "/"}
        if kind == "unsolicited":
            raise UnsolicitedResponse("bad")
        if kind == "status":
            raise StatusError("bad")
        if kind == "invalid":
            raise ValueError("bad signature")
        if kind == "none":
            return None
        return SimpleNamespace(in_response_to="other" if kind == "mismatch" else "r",
            get_subject=lambda: SimpleNamespace(text=" " if kind == "empty" else " ana@example.com "),
            assertion=SimpleNamespace(id="assertion-1", conditions=SimpleNamespace(
                not_on_or_after="bad" if kind == "bad-time" else "2030-01-01T00:00:00Z")),
            ava={"groups": ("staff",)})
    monkeypatch.setattr(saml, "_client", lambda *a: SimpleNamespace(parse_authn_request_response=parse))
    if code:
        with pytest.raises(saml.SamlRefused) as exc:
            saml.finish("https://app.example.com", {"id": "okta"}, "answer", "r")
        assert exc.value.code == code
    else:
        result = saml.finish("https://app.example.com", {"id": "okta"}, "answer", "r")
        assert result["name_id"] == "ana@example.com" and result["assertion_id"] == "assertion-1"
        assert result["attributes"] == {"groups": ["staff"]}
        assert result["expires_at"] == (0.0 if kind == "bad-time" else 1893456000.0)
