"""M5: the lasergame gathering workflow through the server-rendered screens."""
import re
import uuid
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
                 '/ask', '/events', '/me', '/connections']:
        r = browser.get(path)
        assert r.status_code == 200, path
        assert 'name="csrf" value=""' not in r.text, path  # every form carries the session token


def test_next_steps_dismiss_and_snooze(browser):
    setup(browser)
    wid = workspace_of('alex@example.test')
    with db.session() as s:
        s.add(Person(id=uuid.uuid4(), workspace_id=wid, display_name='A pinned friend', name_source='user', pinned=True))
    home = browser.get('/home').text
    key = re.search(r'name="key" value="([^"]+)"', home).group(1)
    browser.post('/home/steps', {'key': key, 'action': 'snooze', 'csrf': browser.csrf()})
    assert f'value="{key}"' not in browser.get('/home').text


def test_about_me_is_editable_and_observations_are_distinct(browser):
    setup(browser)
    csrf = browser.csrf('/me')
    browser.post('/me', {'body': 'Small groups, active plans. See friends monthly.', 'entry_token': str(uuid.uuid4()), 'csrf': csrf})
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
