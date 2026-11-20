"""Real Amazon Bedrock calls: Claude on the Claude Agent SDK and one non-Claude
model on LiteLLM, plus doctor's check. Spends a few cents on the AWS account.

Runs only when HUBZOID_E2E_BEDROCK=1 and AWS credentials and a region resolve
(AWS_REGION, AWS_PROFILE ...). HUBZOID_E2E_BEDROCK_OTHER names the non-Claude
model (default amazon.nova-pro-v1:0, with the region's profile prefix)."""
from __future__ import annotations

import os

import pytest

from hubzoid import bedrock, runtime

pytestmark = pytest.mark.e2e


@pytest.fixture
def aws():
    if os.environ.get("HUBZOID_E2E_BEDROCK") != "1":
        pytest.skip("set HUBZOID_E2E_BEDROCK=1 to spend on the AWS account")
    where, _source = bedrock.region("bedrock/sonnet")
    if not where:
        pytest.skip("no AWS region")
    try:
        import boto3

        boto3.client("sts", region_name=where).get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no usable AWS credentials ({type(exc).__name__})")
    return where


def _hub(tmp_path, model):
    (tmp_path / "AGENTS.md").write_text(f"---\nname: Bedrock check\nmodel: {model}\n---\nAnswer briefly.")
    return tmp_path


@pytest.mark.parametrize("model", ["bedrock/haiku", "bedrock/sonnet"])
def test_claude_on_bedrock_answers(tmp_path, aws, model):
    out = runtime.complete_once(_hub(tmp_path, model), {"prompt": "Reply with the single word: ready"})
    assert "ready" in out["text"].lower()
    assert out["model"].startswith("bedrock/")


def test_a_non_claude_bedrock_model_answers_on_litellm(tmp_path, aws):
    other = os.environ.get("HUBZOID_E2E_BEDROCK_OTHER") or \
        f"{bedrock.geo_prefix(aws)}.amazon.nova-pro-v1:0"
    model = f"bedrock/{other}"
    assert runtime.classify(model).engine == "litellm"
    out = runtime.complete_once(_hub(tmp_path, model), {"prompt": "Reply with the single word: ready"})
    assert "ready" in out["text"].lower()


def test_doctor_passes_for_the_primary_model(aws):
    results = bedrock.check("bedrock/sonnet", used_tiers={"sonnet"})
    assert not [t for st, t in results if st == "fail"], results
