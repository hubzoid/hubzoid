"""Workflow outcome notices, clean stops and bookkeeping failures."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid import email_delivery
from hubzoid.artifacts import series
from hubzoid.workflows import chat_link, outcomes, personal, runtime
from hubzoid.workflows.identity import IdentityError
from hubzoid.workflows.inputs import InputError


@pytest.mark.parametrize('error,cause', [(None, 'error'), (IdentityError('private'), 'access'),
    (InputError('private'), 'inputs'), (TimeoutError('private'), 'timeout'), (RuntimeError('private'), 'error')])
def test_outcome_cause_is_fixed_and_never_quotes_error(error, cause):
    assert outcomes.cause_of(error) == cause


@pytest.fixture
def recorded(monkeypatch):
    reports, bookkeeping, posts, mail = [], [], [], []
    monkeypatch.setattr(series, 'finish_run', lambda *a, **kw: reports.append((a, kw)))
    monkeypatch.setattr(personal, 'after_run', lambda *a, **kw: bookkeeping.append((a, kw)))
    monkeypatch.setattr(chat_link, 'post_result', lambda *a, **kw: posts.append((a, kw)))
    monkeypatch.setattr(email_delivery, 'send_to_owner', lambda *a, **kw: mail.append((a, kw)))
    monkeypatch.setattr(personal, 'email_on', lambda h: True)
    monkeypatch.setattr(runtime, 'title_for', lambda w: 'Daily report')
    monkeypatch.setattr(outcomes.time, 'time', lambda: 200)
    return SimpleNamespace(reports=reports, bookkeeping=bookkeeping, posts=posts, mail=mail)


def test_personal_schedule_failure_records_only_safe_cause(tmp_path, recorded):
    env = {'trigger': 'personal_schedule', 'schedule_id': 's1', 'scope': 'personal', 'chat': {'id': 'c1'}}
    outcomes.finished(tmp_path, 'desk', 'daily', 'r1', env, ok=False, error=IdentityError('secret'),
                      published=[{'url': '/report'}])
    assert recorded.reports[0][1] == {'trigger': 'personal_schedule', 'scope': 'personal',
                                     'schedule_id': 's1', 'ok': False}
    assert recorded.bookkeeping[0][1]['cause'] == 'access'
    assert recorded.posts[0][1]['cause'] == 'access' and 'secret' not in str(recorded.posts)


@pytest.mark.parametrize('stop,cause', [('approval_denied', 'denied'), ('new_stop', 'stopped')])
def test_clean_stop_posts_fixed_reason(tmp_path, recorded, stop, cause):
    outcomes.finished(tmp_path, 'desk', 'daily', 'r1', {'chat': {'id': 'c1'}}, ok=True, stopped=stop)
    assert recorded.posts[0][1]['ok'] is False and recorded.posts[0][1]['cause'] == cause


def test_parked_run_posts_no_terminal_result(tmp_path, recorded):
    outcomes.finished(tmp_path, 'desk', 'daily', 'r1', {'chat': {'id': 'c1'}}, ok=True, parked=True)
    assert len(recorded.reports) == 1 and recorded.posts == []


@pytest.mark.parametrize('published,phrase', [([], 'Ask me for the report in chat'),
    ([{'url': '/r', 'title': 'Report'}, {'url': '/r', 'title': 'Duplicate'}, {}], 'Report: /r')])
def test_late_api_report_emails_owner_once_by_run_key(tmp_path, recorded, published, phrase):
    outcomes.finished(tmp_path, 'desk', 'daily', 'r1', {
        'trigger': 'api', 'acts_as': 'a@example.org', 'acts_as_account': 'u1', 'started_at': 1},
        ok=True, published=published)
    args = recorded.mail[0][1]
    assert args['subject'] == 'Your Daily report is ready' and args['idem_key'] == 'notice:r1:ready'
    assert args['identity'].account_id == 'u1' and phrase in args['body']
    assert 'Duplicate' not in args['body']


def test_failures_in_each_bookkeeping_phase_do_not_hide_others(tmp_path, recorded, monkeypatch, caplog):
    def failed(*a, **kw):
        raise OSError('offline')

    monkeypatch.setattr(series, 'finish_run', failed)
    monkeypatch.setattr(personal, 'after_run', failed)
    monkeypatch.setattr(chat_link, 'post_result', failed)
    outcomes.finished(tmp_path, 'desk', 'daily', 'r1', {
        'trigger': 'personal_schedule', 'schedule_id': 's1', 'chat': {'id': 'c1'}}, ok=False)
    assert 'could not update report links' in caplog.text
    assert 'could not finish bookkeeping' in caplog.text and 'could not post run r1' in caplog.text
