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
        return {'text': draft_text(ctx, "[your answer here].")}
    if ctx['purpose'] == 'assistant_preview':
        return {'text': draft_text(ctx, 'thanks for the invitation! [your answer here].')}
    if ctx['purpose'] == 'proactive':
        return proactive(ctx)
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


def styled(ctx, greeting_name, body):
    """Persona-dependent wording: enough to show that style settings reach the model input."""
    style = (ctx.get('persona') or {}).get('style') or {}
    greet = {'reserved': 'Hello', 'warm': 'Hey'}.get(style.get('warmth'), 'Hi')
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
