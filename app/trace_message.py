"""Read-only message pipeline trace: python -m app.trace_message --help."""
import argparse
import json
import uuid
from sqlalchemy import select, text
from . import db
from .models import Chunk, ChunkSource, Claim, ClaimSource, Embedding, Job, Source
from .repo import Scoped


def trace(ws, source_id, include_text=False):
    source = ws.get(Source, source_id)
    if source is None:
        raise ValueError('Message not found in this workspace')
    result = {'source': {k: getattr(source, k) for k in
              ('id', 'provider', 'occurred_at', 'from_me', 'included', 'deleted_at', 'version')},
              'chunks': [], 'claims': []}
    if include_text:
        result['source']['text'] = source.body
    for link in ws.all(ws.q(ChunkSource).where(ChunkSource.source_id == source.id)):
        chunk = ws.get(Chunk, link.chunk_id)
        if chunk is None:
            continue
        embeddings = ws.all(ws.q(Embedding).where(Embedding.chunk_id == chunk.id))
        jobs = ws.all(ws.q(Job).where(Job.payload['chunk_id'].astext == str(chunk.id)).order_by(Job.created_at))
        result['chunks'].append({
            'id': chunk.id, 'label': link.label, 'source_version_matches': link.source_version == source.version,
            'start_at': chunk.start_at, 'end_at': chunk.end_at, 'extracted_at': chunk.extracted_at,
            'embeddings': [{'model': e.model, 'dimension': e.dimension, 'space': e.space} for e in embeddings],
            'jobs': [{k: getattr(j, k) for k in ('id', 'kind', 'status', 'attempts', 'max_attempts',
                     'last_error', 'run_after', 'updated_at')} for j in jobs]})
    claim_ids = select(ClaimSource.claim_id).where(ClaimSource.workspace_id == ws.wid,
                                                  ClaimSource.source_id == source.id)
    for claim in ws.all(ws.q(Claim).where(Claim.id.in_(claim_ids))):
        item = {k: getattr(claim, k) for k in ('id', 'subject', 'person_id', 'category', 'status')}
        if include_text:
            item['value'] = claim.value
        result['claims'].append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=uuid.UUID)
    parser.add_argument('--source', required=True, type=uuid.UUID)
    parser.add_argument('--include-text', action='store_true', help='Include private message and claim text')
    args = parser.parse_args()
    with db.session() as s:
        s.execute(text('SET TRANSACTION READ ONLY'))
        try:
            result = trace(Scoped(s, args.workspace), args.source, args.include_text)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, default=str, indent=2))


if __name__ == '__main__':
    main()
