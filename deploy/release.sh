#!/usr/bin/env bash
# Build and activate one exact revision on the server (additive; other apps untouched).
#   sudo deploy/release.sh <git-sha>        (run from a checkout of that revision)
# Steps: copy source to /srv/moresocial/releases/<sha>/src, build images tagged <sha>, run
# migrations once, start web/worker, verify readiness, then point /srv/moresocial/src at it.
# Rollback: sudo deploy/rollback.sh <previous-sha>  (images/source only; data is kept).
set -euo pipefail
sha=${1:?usage: release.sh <git-sha>}
[[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || { echo "revision must be a git sha" >&2; exit 2; }
ROOT=/srv/moresocial
here=$(cd "$(dirname "$0")/.." && pwd)
[ "$(git -C "$here" rev-parse HEAD)" = "$(git -C "$here" rev-parse "$sha")" ] || { echo "checkout is not at $sha" >&2; exit 2; }
[ -z "$(git -C "$here" status --porcelain --untracked-files=no)" ] || { echo "checkout has local changes" >&2; exit 2; }
install -d -m 0750 "$ROOT" "$ROOT/releases"
install -d -m 0700 "$ROOT/data" "$ROOT/data/postgres" "$ROOT/data/whatsapp" "$ROOT/secrets" "$ROOT/backups"
dest=$ROOT/releases/$sha/src
if [ ! -d "$dest" ]; then
  mkdir -p "$dest"
  git -C "$here" archive "$sha" | tar -x -C "$dest"   # tracked files only: no secrets, no config/
fi
docker build -t "moresocial-web:$sha" -f "$dest/deploy/Dockerfile" "$dest"
docker build -t "moresocial-connector:$sha" "$dest/connector"
printf 'MORESOCIAL_REVISION=%s\n' "$sha" > "$ROOT/release.env.next"
COMPOSE=(docker compose -p moresocial -f "$dest/deploy/compose.yaml" --env-file "$ROOT/release.env.next")
"${COMPOSE[@]}" up -d db
"${COMPOSE[@]}" --profile migrate run --rm migrate
"${COMPOSE[@]}" up -d web worker
for _ in $(seq 60); do
  curl -fsS http://127.0.0.1:8772/ready >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://127.0.0.1:8772/ready
previous=$(readlink "$ROOT/src" 2>/dev/null || true)
ln -sfn "$dest" "$ROOT/src.next" && mv -T "$ROOT/src.next" "$ROOT/src"
mv "$ROOT/release.env.next" "$ROOT/release.env"
echo "$sha $(date -u +%FT%TZ) previous=${previous:-none}" >> "$ROOT/releases/history.log"
systemctl restart moresocial-connectors.service 2>/dev/null || true
echo "active revision: $sha (previous: ${previous:-none})"
