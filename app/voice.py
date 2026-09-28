"""How the user writes to one person: representative examples of their own messages.

Read-only and deterministic. Examples come only from the user's own included, non-deleted
outgoing messages in the exact destination conversation (one account, one chat), with no
30-day cutoff: older relationship-specific writing is more useful than an empty set. They are
style evidence only (W labels, never valid factual citations) and untrusted data, never
instructions. Messages sent through MoreSocial's composer, and text equal to any prepared
draft, are left out so the assistant does not learn from its own output; other outgoing
text is assumed to be the user's, which the coverage reports as a remaining uncertainty.
Unrelated chats are never used to demonstrate style.
"""
from __future__ import annotations

import re
import unicodedata

from .models import Source, Suggestion, WhatsAppSend
from .repo import Scoped

VERSION = 'voice-v1'
MIN_EXAMPLES = 3         # fewer than this is "sparse": plain wording, and the UI says so
MAX_EXAMPLES = 12
TARGET_EXAMPLES = 8
MAX_CHARS = 8000
EXAMPLE_CHARS = 600
SCAN = 600               # most recent messages of the conversation examined, both directions
URL = re.compile(r'(https?://|www\.)\S+', re.I)
LETTER = re.compile(r'[^\W\d_]', re.UNICODE)
WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def normalize(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text or '').lower().split())


def words(text: str) -> int:
    return len(WORD.findall(text or ''))


def eligible(src: Source) -> str | None:
    """None when the message can demonstrate style, else a reason code."""
    body = (src.body or '').strip()
    if not body:
        return 'empty'
    if (src.meta or {}).get('media') and words(body) < 4:
        return 'media'
    if URL.search(body):
        return 'link'
    if not LETTER.search(body):
        return 'emoji_only'
    if body.startswith('>') or '\n>' in body:
        return 'quoted'
    return None


def select(ws: Scoped, account: str | None, chat: str | None, *, situation: str = 'reply', ignore=None) -> dict:
    """Up to 12 representative examples (at most 8,000 characters) of the user's own writing
    in this conversation, spread over time, preferring substantive messages and, for replies,
    messages that answered the other person. `ignore` is the suggestion being prepared: its
    own draft never changes its own context. Returns examples plus coverage metadata."""
    empty = {'version': VERSION, 'examples': [], 'available': 0, 'eligible': 0, 'excluded_generated': 0,
             'coverage': 'none', 'span': None}
    if not account or not chat:
        return empty
    rows = ws.all(ws.q(Source).where(Source.provider == 'whatsapp', Source.provider_account == account,
                                     Source.conversation_id == chat, Source.included.is_(True),
                                     Source.deleted_at.is_(None), Source.occurred_at.is_not(None))
                  .order_by(Source.occurred_at.desc(), Source.id.desc()).limit(SCAN))
    rows.reverse()
    mine = [r for r in rows if r.from_me]
    if not mine:
        return empty
    sent_ids = set(ws.s.scalars(ws.q(WhatsAppSend, WhatsAppSend.message_id).where(
        WhatsAppSend.account == account, WhatsAppSend.message_id.in_([r.provider_object_id for r in mine]))))
    others = ws.q(Suggestion, Suggestion.draft).where(Suggestion.draft != '')
    if ignore is not None:
        others = others.where(Suggestion.id != ignore)
    drafts = {normalize(d) for d in ws.s.scalars(others)}
    generated, candidates, seen = 0, [], set()
    previous = {}
    for before, row in zip([None] + rows[:-1], rows):
        previous[row.id] = before
    for row in mine:
        if row.provider_object_id in sent_ids or normalize(row.body) in drafts:
            generated += 1
            continue
        if eligible(row) or normalize(row.body) in seen:
            continue
        seen.add(normalize(row.body))
        before = previous.get(row.id)
        answered = bool(before is not None and not before.from_me)
        asked = bool(before is not None and not before.from_me and '?' in (before.body or ''))
        candidates.append({'row': row, 'words': words(row.body), 'answered': answered, 'asked': asked})
    out = dict(empty, available=len(mine), eligible=len(candidates), excluded_generated=generated)
    if not candidates:
        return out
    # Short messages stay when they are how this person is usually written to.
    median = sorted(c['words'] for c in candidates)[len(candidates) // 2]
    short_ok = 2 if median >= 4 else 4

    def score(c):
        substance = min(c['words'], 40) / 40 if c['words'] >= 4 else 0.1
        fit = (0.3 if c['asked'] else 0.15 if c['answered'] else 0) if situation == 'reply' else 0
        return substance + fit

    # Spread over time: round-robin across up to six chronological buckets, best first in each.
    buckets = min(6, len(candidates))
    size = len(candidates) / buckets
    groups = [sorted(candidates[int(i * size):int((i + 1) * size)], key=lambda c: (-score(c), str(c['row'].id)))
              for i in range(buckets)]
    chosen, chars, shorts = [], 0, 0
    while len(chosen) < TARGET_EXAMPLES and any(groups):
        for group in reversed(groups):  # newest bucket first on each pass
            while group:
                c = group.pop(0)
                if c['words'] < 4:
                    if shorts >= short_ok:
                        continue
                    shorts += 1
                text = c['row'].body.strip()
                text = text if len(text) <= EXAMPLE_CHARS else text[:EXAMPLE_CHARS].rstrip() + '…'
                if chars + len(text) > MAX_CHARS:
                    continue
                chosen.append((c['row'], text))
                chars += len(text)
                break
            if len(chosen) >= TARGET_EXAMPLES:
                break
    chosen.sort(key=lambda x: (x[0].occurred_at, str(x[0].id)))
    chosen = chosen[:MAX_EXAMPLES]
    out['examples'] = [{'label': f'W{n + 1}', 'id': str(r.id), 'version': r.version, 'text': text}
                       for n, (r, text) in enumerate(chosen)]
    out['coverage'] = 'recipient' if len(chosen) >= MIN_EXAMPLES else 'sparse'
    out['span'] = [chosen[0][0].occurred_at.date().isoformat(), chosen[-1][0].occurred_at.date().isoformat()] if chosen else None
    return out


def still_valid(ws: Scoped, examples: list[dict]) -> bool:
    """Every selected example still exists, is included and unchanged."""
    if not examples:
        return True
    ids = [e['id'] for e in examples]
    rows = {str(r.id): r for r in ws.all(ws.q(Source).where(Source.id.in_(ids)))}
    return all((r := rows.get(e['id'])) is not None and r.included and r.deleted_at is None and r.version == e['version']
               for e in examples)


def model_view(style: dict) -> dict:
    """What the writer sees: style-only examples, without IDs, dates or versions."""
    return {'coverage': style['coverage'], 'examples': [{'label': e['label'], 'text': e['text']} for e in style['examples']]}


def summary(style: dict | None, person: str) -> str:
    """Plain-language description of the voice basis, for the draft page."""
    if not style:
        return ''
    n = len(style.get('examples') or [])
    if style.get('mode') == 'custom':
        return 'Uses your custom style settings' + (f' and {n} of your messages to {person}.' if n else '.')
    if style.get('coverage') == 'recipient':
        span = style.get('span') or []
        when = f' ({span[0]} to {span[1]})' if len(span) == 2 and span[0] != span[1] else ''
        return f'Uses {n} examples of how you write to {person}{when}.'
    if n:
        return f'Only {n} of your messages to {person} were usable, so the wording stays plain.'
    return f'No usable messages from you to {person} were found, so the wording stays plain and concise.'
