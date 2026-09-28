#!/usr/bin/env bash
# Roll application images/source back to an earlier installed revision, keeping current data.
# Exact migration-head and draft-policy compatibility are required. Never restores an old
# database over new user writes. See docs/operator-setup.md "Rollback" for forward recovery.
set -euo pipefail
sha=${1:?usage: rollback.sh <previous-git-sha>}
[[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || exit 2
ROOT=${MORESOCIAL_ROOT:-/srv/moresocial}
dest=$ROOT/releases/$sha/src
[ -d "$dest" ] || { echo "revision $sha is not installed" >&2; exit 2; }
docker image inspect "moresocial-web:$sha" >/dev/null
# /ready requires an exact migration head. Check before touching release.env/services;
# an additive schema alone does not make an older image operationally compatible.
schema_revision=$(docker exec moresocial-db-1 psql -U moresocial_admin -d moresocial -tAc \
  'SELECT version_num FROM alembic_version')
target_revision=$(docker run --rm --entrypoint python "moresocial-web:$sha" -c \
  'from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config("alembic.ini")).get_current_head())')
if [ "$schema_revision" != "$target_revision" ]; then
  echo "Schema $schema_revision differs from image head $target_revision; coordinated downgrade or forward fix required." >&2
  exit 2
fi
if [ "$schema_revision" = 0016 ]; then
  versions=$(docker exec moresocial-db-1 psql -U moresocial_admin -d moresocial -tAc \
    "SELECT DISTINCT version FROM draft_evidence_bundles ORDER BY version")
  docker run --rm --env "MORESOCIAL_BUNDLE_VERSIONS=$versions" --entrypoint python "moresocial-web:$sha" -c \
    'import os, sys; from app.draft_evidence import VERSION, LIMITED_VERSION; sys.exit(not set(os.environ["MORESOCIAL_BUNDLE_VERSIONS"].split()).issubset({VERSION, LIMITED_VERSION}))' \
    || { echo "Image cannot validate stored draft policies; keep evidence mode off and fix forward." >&2; exit 2; }
fi
# 0012 removes tables read by older images. Refuse before changing release.env or services.
# Inspect the actual schema so this also protects later revisions that retain the removal.
gatherings_removed=$(docker exec moresocial-db-1 psql -U moresocial_admin -d moresocial -tAc \
  "SELECT to_regclass('public.drafts') IS NULL")
if [ "$gatherings_removed" = t ]; then
  docker run --rm --entrypoint python "moresocial-web:$sha" -c \
    'from pathlib import Path; import sys; sys.exit(not Path("migrations/versions/0012_retire_manual_gatherings.py").is_file())' \
    || { echo "Schema 0012 requires a coordinated downgrade before this image. See docs/cleanup/schema-rollout.md." >&2; exit 2; }
fi
# 0014 adds proactive suggestions (no anchor, new kinds) that older images cannot display.
assistant_schema=$(docker exec moresocial-db-1 psql -U moresocial_admin -d moresocial -tAc \
  "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'suggestions' AND column_name = 'producer')")
if [ "$assistant_schema" = t ]; then
  docker run --rm --entrypoint python "moresocial-web:$sha" -c \
    'from pathlib import Path; import sys; sys.exit(not Path("migrations/versions/0014_assistant.py").is_file())' \
    || { echo "Schema 0014 (assistant) requires a coordinated downgrade before this image. Prefer 'python -m app.proactive mode off' and a forward fix; see docs/operator-setup.md." >&2; exit 2; }
fi
previous=$(readlink -f "$ROOT/src")
printf 'MORESOCIAL_REVISION=%s\n' "$sha" > "$ROOT/release.env.next"
trap 'rm -f "$ROOT/release.env.next"' EXIT
restore_previous() {
  docker compose -p moresocial -f "$previous/deploy/compose.yaml" --env-file "$ROOT/release.env" up -d web worker
}
if ! docker compose -p moresocial -f "$dest/deploy/compose.yaml" --env-file "$ROOT/release.env.next" up -d web worker; then
  restore_previous
  exit 1
fi
ready=false
for _ in $(seq "${MORESOCIAL_READINESS_ATTEMPTS:-30}"); do
  if curl -fsS http://127.0.0.1:8772/ready >/dev/null; then ready=true; break; fi
  sleep 1
done
if [ "$ready" != true ]; then
  restore_previous
  echo "Rollback target failed readiness; previous release restored." >&2
  exit 1
fi
mv "$ROOT/release.env.next" "$ROOT/release.env"
ln -sfn "$dest" "$ROOT/src.next" && mv -T "$ROOT/src.next" "$ROOT/src"
systemctl restart moresocial-connectors.service 2>/dev/null || true
echo "rolled back to $sha" | tee -a "$ROOT/releases/history.log"
