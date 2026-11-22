"""budgets.yaml (hubzoid.budgets.parse): the spending and token limits a hub
sets. A cost limit is accepted exactly when it is an amount of 0 or more with
at most 6 decimals (a property test); every other mistake is refused with
the file and line."""
from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hubzoid import budgets
from hubzoid.budgets import BudgetError


@given(units=st.integers(-10**9, 10**12), places=st.integers(0, 9))
def test_a_cost_limit_is_an_amount_with_at_most_six_decimals(units, places):
    amount = Decimal(units).scaleb(-places)
    text = f'version: 1\nlimits:\n  month:\n    max_cost_usd: "{amount}"\n'
    ok = amount >= 0 and amount.as_tuple().exponent >= -6
    if ok:
        assert budgets.parse(text).limits == {"month": {"max_cost_usd": amount}}
    else:
        with pytest.raises(BudgetError, match="max_cost_usd is an amount"):
            budgets.parse(text)


def test_a_valid_file():
    cfg = budgets.parse('version: 1\nlimits:\n  run: {max_turns: 20, max_cost_usd: "0.50"}\n'
                        "  work_item: {max_output_tokens: 50000}\n")
    assert cfg.present and len(cfg.revision) == 16
    assert cfg.limits == {"run": {"max_turns": 20, "max_cost_usd": Decimal("0.50")},
                          "work_item": {"max_output_tokens": 50000}}
    with pytest.raises(BudgetError, match="version must be 1"):    # an empty file still needs its version
        budgets.parse("")


@pytest.mark.parametrize("text,message", [
    ("version: 1\nlimits: [x]\n", "limits must be a mapping"),
    ("- a list\n", "the file must be a mapping"),
    ("version: 2\n", "version must be 1"),
    ("version: 1\nprices: {}\n", "Per-person, per-agent and price settings are not supported"),
    ("version: 1\nsurprise: 1\n", "unknown key 'surprise'"),
    ("version: 1\nlimits:\n  week: {max_turns: 1}\n", "a scope is one of run, work_item, month"),
    ("version: 1\nlimits:\n  month: 5\n", "month must be a mapping"),
    ("version: 1\nlimits:\n  month: {max_turns: 3}\n", "month takes max_input_tokens"),
    ("version: 1\nlimits:\n  run: {max_turns: 0}\n", "max_turns is a whole number of 1 or more"),
    ("version: 1\nlimits:\n  run: {max_turns: true}\n", "max_turns is a whole number of 1 or more"),
    ("version: 1\nlimits:\n  run: {max_cost_usd: abc}\n", "max_cost_usd is an amount"),
    ("version: 1\nlimits:\n  run: {max_cost_usd: true}\n", "max_cost_usd is an amount"),
    ("version: 1\nlimits: {run: [\n", "not valid YAML"),
])
def test_each_mistake_is_named_with_its_file_and_line(text, message):
    with pytest.raises(BudgetError) as err:
        budgets.parse(text, source="budgets.yaml")
    assert message in str(err.value) and str(err.value).startswith("budgets.yaml:")
