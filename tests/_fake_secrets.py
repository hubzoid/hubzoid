"""A stand-in for AWS Secrets Manager, shared by the secrets and layers tests.

Nothing here touches the network: `install` replaces config_secrets._client,
so a test that forgets to name a secret fails loudly instead of calling AWS.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager


class FakeSecrets:
    def __init__(self, secrets: dict | None = None):
        # name -> dict (served as JSON), str (served verbatim), or an exception
        self.secrets = dict(secrets or {})
        self.calls: list[tuple[str, str | None]] = []

    def client(self, region):
        fake = self

        class _Client:
            def get_secret_value(self, SecretId):  # noqa: N803 - boto3's argument name
                fake.calls.append((SecretId, region))
                value = fake.secrets.get(SecretId)
                if value is None:
                    from botocore.exceptions import ClientError

                    raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                                 "Message": "Secrets Manager can't find the specified secret."}},
                                      "GetSecretValue")
                if isinstance(value, BaseException):
                    raise value
                if isinstance(value, dict) and "SecretBinary" in value:
                    return value
                return {"SecretString": value if isinstance(value, str) else json.dumps(value)}

        return _Client()

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def install(monkeypatch, secrets: dict | None = None) -> FakeSecrets:
    from hubzoid import config_secrets

    fake = FakeSecrets(secrets)
    monkeypatch.setattr(config_secrets, "_client", fake.client)
    return fake


@contextmanager
def clean_process_env(**values):
    """Run with exactly these environment values plus PATH and HOME, then put
    the real environment back. Loads mutate os.environ directly, so
    monkeypatch alone cannot undo them."""
    from hubzoid import config_secrets

    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update({k: saved[k] for k in ("PATH", "HOME") if k in saved})
    os.environ.update(values)
    config_secrets.clear_cache()
    try:
        yield os.environ
    finally:
        os.environ.clear()
        os.environ.update(saved)
        config_secrets.clear_cache()
