"""config_secrets.fetch: one JSON secret from AWS Secrets Manager, read through
boto3's default chain, refused clearly (and without values) when unusable."""
from __future__ import annotations

import logging
import sys

import pytest

from hubzoid import config_secrets as cs
from tests._fake_secrets import clean_process_env, install

SENTINEL = "s3cr3t-value-never-printed"


@pytest.fixture(autouse=True)
def _clean():
    with clean_process_env():
        yield


def _stubbed_client(monkeypatch):
    """A real botocore client behind a Stubber: real ClientError shapes, no network."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.session.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="example",
                                   region_name="eu-west-1").client("secretsmanager")
    stubber = Stubber(client)
    monkeypatch.setattr(cs, "_client", lambda region: client)
    return stubber


def test_fetch_returns_strings_and_stringifies_scalars(monkeypatch):
    install(monkeypatch, {"prod/app": {"API_KEY": SENTINEL, "PORT": 8080, "RATIO": 0.5,
                                       "FLAG": True, "OFF": False, "EMPTY": ""}})
    values = cs.fetch("prod/app", region="eu-west-1", layer="deployment")
    assert values == {"API_KEY": SENTINEL, "PORT": "8080", "RATIO": "0.5",
                      "FLAG": "true", "OFF": "false", "EMPTY": ""}


def test_fetch_through_a_real_botocore_client(monkeypatch):
    stubber = _stubbed_client(monkeypatch)
    stubber.add_response("get_secret_value", {"Name": "prod/app", "SecretString": '{"A": "1"}'},
                         {"SecretId": "prod/app"})
    with stubber:
        assert cs.fetch("prod/app", region="eu-west-1", layer="hub") == {"A": "1"}


@pytest.mark.parametrize("code, hint", [
    ("ResourceNotFoundException", "no secret with this name"),
    ("AccessDeniedException", "secretsmanager:GetSecretValue"),
    ("DecryptionFailure", "kms:Decrypt"),
])
def test_aws_errors_name_secret_layer_and_class(monkeypatch, caplog, code, hint):
    stubber = _stubbed_client(monkeypatch)
    stubber.add_client_error("get_secret_value", service_error_code=code,
                             service_message=f"denied for {SENTINEL}", http_status_code=400)
    caplog.set_level(logging.DEBUG)
    with stubber, pytest.raises(cs.SecretFetchError) as info:
        cs.fetch("prod/app", region="eu-west-1", layer="restricted")
    err = info.value
    assert (err.name, err.layer) == ("prod/app", "restricted")
    text = str(err)
    assert "prod/app" in text and "restricted" in text and code in text and hint in text
    assert SENTINEL not in text and SENTINEL not in caplog.text
    assert err.__cause__ is None and err.__suppress_context__  # no botocore message chained


def test_missing_credentials_is_named(monkeypatch):
    from botocore.exceptions import NoCredentialsError

    install(monkeypatch, {"prod/app": NoCredentialsError()})
    with pytest.raises(cs.SecretFetchError, match="NoCredentialsError.*instance or task role"):
        cs.fetch("prod/app", region="eu-west-1", layer="deployment")


def test_unexpected_errors_report_the_class_only(monkeypatch):
    install(monkeypatch, {"prod/app": RuntimeError(f"boom {SENTINEL}")})
    with pytest.raises(cs.SecretFetchError) as info:
        cs.fetch("prod/app", region=None, layer="hub")
    assert "RuntimeError" in str(info.value) and SENTINEL not in str(info.value)


@pytest.mark.parametrize("raw, reason", [
    (f"not json {SENTINEL}", "not valid JSON"),
    (f'["{SENTINEL}"]', "JSON object"),
    (f'"{SENTINEL}"', "JSON object"),
    ('{"A": null}', "value of A is null"),
    (f'{{"A": {{"nested": "{SENTINEL}"}}}}', "value of A is a nested value"),
    (f'{{"A": ["{SENTINEL}"]}}', "value of A is a nested value"),
    (f'{{"AWS_ACCESS_KEY_ID": "{SENTINEL}"}}', "key AWS_ACCESS_KEY_ID may not be set"),
    (f'{{"aws_region": "{SENTINEL}"}}', "key aws_region may not be set"),
    (f'{{"AWS_SECRET_NAME": "{SENTINEL}"}}', "key AWS_SECRET_NAME may not be set"),
    (f'{{"HUBZOID_HUB_SECRET_NAME": "{SENTINEL}"}}', "may not be set"),
    (f'{{"HUBZOID_RESTRICTED_SECRET_NAME": "{SENTINEL}"}}', "may not be set"),
    (f'{{"HUBZOID_DEPLOYMENT_SECRET_INHERITED": "{SENTINEL}"}}', "may not be set"),
    (f'{{"LD_PRELOAD": "{SENTINEL}"}}', "key LD_PRELOAD may not be set"),
    (f'{{"PATH": "{SENTINEL}"}}', "key PATH may not be set"),
    (f'{{"1BAD": "{SENTINEL}"}}', "key number 1 is not a valid"),
    (f'{{"OK": "x", "has space": "{SENTINEL}"}}', "key number 2 is not a valid"),
    ('{"A": "a\\u0000b"}', "NUL character"),
])
def test_unusable_secret_content_is_refused_without_values(monkeypatch, caplog, raw, reason):
    install(monkeypatch, {"prod/app": raw})
    caplog.set_level(logging.DEBUG)
    with pytest.raises(cs.SecretFetchError) as info:
        cs.fetch("prod/app", region="eu-west-1", layer="deployment")
    assert reason in str(info.value)
    assert SENTINEL not in str(info.value) and SENTINEL not in caplog.text


def test_binary_secret_is_refused(monkeypatch):
    install(monkeypatch, {"prod/app": {"SecretBinary": b"\x00\x01"}})
    with pytest.raises(cs.SecretFetchError, match="binary secrets are not supported"):
        cs.fetch("prod/app", region="eu-west-1", layer="deployment")


def test_logs_show_count_at_info_and_names_at_debug_never_values(monkeypatch, caplog):
    install(monkeypatch, {"prod/app": {"ONE": SENTINEL, "TWO": SENTINEL}})
    caplog.set_level(logging.DEBUG, logger="hubzoid.config_secrets")
    cs.fetch("prod/app", region="eu-west-1", layer="deployment")
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("2 key(s)" in m and "prod/app" in m for m in info)
    assert any("ONE" in m and "TWO" in m for m in debug)
    assert not any("ONE" in m for m in info)
    assert SENTINEL not in caplog.text


def test_region_resolution():
    arn = "arn:aws:secretsmanager:ap-south-1:123456789012:secret:prod/app-AbCdEf"
    assert cs.region_for(arn, {"AWS_REGION": "us-east-1"}) == "ap-south-1"
    assert cs.region_for("prod/app", {"AWS_REGION": "eu-west-1", "AWS_DEFAULT_REGION": "us-east-1"}) == "eu-west-1"
    assert cs.region_for("prod/app", {"AWS_DEFAULT_REGION": "us-east-1"}) == "us-east-1"
    assert cs.region_for("prod/app", {}) is None


def test_load_secret_fetches_once_per_process(monkeypatch):
    fake = install(monkeypatch, {"prod/app": {"A": "1"}})
    assert cs.load_secret("prod/app", region="eu-west-1", layer="deployment") == {"A": "1"}
    assert cs.load_secret("prod/app", region="eu-west-1", layer="deployment") == {"A": "1"}
    assert fake.calls == [("prod/app", "eu-west-1")]
    assert cs.secrets_in_use()


def test_failed_fetch_is_not_cached(monkeypatch):
    fake = install(monkeypatch, {})
    for _ in range(2):
        with pytest.raises(cs.SecretFetchError):
            cs.load_secret("prod/app", region="eu-west-1", layer="deployment")
    assert len(fake.calls) == 2 and not cs.secrets_in_use()


def test_no_secret_named_means_no_boto3_and_no_client(tmp_path, monkeypatch):
    """Local .env files keep working with boto3 unavailable and never touched."""
    from hubzoid import settings

    hub = tmp_path / "hub"
    (hub / "restricted").mkdir(parents=True)
    (hub / ".env").write_text("MODEL=openai/gpt-4o-mini\nBRIDGE_API_KEYS=k1\n")
    (hub / "restricted" / ".env").write_text("DB_PASSWORD=x\n")
    monkeypatch.setitem(sys.modules, "boto3", None)  # any import now fails
    monkeypatch.setattr(cs, "_client", lambda region: pytest.fail("no secret is named"))
    s = settings.load(hub)
    assert s.bridge_api_keys == ("k1",)
    assert not cs.secrets_in_use()


def test_fetch_without_boto3_fails_clearly(monkeypatch):
    monkeypatch.setitem(sys.modules, "botocore.exceptions", None)
    with pytest.raises(cs.SecretFetchError, match="boto3 is not installed"):
        cs.fetch("prod/app", region="eu-west-1", layer="deployment")
