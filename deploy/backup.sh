#!/usr/bin/env bash
# Moresocial backup: consistent pg_dump, encrypted private configuration and quiesced
# WhatsApp session copies. Kept on this host for RETENTION_DAYS (default 14). On-host backups
# do not survive loss of the server, and they retain deleted content until they expire.
# Never upload these files to GitHub.
set -euo pipefail
umask 077
ROOT=${MORESOCIAL_ROOT:-/srv/moresocial}
DEST=$ROOT/backups
RETENTION_DAYS=${RETENTION_DAYS:-14}
PASSFILE=$ROOT/secrets/backup_passphrase
COMPOSE=(docker compose -p moresocial -f "$ROOT/src/deploy/compose.yaml" --env-file "$ROOT/release.env")
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$DEST/.work-$stamp
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT

# 1. Database (custom format, consistent snapshot).
"${COMPOSE[@]}" exec -T db pg_dump -U moresocial_admin -d moresocial -Fc > "$work/db.dump"

# 2. WhatsApp sessions: stop each connector briefly so LocalAuth files are not copied mid-write.
if [ -d "$ROOT/data/whatsapp" ]; then
  for dir in "$ROOT"/data/whatsapp/*/; do
    [ -d "$dir" ] || continue
    ws=$(basename "$dir")
    [[ "$ws" =~ ^[0-9a-f]{32}$ ]] || continue
    running=$(docker inspect -f '{{.State.Running}}' "ms-wa-$ws" 2>/dev/null || echo false)
    [ "$running" = true ] && docker stop -t 20 "ms-wa-$ws" >/dev/null
    tar -C "$ROOT/data/whatsapp" -cf "$work/whatsapp-$ws.tar" "$ws"
    [ "$running" = true ] && docker start "ms-wa-$ws" >/dev/null
  done
fi

# 3. Private configuration (includes the encryption key; required to read stored grants).
tar -C "$ROOT" -cf "$work/config.tar" secrets runtime.env release.env

# 4. One encrypted archive.
tar -C "$work" -cf - . | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "file:$PASSFILE" \
  > "$DEST/moresocial-$stamp.tar.enc"
sha256sum "$DEST/moresocial-$stamp.tar.enc" > "$DEST/moresocial-$stamp.tar.enc.sha256"

# 5. Retention: only Moresocial backup files in this directory.
find "$DEST" -maxdepth 1 -type f -name 'moresocial-*.tar.enc*' -mtime "+$RETENTION_DAYS" -delete
echo "backup ok: $DEST/moresocial-$stamp.tar.enc"
