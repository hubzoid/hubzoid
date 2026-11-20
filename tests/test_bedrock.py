"""Amazon Bedrock as a model provider (MODEL=bedrock/...): routing, overrides,
the Claude child's environment at every entry point, region, delegates, chat
model choices, pins, caching, cost and doctor. No AWS and no model: boto3 and
the SDK's query() are stubbed. Real-provider checks live in tests/e2e/."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from hubzoid import bedrock, handover, runtime
from tests._fake_secrets import clean_process_env

MINIMAL = Path(__file__).parent / "fixtures" / "minimal_hub"
PARENT = {   # what a shared box hands every hub process
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "oauth-2",
    "ANTHROPIC_API_KEY": "sk-ant", "AWS_ACCESS_KEY_ID": "AKID", "AWS_SECRET_ACCESS_KEY": "secret",
    "AWS_PROFILE": "dev",
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with clean_process_env():
        for var in ("AWS_REGION", "AWS_REGION_NAME", "AWS_DEFAULT_REGION", "ANTHROPIC_BEDROCK_REGION_PREFIX",
                    "DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "MODEL"):
            monkeypatch.delenv(var, raising=False)
        bedrock._profiles.clear()
        yield


def _hub(tmp_path: Path, env: str) -> Path:
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub, ignore=shutil.ignore_patterns("output", "__pycache__"))
    (hub / ".env").write_text(env)
    return hub


class _Profiles:
    """boto3's bedrock client, only GetInferenceProfile."""

    def __init__(self, backing: str | None):
        self.backing = backing

    def get_inference_profile(self, inferenceProfileIdentifier):
        if self.backing is None:
            raise RuntimeError("AccessDeniedException")
        return {"models": [{"modelArn": f"arn:aws:bedrock:ap-south-1::foundation-model/{self.backing}"}]}


def _capture(monkeypatch) -> dict:
    import claude_agent_sdk

    seen: dict = {}

    async def fake_query(prompt, options):
        seen["options"] = options
        if False:
            yield None

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return seen


# --- routing and overrides ------------------------------------------------------------------
@pytest.mark.parametrize("model, engine, provider", [
    ("claude-local", "claude", "subscription"),
    ("bedrock/sonnet", "claude", "bedrock"),
    ("bedrock/apac.anthropic.claude-sonnet-4-6[1m]", "claude", "bedrock"),
    ("bedrock/arn:aws:bedrock:ap-south-1:1:inference-profile/apac.anthropic.claude-opus-5-5", "claude", "bedrock"),
    ("bedrock/arn:aws:bedrock:ap-south-1::foundation-model/amazon.nova-pro-v1:0", "litellm", "bedrock"),
    ("bedrock/amazon.nova-pro-v1:0", "litellm", "bedrock"),
    ("bedrock/converse/anthropic.claude-sonnet-4-6", "litellm", "bedrock"),
    ("openai/gpt-4o-mini", "litellm", "api"),
])
def test_each_id_shape_has_one_engine_and_provider(model, engine, provider):
    eff = runtime.classify(model)
    assert (eff.engine, eff.provider) == (engine, provider)


def test_bare_names_follow_the_hub_and_named_backends_are_used_as_written():
    o = lambda base, ov: runtime._apply_model_override(base, ov)[0]  # noqa: E731
    assert o("bedrock/sonnet", "OPUS") == "bedrock/opus"
    assert o("bedrock/sonnet", "apac.anthropic.claude-haiku-4-5-20251001-v1:0") == \
        "bedrock/apac.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert o("bedrock/sonnet", "claude-local/opus") == "claude-local/opus"     # the subscription, on purpose
    assert o("bedrock/sonnet", "openai/gpt-4o") == "openai/gpt-4o"
    assert o("claude-local", "opus") == "claude-local/opus"                    # unchanged
    assert o("claude-local", "bedrock/opus") == "bedrock/opus"                 # Bedrock, on purpose


def test_the_hub_model_set_only_in_agents_md_is_found(tmp_path):
    hub = _hub(tmp_path, "AWS_REGION=ap-south-1\n")
    text = (hub / "AGENTS.md").read_text()
    (hub / "AGENTS.md").write_text(text.replace("---\n", "---\nmodel: bedrock/sonnet\n", 1))
    assert runtime.effective_model(hub, "opus").model == "bedrock/opus"
    from hubzoid.admin import diagnostics

    assert diagnostics.hub_model(hub) == "bedrock/sonnet"


def test_an_application_profile_is_routed_by_its_backing_model(monkeypatch):
    import boto3

    profile = "bedrock/arn:aws:bedrock:ap-south-1:1:application-inference-profile/abc"
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Profiles("anthropic.claude-sonnet-4-6"))
    assert runtime.classify(profile).engine == "claude"
    bedrock._profiles.clear()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Profiles("amazon.nova-pro-v1:0"))
    assert runtime.classify(profile).engine == "litellm"
    bedrock._profiles.clear()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Profiles(None))
    with pytest.raises(bedrock.BedrockError, match="GetInferenceProfile"):
        runtime.classify(profile)


# --- region --------------------------------------------------------------------------------
def test_region_order_matches_litellm_and_a_hub_without_one_does_not_start(tmp_path, monkeypatch):
    import boto3

    env = {"AWS_REGION": "eu-west-1", "AWS_REGION_NAME": "ap-south-1", "AWS_DEFAULT_REGION": "us-east-1"}
    assert bedrock.region("bedrock/sonnet", env) == ("ap-south-1", "AWS_REGION_NAME")
    arn_model = "bedrock/arn:aws:bedrock:us-east-2:1:inference-profile/us.anthropic.claude-opus-5-5"
    assert bedrock.region(arn_model, env)[0] == "us-east-2"

    class Session:
        def __init__(self, profile_name=None):
            self.region_name = "ap-southeast-2" if profile_name == "syd" else None

    monkeypatch.setattr(boto3.session, "Session", Session)
    assert bedrock.region("bedrock/sonnet", {"AWS_PROFILE": "syd"}) == ("ap-southeast-2", "the AWS profile syd")
    with pytest.raises(bedrock.BedrockError, match="AWS_REGION"):
        bedrock.require_region("bedrock/sonnet", {})
    hub = _hub(tmp_path, "MODEL=bedrock/sonnet\n")
    with pytest.raises(bedrock.BedrockError, match="needs an AWS region"):
        runtime.build(hub)


def test_tiers_use_the_region_prefix_unless_the_operator_chooses(monkeypatch):
    pins = bedrock.tier_pins("ap-south-1", {})
    assert pins["opus"] == "apac.anthropic.claude-opus-5-5"
    assert bedrock.tier_pins("us-gov-west-1", {})["sonnet"].startswith("us-gov.")
    assert bedrock.tier_pins("ca-central-1", {})["sonnet"].startswith("global.")
    chosen = bedrock.tier_pins("ap-south-1", {"ANTHROPIC_BEDROCK_REGION_PREFIX": "global",
                                              "ANTHROPIC_DEFAULT_HAIKU_MODEL": "my.haiku"})
    assert chosen["sonnet"].startswith("global.") and chosen["haiku"] == "my.haiku"


# --- the Claude child's environment, as it reaches query() ------------------------------------
def _bedrock_child(env: dict) -> None:
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["AWS_REGION"] == "ap-south-1"
    assert env["CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK"] == "1"
    for blank in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "ANTHROPIC_API_KEY"):
        assert env[blank] == ""
    for kept in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
        assert env.get(kept, "kept") != ""          # never blanked for a Bedrock child


def test_call_llm_and_stations_on_bedrock_get_aws_only_and_no_failover(monkeypatch):
    from hubzoid import factory_claude

    os.environ.update(PARENT, AWS_REGION="ap-south-1")
    seen = _capture(monkeypatch)
    # With backup tokens the failover pool needs a hub directory: hub_dir=None
    # running at all proves a Bedrock call never builds the pool.
    asyncio.run(factory_claude.claude_complete("hi", model_setting="bedrock/opus"))
    _bedrock_child(seen["options"].env)
    assert seen["options"].model == "opus"
    assert seen["options"].env["ANTHROPIC_MODEL"] == "apac.anthropic.claude-opus-5-5"
    asyncio.run(factory_claude.claude_station("go", instructions="i", registry={}, schema=None,
                                              model_setting="bedrock/sonnet"))
    _bedrock_child(seen["options"].env)
    with pytest.raises(ValueError, match="Hub directory"):     # the subscription still fails over
        asyncio.run(factory_claude.claude_complete("hi", model_setting="claude-local"))


def test_chat_on_a_bedrock_hub_and_mixed_overrides(tmp_path, monkeypatch):
    os.environ.update(PARENT)
    hub = _hub(tmp_path, "MODEL=bedrock/sonnet\nAWS_REGION=ap-south-1\n")
    rt = runtime.build(hub)
    assert rt._options.model == "sonnet"
    _bedrock_child(rt._options.env)
    rt._otel_endpoint = "http://127.0.0.1:4318"                # per-turn clone keeps it
    _bedrock_child(rt._options_for_turn().env)
    # A task that names the subscription runs on it, with no AWS credentials.
    sub = runtime.build(hub, model="claude-local/opus").__dict__["_options"]
    assert sub.model == "opus" and "CLAUDE_CODE_USE_BEDROCK" not in sub.env
    assert sub.env.get("AWS_ACCESS_KEY_ID") == "" and "CLAUDE_CODE_OAUTH_TOKEN" not in sub.env


def test_a_claude_local_hub_can_send_one_task_to_bedrock(tmp_path):
    os.environ.update(PARENT)
    hub = _hub(tmp_path, "MODEL=claude-local\nAWS_REGION=ap-south-1\n")
    plain = runtime.build(hub)._options.env
    assert "CLAUDE_CODE_USE_BEDROCK" not in plain and plain["AWS_ACCESS_KEY_ID"] == ""
    _bedrock_child(runtime.build(hub, model="bedrock/opus")._options.env)


def test_call_llm_follows_the_hub_and_litellm_gets_the_region(tmp_path, monkeypatch):
    import litellm

    from hubzoid import factory_claude

    hub = _hub(tmp_path, "MODEL=bedrock/sonnet\nAWS_REGION=ap-south-1\n")
    from hubzoid import settings

    settings.load(hub)
    got = {}

    async def fake_complete(prompt, **kw):
        got["claude"] = kw["model_setting"]
        return "ok", {"model": kw["model_setting"]}

    monkeypatch.setattr(factory_claude, "claude_complete", fake_complete)
    runtime.complete_once(hub, {"prompt": "hi", "model": "opus"})
    assert got["claude"] == "bedrock/opus"

    class Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]
        usage = None

    async def fake_acompletion(**kw):
        got["litellm"] = kw
        return Resp()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    runtime.complete_once(hub, {"prompt": "hi", "model": "bedrock/amazon.nova-pro-v1:0"})
    assert got["litellm"]["aws_region_name"] == "ap-south-1"


# --- delegates, stations, chat model choices, pins -----------------------------------------
def test_delegates_match_engine_and_provider_in_both_directions():
    assert handover.classify("haiku", "bedrock/sonnet") == "delegate"
    assert handover.resolve_tier(handover.follow("haiku", "bedrock/sonnet")) == "haiku"
    assert handover.classify("claude-local/haiku", "bedrock/sonnet") == "skill"
    assert handover.classify("bedrock/haiku", "claude-local") == "skill"
    assert handover.classify("haiku", "claude-local") == "skill"                    # as before
    assert handover.classify("claude-local/haiku", "claude-local") == "delegate"    # as before


def test_a_station_follows_a_bedrock_hub_unless_it_names_its_engine(tmp_path):
    from hubzoid.stations import definitions

    hub = _hub(tmp_path, "MODEL=bedrock/sonnet\nAWS_REGION=ap-south-1\n")
    folder = hub / "agents" / "checker"
    folder.mkdir(parents=True, exist_ok=True)

    def station(front: str):
        (folder / "AGENTS.md").write_text(f"---\nname: checker\ndescription: d\nisolated: true\n{front}---\nCheck.\n")
        return definitions.load(hub, "checker")

    assert station("model: opus\n").model_setting() == "bedrock/opus"
    assert station("engine: claude-local\nmodel: opus\n").model_setting() == "claude-local/opus"
    with pytest.raises(Exception, match="Claude model"):
        station("engine: bedrock\nmodel: amazon.nova-pro-v1:0\n")


def test_chat_model_choices_lose_the_prefix_or_are_refused():
    from hubzoid.factory_claude import turn_model

    assert turn_model("bedrock/opus", "bedrock/sonnet") == "opus"
    assert turn_model("apac.anthropic.claude-opus-5-5", "bedrock/sonnet") == "apac.anthropic.claude-opus-5-5"
    assert turn_model("claude-local/opus", "claude-local") == "opus"
    assert turn_model("sonnet", "claude-local") == "sonnet"
    for chosen, hub in (("claude-local/opus", "bedrock/sonnet"), ("bedrock/opus", "claude-local"),
                        ("bedrock/amazon.nova-pro-v1:0", "bedrock/sonnet")):
        with pytest.raises(RuntimeError, match="Choose another model"):
            turn_model(chosen, hub)


def test_bedrock_pins_are_accepted_only_on_bedrock():
    from hubzoid.factory_claude import _validate_model_pin

    pin = "apac.anthropic.claude-sonnet-4-6[1m]"
    assert _validate_model_pin(pin, hub="h") == "sonnet"
    assert _validate_model_pin(pin, hub="h", bedrock=True) == pin
    assert _validate_model_pin("arn:aws:bedrock:ap-south-1:1:inference-profile/x", hub="h", bedrock=True) \
        .startswith("arn:")
    assert _validate_model_pin("opus-typo", hub="h", bedrock=True) == "sonnet"


def test_cache_marks_only_bedrock_families_that_cache():
    from hubzoid.caching import _supports_cache_control

    assert _supports_cache_control("bedrock/amazon.nova-pro-v1:0")
    assert not _supports_cache_control("bedrock/meta.llama3-70b-instruct-v1:0")
    assert _supports_cache_control("anthropic/claude-haiku-4-5")


# --- cost -------------------------------------------------------------------------------------
def test_a_bedrock_row_names_the_provider_and_keeps_an_unknown_cost(tmp_path, monkeypatch):
    from hubzoid import usage
    from hubzoid.factory_claude import claude_usage

    msg = type("R", (), {"usage": {"input_tokens": 5, "cache_read_input_tokens": 10, "output_tokens": 2},
                         "total_cost_usd": None, "model_usage": {"apac.anthropic.claude-sonnet-5-5": {}},
                         "num_turns": 1})()
    row = claude_usage(msg, None, "bedrock/sonnet")
    assert row["model"] == "bedrock/apac.anthropic.claude-sonnet-5-5" and row["input_tokens"] == 15
    assert row["cost_usd"] is None
    monkeypatch.setattr(usage, "estimate_cost", lambda *a: pytest.fail("a Bedrock Claude cost is never estimated"))
    usage.record(tmp_path, hub="h", surface="api", kind="chat", subject=None, model=row["model"],
                 input_tokens=15, output_tokens=2, cost_usd=None)
    assert claude_usage(msg, None, "claude-local")["model"] == "apac.anthropic.claude-sonnet-5-5"


# --- doctor -----------------------------------------------------------------------------------
class _Aws:
    def __init__(self, missing=(), denied=False):
        self.missing, self.denied = set(missing), denied

    def get_caller_identity(self):
        return {"Arn": "arn:aws:iam::1:role/hub", "Account": "1"}

    def _err(self, code):
        exc = RuntimeError(code)
        exc.response = {"Error": {"Code": code}}
        return exc

    def get_inference_profile(self, inferenceProfileIdentifier):
        if self.denied:
            raise self._err("AccessDeniedException")
        if inferenceProfileIdentifier in self.missing:
            raise self._err("ResourceNotFoundException")
        return {}

    get_foundation_model = get_inference_profile

    def get_paginator(self, _name):
        raise self._err("AccessDeniedException")


def test_doctor_checks_region_identity_and_only_the_tiers_the_hub_uses(monkeypatch):
    import boto3

    os.environ["AWS_REGION"] = "ap-south-1"
    aws = _Aws(missing={"apac.anthropic.claude-haiku-4-5-20251001-v1:0"})
    monkeypatch.setattr(boto3, "client", lambda *a, **k: aws)
    out = dict(bedrock.check("bedrock/sonnet"))
    assert "fail" not in out and any("not found" in t for st, t in bedrock.check("bedrock/sonnet") if st == "warn")
    assert any(st == "fail" for st, _t in bedrock.check("bedrock/sonnet", used_tiers={"haiku"}))
    aws.denied = True
    assert [st for st, t in bedrock.check("bedrock/sonnet") if t.startswith("model:")] == ["warn"]
    del os.environ["AWS_REGION"]
    monkeypatch.setattr(boto3.session, "Session", lambda profile_name=None: type("S", (), {"region_name": None})())
    assert bedrock.check("bedrock/sonnet")[0][0] == "fail"


# --- after review: mixed calls, catalog, routes, doctor -----------------------------------
def test_a_subscription_call_on_a_bedrock_hub_carries_nothing_of_bedrock(monkeypatch):
    from hubzoid import factory_claude

    os.environ.update(PARENT, MODEL="bedrock/sonnet", AWS_REGION="ap-south-1", CLAUDE_CODE_USE_BEDROCK="1")
    seen = _capture(monkeypatch)
    # No failover pool without a hub directory. Not monkeypatch: its undo runs
    # after _clean has put the real environment back, and would leak the token.
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY")
    asyncio.run(factory_claude.claude_complete("hi", model_setting="claude-local/opus"))
    env = seen["options"].env
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "" and env["AWS_ACCESS_KEY_ID"] == ""
    assert env["ANTHROPIC_API_KEY"] == "" and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    os.environ["MODEL"] = "claude-local"                          # the older manual setup: kept as before
    asyncio.run(factory_claude.claude_complete("hi", model_setting="claude-local/opus"))
    assert "CLAUDE_CODE_USE_BEDROCK" not in seen["options"].env and "AWS_ACCESS_KEY_ID" not in seen["options"].env


def test_a_subscription_call_on_a_bedrock_hub_without_a_token_fails_clearly(monkeypatch):
    from hubzoid import factory_claude

    os.environ.update(MODEL="bedrock/sonnet", AWS_REGION="ap-south-1", ANTHROPIC_API_KEY="sk-ant")
    _capture(monkeypatch)
    with pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN"):     # never the key, never Bedrock
        asyncio.run(factory_claude.claude_complete("hi", model_setting="claude-local/opus"))


def test_chat_choices_on_another_engine_are_refused():
    from hubzoid.factory_claude import turn_model

    for chosen in ("openai/gpt-4o", "codex-local", "apac.amazon.nova-pro-v1:0"):
        with pytest.raises(RuntimeError, match="Choose another model"):
            turn_model(chosen, "bedrock/sonnet")
    assert turn_model("opus", "bedrock/sonnet") == "opus"
    with pytest.raises(RuntimeError):
        turn_model("openai/gpt-4o", "claude-local")


def test_routes_india_profiles_and_doctor_lookups(monkeypatch):
    import boto3

    assert runtime.classify("bedrock/in.anthropic.claude-sonnet-5").engine == "claude"
    route = "bedrock/converse/arn:aws:bedrock:us-east-2:1:inference-profile/us.anthropic.claude-sonnet-5-5"
    assert bedrock.region(route, {})[0] == "us-east-2"
    os.environ["AWS_REGION"] = "ap-south-1"
    aws = _Aws(missing={"apac.amazon.nova-micro-v1:0"})
    monkeypatch.setattr(boto3, "client", lambda *a, **k: aws)
    assert any(st == "fail" for st, _t in bedrock.check("bedrock/converse/apac.amazon.nova-micro-v1:0"))


def test_doctor_counts_only_stations_on_bedrock(tmp_path):
    from hubzoid import doctor

    hub = _hub(tmp_path, "MODEL=bedrock/sonnet\nAWS_REGION=ap-south-1\n")
    folder = hub / "agents" / "checker"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "AGENTS.md").write_text("---\nname: checker\ndescription: d\nisolated: true\nengine: claude-local\n"
                                      "model: opus\n---\nCheck.\n")
    assert "opus" not in doctor.bedrock_used_tiers(hub)
