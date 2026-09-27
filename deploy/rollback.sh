#!/usr/bin/env bash
# Roll application images/source back to an earlier installed revision, keeping current data.
# Migrations in the first release are additive, so the previous revision can run on the newer
# schema. Never restores an old database over new user writes. If a future migration is not
# backward compatible, follow docs/operator-setup.md "Rollback" instead of this script.
set -euo pipefail
sha=${1:?usage: rollback.sh <previous-git-sha>}
[[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || exit 2
ROOT=${MORESOCIAL_ROOT:-/srv/moresocial}
dest=$ROOT/releases/$sha/src
[ -d "$dest" ] || { echo "revision $sha is not installed" >&2; exit 2; }
docker image inspect "moresocial-web:$sha" >/dev/null
# 0012 removes tables read by older images. Refuse before changing release.env or services.
# Inspect the actual schema so this also protects later revisions that retain the removal.
gatherings_removed=$(docker exec moresocial-db-1 psql -U moresocial_admin -d moresocial -tAc \
  "SELECT to_regclass('public.drafts') IS NULL")
if [ "$gatherings_removed" = t ]; then
  docker run --rm --entrypoint python "moresocial-web:$sha" -c \
    'from pathlib import Path; import sys; sys.exit(not Path("migrations/versions/0012_retire_manual_gatherings.py").is_file())' \
    || { echo "Schema 0012 requires a coordinated downgrade before this image. See docs/cleanup/schema-rollout.md." >&2; exit 2; }
fi
printf 'MORESOCIAL_REVISION=%s\n' "$sha" > "$ROOT/release.env"
docker compose -p moresocial -f "$dest/deploy/compose.yaml" --env-file "$ROOT/release.env" up -d web worker
ln -sfn "$dest" "$ROOT/src.next" && mv -T "$ROOT/src.next" "$ROOT/src"
systemctl restart moresocial-connectors.service 2>/dev/null || true
curl -fsS http://127.0.0.1:8772/ready && echo "rolled back to $sha" | tee -a "$ROOT/releases/history.log"
