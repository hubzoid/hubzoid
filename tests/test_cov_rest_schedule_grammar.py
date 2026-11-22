"""Schedule parsing refusals and human-readable monthly and yearly times."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hubzoid.workflows import schedule_grammar as grammar


@pytest.mark.parametrize('phrase,message', [('daily noon', 'cannot parse time'),
    ('daily 24:01', 'time out of range'), ('61 * * * *', 'invalid cron'),
    ('every 7 minutes', 'minute interval must divide'), ('every 5 hours', 'hour interval must divide')])
def test_invalid_schedules_explain_the_problem(phrase, message):
    with pytest.raises(grammar.ScheduleError, match=message):
        grammar.to_cron(phrase)


@pytest.mark.parametrize('phrase,description', [
    ('every 1 minute', 'Every minute'), ('every 1 hour', 'Every hour'),
    ('every 2 hours', 'Every 2 hours'), ('daily 12am', 'Daily at 00:00'),
    ('daily 12pm', 'Daily at 12:00'), ('0 9 * * 1-5', 'Weekdays at 09:00'),
    ('0 9 5 * *', 'Day 5 of each month at 09:00'),
    ('30 6 7 11 *', 'Every year on 7 November at 06:30'),
    ('*/5 9-17 * * 1-5', '*/5 9-17 * * 1-5')])
def test_schedules_have_unambiguous_descriptions(phrase, description):
    assert grammar.describe(phrase) == description


def test_naive_start_is_utc_and_empty_window_is_not_due():
    start = datetime(2026, 1, 1)
    assert grammar.next_after('every 1 hour', None, start) == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert grammar.due_between('every 1 hour', None, start, start) is False


def test_invalid_zone_is_reported_by_next_fire():
    from zoneinfo import ZoneInfoNotFoundError

    with pytest.raises(ZoneInfoNotFoundError):
        grammar.shortest_gap('daily 6am', 'Invalid/Zone')
