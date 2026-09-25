"""M2: bounded, resumable, idempotent Google sync against the synthetic Google API."""
import dataclasses

import pytest
from sqlalchemy import func, select

from app import config, db, google_sync, synthetic
from app.models import Claim, Chunk, Job, Source, SyncStream
from conftest import drain, workspace_of
from worker.main import schedule


def sync():
    schedule()
    return drain()


def sources(email, provider=None, include_deleted=False):
    with db.session() as s:
        q = select(Source).where(Source.workspace_id == workspace_of(email))
        if provider:
            q = q.where(Source.provider == provider)
        if not include_deleted:
            q = q.where(Source.deleted_at.is_(None))
        return list(s.scalars(q))


def stream(email, name):
    with db.session() as s:
        return s.scalars(select(SyncStream).where(SyncStream.workspace_id == workspace_of(email),
                                                  SyncStream.stream == name)).one()


def make_due(email):
    with db.session() as s:
        for st in s.scalars(select(SyncStream).where(SyncStream.workspace_id == workspace_of(email))):
            if st.status != 'disabled':
                st.next_run_at = func.now()


@pytest.fixture
def limits():
    original = config.get()

    def set_(**kw):
        config.override(dataclasses.replace(original, **kw))
    yield set_
    config.override(original)


def test_initial_sync_imports_bounded_inert_sources(browser):
    browser.login('alex')
    sync()
    mail = {s.provider_object_id: s for s in sources('alex@example.test', 'gmail')}
    assert 'a-spam' not in mail  # Spam/Trash excluded
    assert set(mail) == {'a-m1', 'a-m2', 'a-m3', 'a-m4', 'a-m5', 'a-m6'}
    html = mail['a-m4']
    assert 'vegetarian' in html.body and '<' not in html.body and 'alert' not in html.body and 'tracker' not in html.body
    assert html.meta['attachments'] == 1
    assert mail['a-m2'].from_me
    events = {s.provider_object_id: s for s in sources('alex@example.test', 'calendar', include_deleted=True)}
    assert len(events) == 11  # 3 single events + 8 recurring instances (singleEvents)
    assert 'a-e3' not in events  # first seen already cancelled: never imported
    assert 'not proof of attendance' in events['a-e1'].meta['note']
    cal = stream('alex@example.test', 'calendar')
    assert cal.cursor and not cal.truncated and cal.status == 'ok'
    gm = stream('alex@example.test', 'gmail')
    assert gm.cursor and gm.status == 'ok' and not gm.truncated
    # no writes to Google anywhere
    assert all(m == 'GET' or 'oauth2' in host for m, host in synthetic.STATE.calls)


def test_repeated_sync_is_idempotent_and_does_not_reprocess(browser):
    browser.login('alex')
    sync()
    before = len(sources('alex@example.test', include_deleted=True))
    with db.session() as s:
        jobs_before = s.scalar(select(func.count()).select_from(Job))
        chunks_before = s.scalar(select(func.count()).select_from(Chunk))
    make_due('alex@example.test')
    sync()
    assert len(sources('alex@example.test', include_deleted=True)) == before
    with db.session() as s:
        new_jobs = s.scalars(select(Job.kind).where(Job.kind.not_in(['sync_gmail', 'sync_calendar']))).all()
        assert s.scalar(select(func.count()).select_from(Job)) - jobs_before == 2  # just the two sync jobs
        assert s.scalar(select(func.count()).select_from(Chunk)) == chunks_before


def test_interrupted_sync_resumes_from_saved_page(browser, limits):
    browser.login('alex')
    wid = workspace_of('alex@example.test')
    st = stream('alex@example.test', 'calendar')
    synthetic.STATE.fail_next = []
    g = google_sync.provider()
    calls = {'n': 0}
    original = g.calendar_window

    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] == 2:
            raise google_sync.GoogleError('unavailable', 503)
        return original(*a, **k)
    g.calendar_window = flaky
    with pytest.raises(google_sync.GoogleError):
        google_sync.run(wid, st.id)
    saved = stream('alex@example.test', 'calendar')
    assert saved.progress['page'] == '5' and saved.items_seen == 5 and saved.status == 'error'
    assert saved.cursor is None  # no cursor before all pages are processed
    google_sync.run(wid, st.id)
    done = stream('alex@example.test', 'calendar')
    assert done.cursor and done.items_seen == 12
    assert len(sources('alex@example.test', 'calendar', include_deleted=True)) == 11


def test_worker_crash_lease_recovery(browser):
    browser.login('alex')
    schedule()
    from app import jobs
    with db.session() as s:
        job = jobs.claim(s, 'crashed-worker', ['sync_calendar'], lease=1)
        assert job is not None
    import time
    time.sleep(1.2)
    drain()
    assert stream('alex@example.test', 'calendar').status == 'ok'
    with db.session() as s:
        assert s.scalars(select(Job.status).where(Job.kind == 'sync_calendar')).one() == 'done'


def test_gmail_history_increment_adds_and_deletes(browser):
    browser.login('alex')
    sync()
    synthetic.STATE.extra_mail['alex'] = [dict(id='a-m7', thread='a-t1', days=-1, frm='Sam Jansen <sam.jansen@example.test>',
                                               to='alex@example.test', subject='Re: Lasergame?', body="Count me in for Saturday!")]
    synthetic.STATE.deleted_mail['alex'] = {'a-m3'}
    make_due('alex@example.test')
    sync()
    mail = {s.provider_object_id for s in sources('alex@example.test', 'gmail')}
    assert 'a-m7' in mail and 'a-m3' not in mail
    deleted = [s for s in sources('alex@example.test', 'gmail', include_deleted=True) if s.provider_object_id == 'a-m3'][0]
    assert deleted.deleted_at is not None and deleted.body == ''


def test_invalid_gmail_cursor_triggers_bounded_resync_without_inferring_deletions(browser):
    browser.login('alex')
    sync()
    synthetic.STATE.invalid_history = True
    make_due('alex@example.test')
    sync()
    assert len(sources('alex@example.test', 'gmail')) == 6
    assert stream('alex@example.test', 'gmail').status == 'ok'


def test_calendar_incremental_and_gone_token(browser):
    browser.login('alex')
    sync()
    synthetic.STATE.cancelled_events['alex'] = {'a-e1'}
    make_due('alex@example.test')
    sync()
    e1 = [s for s in sources('alex@example.test', 'calendar', include_deleted=True) if s.provider_object_id == 'a-e1'][0]
    assert e1.deleted_at is not None
    synthetic.STATE.invalid_sync_token = True
    make_due('alex@example.test')
    sync()  # 410 -> bounded full refresh, never combining syncToken with timeMin/timeMax
    assert stream('alex@example.test', 'calendar').status == 'ok'
    assert not any('syncToken' in h and 'timeMin' in h for _, h in synthetic.STATE.calls)


def test_truncation_is_visible_and_never_invents_a_cursor(browser, limits):
    limits(gmail_max_messages=3, calendar_max_events=4)
    browser.login('alex')
    sync()
    assert len(sources('alex@example.test', 'gmail')) == 3
    gm = stream('alex@example.test', 'gmail')
    assert gm.truncated and gm.cursor  # Gmail history cursor is still valid
    cal = stream('alex@example.test', 'calendar')
    assert cal.truncated and cal.cursor is None
    assert 'limit reached' in browser.get('/connections').text


def test_partial_consent_runs_only_granted_sync(browser):
    browser.login('alex', gmail=False)
    sync()
    assert not sources('alex@example.test', 'gmail')
    assert sources('alex@example.test', 'calendar')
    assert not any('gmail.googleapis.com' in h for _, h in synthetic.STATE.calls)


def test_revocation_stops_requests(browser):
    browser.login('alex')
    sync()
    synthetic.STATE.revoked.add('alex')
    with db.session() as s:
        from app.models import Connection
        conn = s.scalars(select(Connection)).one()
        conn.access_expires_at = func.now()
    make_due('alex@example.test')
    sync()
    with db.session() as s:
        from app.models import Connection
        assert s.scalars(select(Connection)).one().state == 'reconnect_required'
    calls = len(synthetic.STATE.calls)
    make_due('alex@example.test')
    sync()
    assert len(synthetic.STATE.calls) == calls  # nothing requested while reconnect is required


def test_throttling_backs_off(browser):
    browser.login('alex', calendar=False)
    synthetic.STATE.fail_next = [429]
    sync()
    gm = stream('alex@example.test', 'gmail')
    assert gm.status == 'error' and gm.failures == 1 and 'slow down' in gm.last_error
    from datetime import datetime, timezone
    assert gm.next_run_at > datetime.now(timezone.utc)


def test_disconnect_revokes_and_stops_future_jobs(browser):
    browser.login('alex')
    browser.post('/connections/google/disconnect', {'csrf': browser.csrf('/connections')})
    assert 'alex' in synthetic.STATE.revoked
    n = len(synthetic.STATE.calls)
    sync()
    assert len(synthetic.STATE.calls) == n
    assert sources('alex@example.test') == []
    # imported data survives disconnect until explicitly deleted
    browser.login('alex')
    sync()
    assert sources('alex@example.test')
    browser.post('/connections/google/delete-data', {'csrf': browser.csrf('/connections')})
    assert sources('alex@example.test', include_deleted=True) == []


def test_source_browsing_shows_inert_text_and_status(make_browser):
    a, b = make_browser(), make_browser()
    a.login('alex')
    b.login('blake')
    sync()
    listing = a.get('/sources').text
    assert 'Lasergame?' in listing and 'kiwi' not in listing
    src = [s for s in sources('alex@example.test', 'gmail') if s.provider_object_id == 'a-m5'][0]
    page = a.get(f'/sources/{src.id}').text
    assert 'IGNORE ALL PREVIOUS INSTRUCTIONS' in page and '<script' not in page.split('<main')[1].split('</main>')[0]
    assert b.get(f'/sources/{src.id}').status_code == 404
    status = a.get('/connections').text
    assert 'Up to date' in status and 'Last 90 days, up to 500 messages' in status
