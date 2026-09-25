#!/usr/bin/env bash
# Restore a Moresocial backup into a DISPOSABLE environment for verification, or (with
# --target, deliberately) into a fresh installation. It never overwrites the live database:
# it refuses to run against the compose project "moresocial" or a non-empty target directory.
#
#   deploy/restore.sh BACKUP.tar.enc --scratch /tmp/ms-restore-test
#
# Result: an isolated Postgres container (project "moresocial-restore-<n>", no published
# ports) loaded with the dump, checked for the Alembic head, plus unpacked sessions/config
# under the scratch directory. Remove it afterwards with the printed command.
set -euo pipefail
umask 077
backup=${1:?usage: restore.sh BACKUP.tar.enc --scratch DIR}
[ "${2:-}" = "--scratch" ] || { echo "only --scratch restores are automated" >&2; exit 2; }
scratch=${3:?scratch directory}
ROOT=${MORESOCIAL_ROOT:-/srv/moresocial}
PASSFILE=${BACKUP_PASSPHRASE_FILE:-$ROOT/secrets/backup_passphrase}
[ ! -e "$scratch" ] || [ -z "$(ls -A "$scratch")" ] || { echo "scratch directory must be empty" >&2; exit 2; }
mkdir -p "$scratch/unpacked"
sha256sum -c "$backup.sha256" >/dev/null
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "file:$PASSFILE" < "$backup" | tar -C "$scratch/unpacked" -xf -
project="moresocial-restore-$(date +%s)"
docker run -d --name "$project-db" --label moresocial.restore=1 -e POSTGRES_PASSWORD=restore-only \
  pgvector/pgvector:0.8.6-pg17-bookworm >/dev/null
for _ in $(seq 60); do docker exec "$project-db" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec -i "$project-db" psql -v ON_ERROR_STOP=1 -U postgres \
  -v restore_owner=moresocial_admin -v restore_role=moresocial -v restore_db=moresocial \
  < "$(dirname "$0")/restore-init.sql" >/dev/null
docker exec -i "$project-db" pg_restore -U postgres -d moresocial --no-owner --exit-on-error < "$scratch/unpacked/db.dump"
COMPOSE=(docker compose -p moresocial -f "$ROOT/src/deploy/compose.yaml" --env-file "$ROOT/release.env")
head=$("${COMPOSE[@]}" run --rm --no-deps --entrypoint python web -c \
  'from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config("alembic.ini")).get_current_head())')
current=$(docker exec "$project-db" psql -U postgres -d moresocial -tAc "SELECT version_num FROM alembic_version")
echo "restored database revision: $current (source head: $head)"
[ "$current" = "$head" ] || { echo "restore failed: migration revision mismatch" >&2; exit 1; }
docker exec "$project-db" psql -U postgres -d moresocial -tAc \
  "SELECT 'workspaces='||count(*) FROM workspaces UNION ALL SELECT 'sources='||count(*) FROM sources"
ls "$scratch/unpacked" | sed 's/^/unpacked: /'
echo "cleanup: docker rm -f $project-db && rm -rf $scratch"
