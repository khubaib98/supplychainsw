#!/bin/sh
# Database dump + uploaded documents in one archive, optionally encrypted,
# rotated after BACKUP_KEEP_DAYS and copied off-site with rclone.
#
#   backup.sh loop   (default) run now, then every BACKUP_INTERVAL_SECONDS
#   backup.sh once   run a single backup (e.g. before an upgrade)
#
# Restore (see docs/04-deployment-guide.md):
#   openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in portal-X.tar.enc -out portal-X.tar
#   tar -xf portal-X.tar && pg_restore --clean --if-exists -d portal db.dump && tar -xzf media.tar.gz
set -u

OUT=/backups
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"

fail() {
  echo "$(date -u +%FT%TZ) backup FAILED: $1" >&2
  return 1
}

# Every step is checked explicitly: `set -e` is not reliable inside a function
# that is called from `run_once || …`.
run_once() {
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  work="$OUT/.work-$stamp"
  archive="$OUT/portal-$stamp.tar"
  mkdir -p "$work" || { fail "cannot create $work"; return 1; }

  if ! pg_dump --format=custom --file="$work/db.dump"; then
    rm -rf "$work"; fail "pg_dump"; return 1
  fi
  if [ -d /data/media ]; then
    tar -C /data -czf "$work/media.tar.gz" media || { rm -rf "$work"; fail "archiving documents"; return 1; }
  else
    tar -czf "$work/media.tar.gz" -T /dev/null || { rm -rf "$work"; fail "archiving documents"; return 1; }
  fi
  tar -C "$work" -cf "$archive" db.dump media.tar.gz || { rm -rf "$work" "$archive"; fail "tar"; return 1; }
  rm -rf "$work"

  if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
    if ! openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -in "$archive" -out "$archive.enc" \
        -pass env:BACKUP_PASSPHRASE; then
      # Never leave an unencrypted copy behind when encryption was asked for.
      rm -f "$archive" "$archive.enc"; fail "encryption (plaintext archive removed)"; return 1
    fi
    rm -f "$archive"
    archive="$archive.enc"
  fi

  name="$(basename "$archive")"
  (cd "$OUT" && sha256sum "$name" > "$name.sha256") || { fail "checksum"; return 1; }
  find "$OUT" -maxdepth 1 -name 'portal-*' -type f -mtime +"$KEEP_DAYS" -delete

  if [ -n "${RCLONE_REMOTE:-}" ]; then
    rclone copy "$archive" "$RCLONE_REMOTE" && rclone copy "$archive.sha256" "$RCLONE_REMOTE" \
      || { fail "off-site copy to $RCLONE_REMOTE (local copy kept)"; return 1; }
  fi
  echo "$(date -u +%FT%TZ) backup ok: $name ($(du -h "$archive" | cut -f1))"
}

if [ "${1:-loop}" = "once" ]; then
  run_once
  exit $?
fi

while true; do
  run_once
  sleep "$INTERVAL"
done
