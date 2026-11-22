"""An agent's `guardrails:` frontmatter (hubzoid.guardrails.parse): each value
it accepts, and each mistake refused with a message that names it (bad or
over-long patterns, one that matches empty text, the model and threshold)."""
from __future__ import annotations

import pytest

from hubzoid import guardrails


def test_off_and_a_full_config():
    assert guardrails.parse(None) is None and guardrails.parse(False) is None
    cfg = guardrails.parse({"prompt_injection": "block", "personal_data": "redact",
                            "personal_data_types": ["email", "email"],
                            "personal_data_patterns": {" order id ": r"ORD-\d{6}"},
                            "threshold": 0.6, "model": " haiku ", "on_error": "block"})
    assert cfg.prompt_injection == "block" and cfg.personal_data == "redact"
    assert cfg.personal_data_types == ("email",) and set(cfg.patterns) == {"order id"}
    assert cfg.patterns["order id"].search("see ORD-123456") is not None
    assert cfg.threshold == 0.6 and cfg.model == "haiku" and cfg.on_error == "block"


@pytest.mark.parametrize("raw,message", [
    ("block", "is a mapping"),
    ({"surprise": 1}, "unknown key(s) surprise"),
    ({"personal_data_types": "email"}, "personal_data_types is a list of"),
    ({"personal_data_types": ["fingerprint_scan"]}, "personal_data_types is a list of"),
    ({"personal_data_patterns": ["x"]}, "is a mapping of name: regex"),
    ({"personal_data_patterns": {"": "x"}}, "is a mapping of name: regex"),
    ({"personal_data_patterns": {"id": "x" * 600}}, "is longer than"),
    ({"personal_data_patterns": {"id": "(unclosed"}}, "is not a valid regex"),
    ({"personal_data_patterns": {"id": "a*"}}, "matches empty text"),
    ({"threshold": 0}, "threshold is a number above 0 and at most 1"),
    ({"threshold": True}, "threshold is a number above 0 and at most 1"),
    ({"model": "  "}, "model is a model name"),
    ({"on_error": "ignore"}, "on_error is allow or block"),
])
def test_each_mistake_is_named(raw, message):
    with pytest.raises(ValueError) as err:
        guardrails.parse(raw)
    assert message in str(err.value)
