"""The draft evaluation harness runs end to end with synthetic providers. This proves its
structure and scoring, not live answer quality (that needs a live run and human review)."""
import json

from sqlalchemy import func, select

from app import db, draft_eval
from app.models import Account


def test_harness_compares_three_systems_on_fictional_cases(database, tmp_path):
    cases = 'housing_cross_chat,gmail_only,user_correction,confidential_news,prompt_injection'
    assert draft_eval.main(['--generations', '1', '--cases', cases, '--out', str(tmp_path)]) == 0
    result = json.loads((tmp_path / 'draft-eval.json').read_text())
    by = {(run['case'], r['system']): r for run in result['runs'] for r in run['results']}
    assert all('error' not in r for r in by.values())
    # The previous system cannot see the other chat or the email; the evidence pipeline can.
    for case in ('housing_cross_chat', 'gmail_only'):
        assert by[(case, 'evidence')]['score']['facts'] and not by[(case, 'previous')]['score']['facts']
        assert by[(case, 'voice')]['score']['facts'] is False
    assert by[('user_correction', 'evidence')]['score']['facts'] and not by[('user_correction', 'evidence')]['score']['leaks']
    assert not by[('confidential_news', 'evidence')]['score']['leaks']
    assert not by[('prompt_injection', 'evidence')]['score']['leaks']
    assert by[('housing_cross_chat', 'evidence')]['score']['greeting'] is True  # voice examples reach the writer
    assert by[('housing_cross_chat', 'previous')]['score']['greeting'] is False
    summary = result['summary']
    assert set(summary) == {'previous', 'voice', 'evidence'} and summary['evidence']['calls_per_draft'] >= 2
    report = (tmp_path / 'draft-eval.md').read_text()
    assert '| evidence |' in report and 'human review' in report
    with db.session() as s:  # one throwaway workspace per case
        assert s.scalar(select(func.count()).select_from(Account).where(Account.email.like('eval+%'))) == 5


def test_live_runs_refuse_the_configured_database(database, monkeypatch):
    assert draft_eval.main(['--database', 'moresocial', '--cases', 'gmail_only']) == 2
    assert draft_eval.main(['--database', 'moresocial_eval_ok; DROP', '--cases', 'gmail_only']) == 2


def test_placeholders_are_not_assertions_or_evidence():
    case = next(c for c in draft_eval.CASES if c.name == 'open_obligation')
    result = draft_eval.score(case, "Hoi Jonas! [your answer about whether I've found a permanent place in Utrecht yet]")
    assert result['placeholders'] == 1 and result['leaks'] == []
    housing = next(c for c in draft_eval.CASES if c.name == 'housing_cross_chat')
    assert not draft_eval.score(housing, '[until 15 October?]')['facts']


def test_evaluation_rate_limit_recovery_is_bounded_and_restores_calls(monkeypatch):
    from app import ai
    calls, waits = [], []
    def limited(*args, **kwargs):
        calls.append(1)
        raise ai.rate_limited(2)
    monkeypatch.setattr(ai, 'generate', limited)
    monkeypatch.setattr(draft_eval.time, 'sleep', waits.append)
    import pytest
    with draft_eval.paced_provider(0) as state:
        with pytest.raises(ai.AIUnavailable, match='rate_limited'):
            ai.generate()
    assert len(calls) == 4 and waits == [3, 3, 3]
    assert state['wait_ms'] == 9000 and ai.generate is limited
