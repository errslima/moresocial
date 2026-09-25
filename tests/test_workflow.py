"""M5: the lasergame gathering workflow through the server-rendered screens."""
import re
from datetime import timedelta

from sqlalchemy import select

from app import db, synthetic, synthetic_data
from app.models import Draft, EventPerson, Person, PersonIdentifier
from conftest import drain, workspace_of
from worker.main import schedule


def setup(browser):
    browser.login('alex')
    browser.post('/connections/whatsapp/connect', {'csrf': browser.csrf('/connections')})
    browser.get('/api/whatsapp/status.json')
    browser.get('/api/whatsapp/status.json')
    schedule()
    drain()


def person(wid, ident):
    with db.session() as s:
        return s.scalars(select(Person).join(PersonIdentifier, PersonIdentifier.person_id == Person.id)
                         .where(PersonIdentifier.workspace_id == wid, PersonIdentifier.value == ident)).one()


def test_every_screen_renders(browser):
    setup(browser)
    wid = workspace_of('alex@example.test')
    sam = person(wid, 'sam.jansen@example.test')
    for path in ['/home', '/people', '/people?q=sam', f'/people/{sam.id}', '/sources', '/sources?provider=whatsapp',
                 '/ask', '/gatherings', '/me', '/connections']:
        r = browser.get(path)
        assert r.status_code == 200, path
        assert 'name="csrf" value=""' not in r.text, path  # every form carries the session token


def test_lasergame_gathering_flow(browser):
    setup(browser)
    wid = workspace_of('alex@example.test')
    sam, noor = person(wid, 'sam.jansen@example.test'), person(wid, '31600000001@c.us')
    csrf = browser.csrf('/gatherings')
    r = browser.post('/gatherings', {'title': 'Lasergame evening', 'activity': 'lasergame', 'timezone': 'Europe/Amsterdam',
                                     'notes': 'Not on Sunday', 'csrf': csrf})
    gid = r.headers['location'].rsplit('/', 1)[1]
    # One candidate overlaps the fixture's dentist appointment, one is free (explicit UTC offsets).
    browser.post(f'/gatherings/{gid}/candidates', {'start': (synthetic_data.anchor() + timedelta(days=3, hours=10)).isoformat(),
                                                    'hours': '2', 'csrf': csrf})
    browser.post(f'/gatherings/{gid}/candidates', {'start': (synthetic_data.anchor() + timedelta(days=5, hours=12)).isoformat(),
                                                    'hours': '2', 'csrf': csrf})
    for p in (sam, noor):
        assert browser.post(f'/gatherings/{gid}/people', {'person': str(p.id), 'csrf': csrf}).status_code == 303
    page = browser.get(f'/gatherings/{gid}').text
    assert 'Dentist' in page  # conflict in the user's own imported calendar
    assert 'No conflicting events in your imported primary calendar' in page
    assert "says nothing about invitees" in page
    home = browser.get('/home').text
    assert 'Draft invitations for 2 person(s) for Lasergame evening' in home
    with db.session() as s:
        eps = {e.person_id: e for e in s.scalars(select(EventPerson).where(EventPerson.workspace_id == wid))}
    ep_sam = eps[sam.id]
    calls_before = list(synthetic.STATE.calls)
    assert browser.post(f'/gatherings/{gid}/people/{ep_sam.id}/draft', {'csrf': csrf}).status_code == 303
    page = browser.get(f'/gatherings/{gid}').text
    m = re.search(r'<textarea id="d-([0-9a-f-]+)" name="text"[^>]*>([^<]*)</textarea>', page)
    draft_id, text = m.group(1), m.group(2)
    assert 'Sam' in text and 'lasergame' in text.lower()
    assert 'data-copy="d-' + draft_id in page and 'Copying does not send anything' in page
    assert '>Send<' not in page and 'Send' not in re.sub(r'(?s)<footer.*', '', page).replace('send it yourself', '').replace(
        'Send it yourself', '').replace('does not send', '').replace('never sends', '')
    browser.post(f'/drafts/{draft_id}', {'action': 'shorter', 'text': text, 'csrf': csrf})
    browser.post(f'/drafts/{draft_id}', {'action': 'save', 'text': 'Hey Sam, lasergame on Saturday?', 'csrf': csrf})
    with db.session() as s:
        d = s.get(Draft, draft_id)
        assert d.text == 'Hey Sam, lasergame on Saturday?' and d.edited
        assert s.get(EventPerson, ep_sam.id).status == 'not_contacted'  # drafting/copying never advances state
    # user sends it themselves, then records it
    browser.post(f'/gatherings/{gid}/people/{ep_sam.id}/status', {'status': 'awaiting_reply', 'csrf': csrf})
    browser.post(f'/gatherings/{gid}/people/{eps[noor.id].id}/status', {'status': 'awaiting_reply', 'csrf': csrf})
    # Sam replies (new Gmail message arrives on next sync)
    synthetic.STATE.extra_mail['alex'] = [dict(id='a-reply', thread='a-t1', days=0, frm='Sam Jansen <sam.jansen@example.test>',
                                               to='alex@example.test', subject='Re: Lasergame', body="Count me in!")]
    from app.models import SyncStream
    with db.session() as s:
        for st in s.scalars(select(SyncStream)):
            st.next_run_at = st.last_success_at
    schedule()
    drain()
    browser.post(f'/gatherings/{gid}/check-replies', {'csrf': csrf})
    with db.session() as s:
        ep = s.get(EventPerson, ep_sam.id)
        assert ep.suggested_status == 'accepted' and ep.status == 'awaiting_reply'  # suggestion only
    page = browser.get(f'/gatherings/{gid}').text
    assert 'Possible reply' in page and 'Count me in!' in page
    browser.post(f'/gatherings/{gid}/people/{ep_sam.id}/suggestion', {'action': 'accept', 'csrf': csrf})
    with db.session() as s:
        assert s.get(EventPerson, ep_sam.id).status == 'accepted'
        # Noor has not replied since the invitation: no status is invented for her.
        assert s.get(EventPerson, eps[noor.id].id).status == 'awaiting_reply'
    # Google was only ever read
    assert all(m == 'GET' or 'oauth2' in h for m, h in synthetic.STATE.calls[len(calls_before):])


def test_next_steps_dismiss_and_snooze(browser):
    setup(browser)
    csrf = browser.csrf('/gatherings')
    r = browser.post('/gatherings', {'title': 'Lasergame', 'csrf': csrf})
    gid = r.headers['location'].rsplit('/', 1)[1]
    wid = workspace_of('alex@example.test')
    browser.post(f'/gatherings/{gid}/people', {'person': str(person(wid, 'noor@example.test').id), 'csrf': csrf})
    home = browser.get('/home').text
    key = re.search(r'name="key" value="([^"]+)"', home).group(1)
    browser.post('/home/steps', {'key': key, 'action': 'snooze', 'csrf': csrf})
    assert 'Draft invitations' not in browser.get('/home').text
    # at most three suggestions, never a score or streak
    assert browser.get('/home').text.count('class="card"') <= 3


def test_about_me_is_editable_and_observations_are_distinct(browser):
    setup(browser)
    csrf = browser.csrf('/me')
    browser.post('/me', {'preferences': 'Small groups, active plans', 'goals': 'See friends monthly', 'csrf': csrf})
    page = browser.get('/me').text
    assert 'Small groups, active plans' in page and 'See friends monthly' in page
    assert 'plan something for October' in page and 'unconfirmed' in page


def test_manual_identity_link_and_unlink(browser):
    setup(browser)
    wid = workspace_of('alex@example.test')
    noor_mail, noor_wa = person(wid, 'noor@example.test'), person(wid, '31600000001@c.us')
    assert noor_mail.id != noor_wa.id  # never merged automatically
    csrf = browser.csrf(f'/people/{noor_mail.id}')
    browser.post(f'/people/{noor_mail.id}/link', {'other': str(noor_wa.id), 'csrf': csrf})
    merged = person(wid, '31600000001@c.us')
    assert merged.id == noor_mail.id
    page = browser.get(f'/people/{noor_mail.id}').text
    assert 'linked by you' in page and 'WhatsApp · ' in page and 'Gmail · ' in page and 'vegetarian' in page
    with db.session() as s:
        ident = s.scalars(select(PersonIdentifier).where(PersonIdentifier.workspace_id == wid,
                                                         PersonIdentifier.value == '31600000001@c.us')).one()
    browser.post(f'/people/{noor_mail.id}/unlink', {'identifier': str(ident.id), 'csrf': csrf})
    assert person(wid, '31600000001@c.us').id != noor_mail.id


def test_account_deletion_removes_everything(make_browser):
    a, b = make_browser(), make_browser()
    setup(a)
    b.login('blake')
    from sqlalchemy import func
    from app.models import Base
    wid = workspace_of('alex@example.test')
    r = a.post('/account/delete', {'confirm': 'DELETE', 'csrf': a.csrf('/connections')})
    assert r.headers['location'] == '/moresocial/?notice=account-deleted'
    assert a.get('/home').headers['location'] == '/moresocial/'
    assert 'alex' in synthetic.STATE.revoked
    with db.session() as s:
        for table in Base.metadata.sorted_tables:
            if table.name == 'connector_cleanups':
                # Only the opaque ID survives until host-side resource cleanup is acknowledged.
                assert s.scalar(select(func.count()).select_from(table).where(table.c.workspace_id == wid)) == 1
                continue
            if 'workspace_id' in table.c:
                assert s.scalar(select(func.count()).select_from(table).where(table.c.workspace_id == wid)) == 0, table.name
    assert 'Blake Synthetic' in b.get('/home').text  # other users untouched
