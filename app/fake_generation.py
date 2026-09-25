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
    return {'extract': extract, 'answer': answer, 'draft': draft, 'rewrite': rewrite, 'rsvp': rsvp}[ctx['purpose']](ctx)


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


def draft(ctx: dict) -> dict:
    ev = ctx['event']
    when = f" on {ev['times'][0]}" if ev.get('times') else ''
    hook = next((f for f in ctx['facts'] if f.get('category') == 'interest'), None)
    extra = f" I remembered you said you enjoy {hook['value']}." if hook else ''
    text = f"Hi {ctx['person']}! I'm organizing {ev['title']}{when}.{extra} Would you like to join?"
    return {'text': text, 'citations': [hook['label']] if hook else []}


def rewrite(ctx: dict) -> dict:
    text = ctx['text']
    if ctx['style'] == 'shorter':
        first = re.split(r'(?<=[.!?])\s+', text.strip())
        text = ' '.join(first[:1] + first[-1:]) if len(first) > 2 else text
    else:
        text = text.replace('Hi ', 'Hey ').replace('Would you like to join?', 'Up for it?')
    return {'text': text, 'citations': ctx.get('citations', [])}


def rsvp(ctx: dict) -> dict:
    for line in reversed(ctx['lines']):
        t = line['text'].lower()
        if re.search(r"\b(can't|cannot|won't make|not able|no thanks)\b", t):
            return {'status': 'declined', 'evidence': [line['label']]}
        if re.search(r"\b(maybe|not sure|might)\b", t):
            return {'status': 'maybe', 'evidence': [line['label']]}
        if re.search(r"\b(i'm in|count me in|yes|sounds great|i'll be there|i'll come)\b", t):
            return {'status': 'accepted', 'evidence': [line['label']]}
    return {'status': 'unclear', 'evidence': []}
