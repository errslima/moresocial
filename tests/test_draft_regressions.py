"""Regression cases from the 2026-09-29 evidence-draft review; fictional data only."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app import ai, db, draft_evidence as de, memory, retrieval, suggestions
from app.models import Chunk, ChunkSource, Suggestion
from app.repo import Scoped
from draft_fixtures import IRIS, mail, msg
from test_draft_evidence import world, bundle_of, current, evidence_state
from test_draft_evidence import save, row_of
from app.models import Claim, ClaimSource, DraftEvidenceBundle
import pytest


def test_negated_answer_is_not_a_paraphrase():
    base = dict(question='Q1', status='supported', decision='none', deadline=None,
                effective_to=None, disclosure='share')
    old = [{**base, 'answer': 'I can stay with Iris in October.'}]
    new = [{**base, 'answer': 'I cannot stay with Iris in October.'}]
    assert not de._same_answers(old, new)


def test_changed_effective_start_is_not_unchanged():
    base = dict(question='Q1', status='supported', decision='accepted', deadline=None,
                effective_to='2026-12-31', disclosure='share', answer='I can stay with Iris.')
    assert not de._same_answers([{**base, 'effective_from':'2026-10-01'}],
                               [{**base, 'effective_from':'2026-11-01'}])


def test_acknowledged_draft_still_checks_future_changes(world):
    wid, ids, sid = world
    suggestions.draft(wid, sid)
    with db.session() as s:
        ws = Scoped(s, wid)
        row = ws.get(Suggestion, sid)
        bundle = de.bundle_for(ws, row)
        bundle.freshness = 'outdated'
        row.draft_origin = 'user'
        de.acknowledge(ws, row)
    assert evidence_state(wid, sid) == 'acknowledged'
    assert current(wid, sid)
    with db.session() as s:
        msg(Scoped(s, wid), IRIS, 'The arrangement has changed again; you cannot stay here.',
            datetime.now(timezone.utc), IRIS)
    assert not current(wid, sid), 'Acknowledgment must apply only to the reviewed evidence revision'


def test_long_email_uses_the_matching_segment(world, monkeypatch):
    wid, ids, sid = world
    marker = 'Your Zephyr booking confirmation is ZQ7491.'
    with db.session() as s:
        src = mail(Scoped(s, wid), 'Travel receipt', 'General terms and conditions. ' * 300 + marker,
                   datetime.now(timezone.utc) - timedelta(days=2))
        source_id, conversation = src.id, src.conversation_id
    memory.rebuild_conversation(wid, 'gmail', conversation)
    with db.session() as s:
        cid = s.scalar(select(ChunkSource.chunk_id).where(ChunkSource.source_id == source_id,
                                                        ChunkSource.segment_text.contains(marker)))
        assert cid is not None
        monkeypatch.setattr(retrieval, 'hybrid', lambda *a, **k:
                            retrieval.Ranked([(cid, 1.0, frozenset({'keyword'}))], 0, 1, {}))
        result = de.gather(Scoped(s, wid), [{'id':'Q1','text':'What is the Zephyr booking confirmation?',
                            'query':'Zephyr booking confirmation','kind':'fact'}], [], {}, ai.embedder(),
                            recipient_id=s.get(Suggestion, sid).person_id)
        assert marker in '\n'.join(p.text for p in result['passages'])


def test_claim_inherits_confidentiality_from_its_source():
    needs = [{'id':'Q1','kind':'fact','text':'Where will you stay?'}]
    passage = SimpleNamespace(label='F1', confidential=True)
    claims = [{'label':'C1','status':'active','supported_by':['F1']}]
    answer = dict(question='Q1', status='supported', answer='I will stay with Iris.', decision='accepted',
                  statement_date=None,effective_from=None,effective_to=None,deadline=None,
                  support=['C1'],conflicts=[],disclosure='share',reason='ok',note='')
    result = de.validate_resolution({'answers':[answer]}, needs, [passage], claims)
    assert result[0]['disclosure'] == 'withhold'


def test_claim_source_confidentiality_is_loaded_without_a_retrieved_passage(world):
    wid, ids, sid = world
    import uuid
    with db.session() as s:
        ws = Scoped(s, wid)
        c = ws.add(Claim(id=uuid.uuid4(), subject='self', category='commitment', value='I will stay with Iris',
                         basis='explicit', status='active', origin='extraction', claim_key=uuid.uuid4().hex))
        s.flush()
        from app.models import Source
        src = ws.get(Source, ids['secret'])
        ws.add(ClaimSource(id=uuid.uuid4(), claim_id=c.id, source_id=src.id, source_version=src.version))
        s.flush()
        assert de.claim_lines(ws, [c], [])[0]['confidential'] is True


def test_acknowledged_draft_revalidates_again_after_second_change(world, browser):
    wid, ids, sid = world
    suggestions.draft(wid, sid)
    with db.session() as s:
        msg(Scoped(s, wid), IRIS, "I can't host you after all.", datetime.now(timezone.utc), IRIS)
    from worker.main import drain
    drain()
    bid = bundle_of(wid, sid).id
    assert de.revalidate(wid, bid) == 'outdated'
    assert save(browser, sid, 'Hoi Jonas! [where I will stay]').status_code == 303
    assert current(wid, sid)
    with db.session() as s:
        msg(Scoped(s, wid), IRIS, 'Another housing change to discuss.', datetime.now(timezone.utc), IRIS)
        de.mark_changed(Scoped(s, wid))
    assert not current(wid, sid)
    with db.session() as s:
        assert de.schedule(s) == 1
    assert de.revalidate(wid, bid) in ('refreshed', 'unchanged', 'outdated')


def test_usage_metrics_include_truncated_resolution_retry(world, monkeypatch):
    wid, ids, sid = world
    provider = ai.provider()
    original = provider.generate
    failed = False
    def generate(**kwargs):
        nonlocal failed
        if kwargs['context'].get('purpose') == 'draft_resolve' and not failed:
            failed = True
            raise ai.InvalidOutput('truncated', 101, 52)
        return original(**kwargs)
    monkeypatch.setattr(provider, 'generate', generate)
    suggestions.draft(wid, sid)
    metrics = bundle_of(wid, sid).payload['metrics']
    assert metrics['calls'] == 5 and metrics['embedding_calls'] == 1 and metrics['failed_calls'] == 1
    assert metrics['cost_known'] is False
