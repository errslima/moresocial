"""Deterministic stand-in for the generation model (tests and local development only).

It reads the structured context the real prompt is built from, uses only that evidence,
and ignores instructions inside imported text, like a well-behaved model should. It
proves pipeline contracts, not answer quality.
"""
from __future__ import annotations

import re

RULES = [
    (re.compile(r"\bI (?:love|enjoy|like|am into) ([^.,!?\n]{2,60})", re.I), 'interest', None),
    (re.compile(r"\bI(?:'m| am) moving to ([A-Z][\w-]+)"), 'life_update', 'location'),
    (re.compile(r"\bI moved to ([A-Z][\w-]+)"), 'life_update', 'location'),
    (re.compile(r"\bI (?:started working|work|started) at ([A-Z][\w &-]{1,40}?)(?: last| this|[.,!\n]|$)"), 'life_update', 'job'),
    (re.compile(r"\bI(?:'m| am) (vegetarian|vegan)\b", re.I), 'preference', 'diet'),
    (re.compile(r"\bI can(?:'t|not) do ([^.,!?\n]{2,60})", re.I), 'preference', 'availability'),
    (re.compile(r"\b(Weekday evenings work better[^.!?\n]*)", re.I), 'preference', 'availability'),
    (re.compile(r"\bI(?:'ll| will) ([^.,!?\n]{3,80})", re.I), 'commitment', None),
]
STOP = {'the', 'and', 'what', 'does', 'did', 'who', 'where', 'when', 'how', 'with', 'about', 'for', 'likes', 'like',
        'is', 'are', 'was', 'do', 'to', 'of', 'a', 'an', 'in', 'on', 'me', 'my', 'any', 'which', 'wie', 'wat'}


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w']+", text.lower()) if len(w) > 2 and w not in STOP}


def respond(ctx: dict) -> dict:
    if ctx['purpose'] == 'events':
        return {'events': []}
    if ctx['purpose'] == 'event_identity':
        return event_identity(ctx)
    if ctx['purpose'] == 'suggestions':
        return suggestion_review(ctx)
    if ctx['purpose'] == 'suggestion_draft':
        return reply(ctx)
    if ctx['purpose'] == 'assistant_preview':
        return {'text': draft_text(ctx, 'thanks for the invitation! [your answer here].'), 'used_facts': []}
    if ctx['purpose'] == 'proactive':
        return proactive(ctx)
    if ctx['purpose'] == 'draft_resolve':
        return resolve(ctx)
    if ctx['purpose'] == 'draft_verify':
        return verify(ctx)
    return {'extract': extract, 'answer': answer, 'group_observations': group_observations}[ctx['purpose']](ctx)


def event_identity(ctx):
    """Narrow synthetic verdicts: a shared payment/booking reference is the same outing;
    unrelated named activities are different; anything else stays uncertain."""
    for r in ctx['references']:
        if r['type'] in ('payment', 'booking', 'reference') and r['a'] and r['b']:
            return {'verdict': 'same', 'evidence_a': r['a'][:2], 'evidence_b': r['b'][:2],
                    'supporting': ['shared_payment_link' if r['type'] == 'payment' else 'shared_booking_reference'],
                    'conflicting': [], 'explanation': 'Both records share one booking reference.'}
    first = lambda side: [m['label'] for m in ctx['messages'] if m['label'][0] == side][:1]  # noqa: E731
    from .events import tokens
    ta, tb = (tokens(side['title'] + ' ' + side['activity']) for side in (ctx['a'], ctx['b']))
    if ta and tb and not ta & tb and first('A') and first('B'):
        return {'verdict': 'different', 'evidence_a': first('A'), 'evidence_b': first('B'), 'supporting': [],
                'conflicting': ['different_activity'], 'explanation': 'The activities differ.'}
    support = ['same_people'] if 'shared_people' in ctx['signals'] else []
    return {'verdict': 'uncertain', 'evidence_a': first('A'), 'evidence_b': first('B'), 'supporting': support,
            'conflicting': [], 'explanation': 'The evidence does not settle it.'}


GREETINGS = ('hoi', 'hey', 'heyy', 'hi', 'hallo', 'hello', 'yo', 'hiya', 'ola', 'olá', 'dag')
COMMON = set('''Yes Thanks Thank Sure Great Perfect Sorry See Looking Hope Also That This The And But Maybe Just Let
Sounds Have Are How What When Where Would Could Can Will Good Nice Staying'''.split())


def styled(ctx, greeting_name, body):
    """Persona-dependent wording: enough to show that style settings reach the model input.
    Custom mode uses the tone controls; conversation mode borrows the greeting the user's own
    examples use (style only: nothing else from an example is repeated)."""
    style = (ctx.get('persona') or {}).get('style') or {}
    greet = {'reserved': 'Hello', 'warm': 'Hey'}.get(style.get('warmth'), 'Hi')
    examples = (ctx.get('style') or {}).get('examples') or []
    if not style and examples:
        first = [e['text'].split()[0].strip(',!.') for e in examples if e['text'].split()]
        used = [w for w in first if w.lower() in GREETINGS]
        if used:
            greet = max(set(used), key=used.count)
    text = f'{greet} {greeting_name}, {body}'
    if style.get('directness') == 'direct':
        text = text.replace('Would you like to', 'Let us').replace('Shall we', 'Let us')
    if style.get('emoji') == 'often':
        text += ' 😊'
    return text


def linked_event(ctx):
    label = (ctx.get('action') or ctx.get('candidate') or {}).get('event')
    return next((e for e in ctx.get('events', []) if e['label'] == label), None)


def draft_text(ctx, fallback):
    name = ctx.get('person') or 'there'
    ev = linked_event(ctx)
    if ev and ev.get('place') and ev['status'] == 'planned' and not ev.get('needs_checking'):
        when = ev['weekday'] + (f" at {ev['booked_start']}" if ev.get('booked_start') else '')
        return styled(ctx, name, f"see you at {ev['place']} on {when}! {fallback}")
    return styled(ctx, name, fallback)


def reply(ctx):
    """Synthetic reply writer: the linked event, permitted facts (never raw evidence) and a
    placeholder for every unresolved point; style examples only shape the greeting."""
    answers = ctx.get('answers') or {}
    facts = answers.get('permitted_facts') or []
    parts = []
    for f in facts:
        sentence = f['fact'].strip()
        parts.append(sentence[0].upper() + sentence[1:] + ('' if sentence.endswith(('.', '!', '?')) else '.'))
    for u in answers.get('unresolved') or []:
        parts.append('[how you are]' if u.get('reason') == 'current_state' else '[your answer].')
    body = ' '.join(parts) if parts else '[your answer here].'
    return {'text': draft_text(ctx, body), 'used_facts': [f['label'] for f in facts]}


STAY = re.compile(r"you can stay (?:at|in|with) (mine|my place|me|ours)(?: until| till| through) ([^.,!\n]+)", re.I)
ASSENT = re.compile(r"^(yes|great|perfect|deal|let's do (?:it|that)|sounds good|staying at yours would be amazing|amazing)", re.I)
CANCEL = re.compile(r"can(?:'|no)?t host you|cannot host you|can't have you (?:stay|over)|fell through|have to cancel", re.I)
LANDS = re.compile(r"(?:arrives|lands|arrival)\s*(?:at\s*)?(\d{1,2}:\d{2})", re.I)
HOUSING = {'stay', 'staying', 'place', 'live', 'living', 'sleep', 'accommodation', 'housing', 'room', 'wonen', 'slapen'}
MONTHS = {m: n + 1 for n, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                          'september', 'october', 'november', 'december'])}


def _answer(q, status='missing', reason='no_evidence', **kw):
    return {'question': q['label'], 'status': status, 'answer': kw.get('answer', ''), 'decision': kw.get('decision', 'none'),
            'statement_date': kw.get('statement_date'), 'effective_from': None, 'effective_to': kw.get('until'),
            'deadline': kw.get('deadline'), 'support': kw.get('support', []), 'conflicts': kw.get('conflicts', []),
            'disclosure': kw.get('disclosure', 'withhold'), 'reason': reason, 'note': kw.get('note', '')}


def _day(text, at):
    m = re.search(r'(\d{1,2})(?:st|nd|rd|th)? ([A-Za-z]+)', text)
    if not m or m.group(2).lower() not in MONTHS or not at:
        return None
    return f'{at[:4]}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}'


def resolve(ctx):
    """Intentionally narrow synthetic fixtures (fictional housing, flight, correction, news and
    same-name cases), not a production heuristic. It cites what it relies on; the server applies
    the disclosure guards."""
    out = []
    passages = ctx['passages']
    for q in ctx['questions']:
        if q['about_current_feelings']:
            out.append(_answer(q, reason='current_state'))
            continue
        qwords = words(q['text'])
        claim = next((c for c in ctx['claims'] if c['status'] != 'rejected' and (c['status'] == 'confirmed' or c['authored_by_user'])
                      and qwords & words(c['value'])), None)
        named = [n for n in re.findall(r"\b([A-Z][a-z]{2,})\b", q['text'])
                 if len({p['speaker'] for p in passages if p['speaker'].split(' (')[0] == n}) > 1]
        stays = [p for p in passages if p['speaker'] != 'You' and STAY.search(p['text'])]
        if named:
            out.append(_answer(q, 'conflicting', 'ambiguous', note=f'Two different contacts are called {named[0]}.',
                               conflicts=[p['label'] for p in passages if p['speaker'].startswith(named[0])][:4]))
        elif claim:
            value = claim['value'] if claim['value'].lower().startswith(('i ', "i'm")) else 'I ' + claim['value']
            out.append(_answer(q, 'supported', 'ok', answer=value, decision='completed', support=[claim['label']],
                               disclosure='share'))
        elif stays and qwords & HOUSING:
            offer = stays[-1]
            m = STAY.search(offer['text'])
            later = [p for p in passages if p['where'] == offer['where'] and p['at'] > offer['at'] and CANCEL.search(p['text'])]
            assent = [p for p in passages if p['where'] == offer['where'] and p['speaker'] == 'You'
                      and ASSENT.search(p['text'].strip())]
            host = offer['speaker'].split(' (')[0]
            if later:
                out.append(_answer(q, 'uncertain', 'outdated', decision='cancelled', support=[later[-1]['label']],
                                   note='A later message cancelled the arrangement.'))
            elif assent:
                until = m.group(2).strip()
                out.append(_answer(q, 'supported', 'ok', answer=f"I'm staying at {host}'s place until {until}",
                                   decision='accepted', support=[offer['label'], assent[0]['label']], disclosure='share',
                                   until=_day(until, offer['at']), deadline=_day(until, offer['at']),
                                   statement_date=offer['at'][:10]))
            else:
                out.append(_answer(q, 'uncertain', 'ambiguous', decision='offer', support=[offer['label']],
                                   note='An offer, not an agreed arrangement.'))
        elif any(w in qwords for w in ('land', 'arrive', 'arrival', 'flight')) and (
                hit := next((p for p in passages if p['channel'] == 'email' and LANDS.search(p['text'])), None)):
            out.append(_answer(q, 'supported', 'ok', answer=f"My flight lands at {LANDS.search(hit['text']).group(1)}",
                               decision='completed', support=[hit['label']], disclosure='share'))
        elif 'news' in qwords and (hit := next((p for p in passages if p['confidential']), None)):
            # A careless reading would repeat it; the server's confidentiality guard must withhold it.
            out.append(_answer(q, 'supported', 'ok', answer='My friend has big news', support=[hit['label']],
                               disclosure='share', decision='completed'))
        else:
            out.append(_answer(q))
    return {'answers': out}


def verify(ctx):
    """Synthetic grounding check: a sentence naming a capitalised word or number that appears
    in none of the conversation, events, permitted facts or the recipient's name is removed."""
    corpus = ' '.join([ctx.get('person') or ''] + [m['text'] for m in ctx.get('messages', [])] +
                      [json_text(e) for e in ctx.get('events', [])] +
                      [f['fact'] for f in (ctx.get('answers') or {}).get('permitted_facts', [])]).lower()
    kept, issues = [], []
    for sentence in re.split(r'(?<=[.!?])\s+', ctx['draft'].strip()):
        body = re.sub(r'\[[^\]]*\]', '', sentence)
        tokens = re.findall(r"\b(\d[\d:.]*|[A-Z][a-z]{2,})\b", body)
        if any(t.lower() not in corpus for t in tokens if t.lower() not in GREETINGS and t not in COMMON):
            issues.append({'kind': 'unsupported', 'about': 'a detail without support'})
        else:
            kept.append(sentence)
    if not issues:
        return {'verdict': 'ok', 'issues': [], 'revised_text': None}
    return {'verdict': 'revise', 'issues': issues, 'revised_text': ' '.join(kept) or '[your answer]'}


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


def proactive(ctx):
    """Synthetic preparation: a message for the supplied candidate using only supplied facts."""
    c = ctx['candidate']
    name = ctx['person']
    ev = linked_event(ctx)
    update = next(iter(ctx.get('updates') or []), None)
    if c['kind'] == 'event_question':
        title, body = f'Check a detail with {name}', f"is the time for {ev['title']} still [time]?" if ev else '[question]'
    elif ev:
        place = f" at {ev['place']}" if ev.get('place') else ''
        start = f" {ev['booked_start']}" if ev.get('booked_start') else ''
        title = f"Remind {name} about {ev['title']}"
        body = f"looking forward to {ev['title']}{place} on {ev['weekday']}{start}!"
    elif c['kind'] == 'share_update' and update:
        title, body = f'Share your news with {name}', f"a quick update: {update['text']}"
    else:
        title, body = f'Check in with {name}', 'how are you doing? It has been a while.'
        if update:
            body += f" News from me: {update['text']}"
    evidence = [m['label'] for m in ctx.get('messages', [])[-1:]]
    return {'action': 'prepare', 'title': title[:200], 'reason': 'Prepared from your own schedule and preferences.',
            'draft': styled(ctx, name, body), 'evidence': evidence, 'event': ev['label'] if ev else None,
            'update': update['label'] if update and c['kind'] in ('check_in', 'share_update') else None, 'skip_reason': None}


def event_reply(ctx, m):
    """The Aloha fixture: a question about meeting at an event. Uses the one event that shares
    messages with this chat; participation alone is not enough."""
    related = [e for e in ctx.get('events', []) if e['relation'] == 'shares messages with this conversation']
    if len(related) != 1:
        return f"Reply to {ctx['person']} about Sunday", None
    ev = related[0]
    where = f" at {ev['place'].split()[0]}" if ev.get('place') else ' there'
    return f"Meet {ctx['person']}{where} on {ev['weekday']}", ev['label']


def suggestion_review(ctx):
    """Intentionally narrow synthetic fixtures, not a production heuristic."""
    items, resolved = [], []
    for m in ctx['messages']:
        kind = ('reply' if not m['from_me'] and m['text'].startswith(('Can you join dinner', 'Are we meeting')) else
                'promise' if m['from_me'] and m['text'].startswith("I'll send you the photos") else None)
        if not kind:
            continue
        old = next((x for x in ctx['existing'] if x['anchor'] == m['label'] and x['kind'] == kind), None)
        later = [x for x in ctx['messages'] if x['at'] > m['at'] and x['from_me'] and
                 x['text'].startswith(('Yes, Friday works', 'Here are the photos'))]
        if later:
            if old and old['state'] in ('active', 'snoozed'):
                resolved.append({'id': old['id'], 'evidence': [later[0]['label']]})
            continue
        title, event = ('Reply about dinner' if kind == 'reply' else 'Send the photos'), None
        if m['text'].startswith('Are we meeting'):
            title, event = event_reply(ctx, m)
        items.append({'id': old['id'] if old else None, 'kind': kind, 'anchor': m['label'], 'title': title,
            'reason': "I couldn't find this handled in the synced messages.", 'evidence': [m['label']], 'due_at': None,
            'event': event})
    return {'items': items[:8], 'resolved': resolved[:30]}


def extract(ctx: dict) -> dict:
    claims = []
    for line in ctx['lines']:
        if line.get('speaker') is None:
            continue
        for pattern, category, attribute in RULES:
            m = pattern.search(line['text'])
            if m:
                claims.append({'subject': line['speaker'], 'category': category, 'attribute': attribute or 'none',
                               'value': m.group(1).strip(), 'basis': 'explicit', 'evidence': [line['label']]})
    return {'claims': claims}


def answer(ctx: dict) -> dict:
    q = words(ctx['question'])
    scored = []
    for e in ctx['evidence']:
        overlap = q & words(e['text'] + ' ' + (e.get('speaker') or ''))
        if overlap:
            scored.append((len(overlap), e))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return {'answer': 'I could not find enough evidence in your imported sources to answer that.',
                'citations': [], 'insufficient': True}
    top = [e for _, e in scored[:3]]
    text = ' '.join(f"{e.get('speaker') or 'Someone'} wrote: “{e['text'][:160]}” [{e['label']}]" for e in top)
    return {'answer': text, 'citations': [e['label'] for e in top], 'insufficient': False}


def group_observations(ctx):
    # Synthetic equivalence only. Real semantic quality is tested with adjudicated fixtures.
    values = {c['value'].strip().lower().rstrip('.!') for c in ctx['incoming']['claims']}
    return {'decisions': [{'candidate': key, 'relation': 'equivalent' if
        values == {c['value'].strip().lower().rstrip('.!') for c in group['claims']} else 'uncertain'}
        for key, group in ctx['candidates'].items()]}
