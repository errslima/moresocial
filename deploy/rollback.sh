#!/usr/bin/env bash
# Roll application images/source back to an earlier installed revision, keeping current data.
# Migrations in the first release are additive, so the previous revision can run on the newer
# schema. Never restores an old database over new user writes. If a future migration is not
# backward compatible, follow docs/operator-setup.md "Rollback" instead of this script.
set -euo pipefail
sha=${1:?usage: rollback.sh <previous-git-sha>}
[[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || exit 2
ROOT=/srv/moresocial
dest=$ROOT/releases/$sha/src
[ -d "$dest" ] || { echo "revision $sha is not installed" >&2; exit 2; }
docker image inspect "moresocial-web:$sha" >/dev/null
printf 'MORESOCIAL_REVISION=%s\n' "$sha" > "$ROOT/release.env"
docker compose -p moresocial -f "$dest/deploy/compose.yaml" --env-file "$ROOT/release.env" up -d web worker
ln -sfn "$dest" "$ROOT/src.next" && mv -T "$ROOT/src.next" "$ROOT/src"
systemctl restart moresocial-connectors.service 2>/dev/null || true
curl -fsS http://127.0.0.1:8772/ready && echo "rolled back to $sha" | tee -a "$ROOT/releases/history.log"
