#!/usr/bin/env bash
# CI-only driver for the encrypted backup + disposable restore proof.
#
# Runs against the ephemeral postgres:16 service the GitHub Actions job starts,
# using the image built from Dockerfile.backup. It never reads a developer
# secret or a production credential. Every negative case asserts the EXACT exit
# code and that the restore target still holds its pre-restore sentinel data.
#
# Subcommands (run in order by .github/workflows/ci.yml):
#   backup              real encrypted pg_dump backup + artifact verification
#   restore-wrong-key   wrong key -> exit 1, target untouched
#   restore-tampered    one flipped archive byte -> exit 1, target untouched
#   restore-refused     app DB / missing confirmation -> exit 2, nothing touched
#   restore             correct key -> exit 0, restored DB verified
#   cleanup             remove every ephemeral key and working file
set -euo pipefail

: "${BACKUP_CI_DIR:?BACKUP_CI_DIR must be set (see the ephemeral-secrets step)}"
: "${CI_SOURCE_DB:?}" "${CI_RESTORE_TARGET:?}" "${CI_DB_ADMIN_USER:?}"
: "${CI_DB_ADMIN_PASSWORD:?}" "${CI_APP_ROLE:?}" "${CI_APP_PASSWORD:?}"

IMAGE="${BACKUP_IMAGE:-austro-backup:ci}"
KEY="$BACKUP_CI_DIR/keys/backup.key"
WRONG_KEY="$BACKUP_CI_DIR/keys/wrong.key"
OUT="$BACKUP_CI_DIR/out"
TAMPER="$BACKUP_CI_DIR/tampered"
EXPECTED="$BACKUP_CI_DIR/expected.json"
FIXTURE="scripts/ci_backup_fixture.py"

# Common container hardening for every run: non-root (runner uid), read-only
# root filesystem, tmpfs scratch for plaintext, no privilege escalation.
run_container() {
  local key="$1" archive_dir="$2"; shift 2
  docker run --rm --network host \
    --user "$(id -u):$(id -g)" \
    --read-only \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:size=64m,mode=1777 \
    --tmpfs /var/tmp/austro-backup-work:size=512m,mode=1777 \
    -e DB_HOST=127.0.0.1 -e DB_PORT=5432 \
    -e BACKUP_ENCRYPTION_ENABLED=true \
    -e BACKUP_ENCRYPTION_KEY_FILE=/run/secrets/backup_encryption_key \
    -e BACKUP_WORKDIR=/var/tmp/austro-backup-work \
    -v "$key:/run/secrets/backup_encryption_key:ro" \
    -v "$archive_dir:/app/backups" \
    "$@"
}

# Drill runs as the restore-only configuration. Secrets are passed by NAME so
# values never appear in the docker command line.
drill() {
  local key="$1" archive_dir="$2" target="$3" confirm="$4" archive
  archive="$(basename "$(ls "$archive_dir"/austro_ai_backup_*.sql.gz | head -n1)")"
  run_container "$key" "$archive_dir" \
    -e DB_NAME="$CI_SOURCE_DB" -e DB_USER="$CI_APP_ROLE" -e DB_PASSWORD \
    -e RESTORE_TARGET_DB="$target" \
    -e RESTORE_CONFIRM_DESTRUCTIVE="$confirm" \
    -e RESTORE_ADMIN_USER="$CI_DB_ADMIN_USER" -e RESTORE_ADMIN_PASSWORD \
    --entrypoint python "$IMAGE" scripts/pg_restore_drill.py "/app/backups/$archive"
}

expect_rc() {
  local want="$1" got="$2" label="$3"
  if [ "$got" -ne "$want" ]; then
    echo "ERROR: $label exited $got, expected $want"; exit 1
  fi
  echo "OK: $label exited $want as required"
}

# Secrets reach containers by NAME (`-e NAME`), never as argv values. The
# drill's app role uses DB_PASSWORD; its administrative role uses the admin
# password. Both are exported here, so a missing export fails the drill's own
# "credentials required" refusal instead of silently using a wrong account.
export DB_PASSWORD="$CI_APP_PASSWORD"
export RESTORE_ADMIN_PASSWORD="$CI_DB_ADMIN_PASSWORD"

case "${1:-}" in
  backup)
    # The backup password is passed by NAME (value inherited from the step env).
    export DB_PASSWORD="$CI_DB_ADMIN_PASSWORD"
    run_container "$KEY" "$OUT" \
      -e DB_NAME="$CI_SOURCE_DB" -e DB_USER="$CI_DB_ADMIN_USER" -e DB_PASSWORD \
      -e BACKUP_OUTDIR=/app/backups \
      --entrypoint python "$IMAGE" scripts/pg_backup.py /app/backups
    archive="$(ls "$OUT"/austro_ai_backup_*.sql.gz)"
    manifest="${archive%.sql.gz}.manifest.json"
    echo "published: $(basename "$archive") and $(basename "$manifest")"
    # Only the published pair may remain (checked inside the verifier, which
    # asserts the directory listing exactly): no partial or plaintext artifacts.
    python3 "$FIXTURE" verify-artifact --archive "$archive" --manifest "$manifest" \
      --key "$KEY" --outdir "$OUT"
    export DB_PASSWORD="$CI_APP_PASSWORD"
    ;;

  restore-wrong-key)
    rc=0
    drill "$WRONG_KEY" "$OUT" "$CI_RESTORE_TARGET" \
      "DROP-AND-RESTORE:$CI_RESTORE_TARGET" > "$BACKUP_CI_DIR/wrong-key.log" 2>&1 || rc=$?
    cat "$BACKUP_CI_DIR/wrong-key.log"
    expect_rc 1 "$rc" "restore with a wrong key"
    grep -q "NOT modified" "$BACKUP_CI_DIR/wrong-key.log" || { echo "ERROR: no 'NOT modified' proof"; exit 1; }
    python3 "$FIXTURE" sentinel --target "$CI_RESTORE_TARGET" --expect present
    ;;

  restore-tampered)
    rm -rf "$TAMPER"; mkdir -p "$TAMPER"; chmod 0700 "$TAMPER"
    archive="$(ls "$OUT"/austro_ai_backup_*.sql.gz)"
    cp "$archive" "$TAMPER/"; cp "${archive%.sql.gz}.manifest.json" "$TAMPER/"
    python3 - "$TAMPER/$(basename "$archive")" <<'PY'
import sys
path = sys.argv[1]
data = bytearray(open(path, "rb").read())
data[len(data) // 2] ^= 0x01          # flip a single ciphertext bit
open(path, "wb").write(bytes(data))
print("tampered one byte of the copied archive")
PY
    rc=0
    drill "$KEY" "$TAMPER" "$CI_RESTORE_TARGET" \
      "DROP-AND-RESTORE:$CI_RESTORE_TARGET" > "$BACKUP_CI_DIR/tampered.log" 2>&1 || rc=$?
    cat "$BACKUP_CI_DIR/tampered.log"
    expect_rc 1 "$rc" "restore of a tampered archive"
    grep -q "NOT modified" "$BACKUP_CI_DIR/tampered.log" || { echo "ERROR: no 'NOT modified' proof"; exit 1; }
    python3 "$FIXTURE" sentinel --target "$CI_RESTORE_TARGET" --expect present
    ;;

  restore-refused)
    rc=0
    drill "$KEY" "$OUT" "$CI_SOURCE_DB" "DROP-AND-RESTORE:$CI_SOURCE_DB" \
      > "$BACKUP_CI_DIR/refused-appdb.log" 2>&1 || rc=$?
    cat "$BACKUP_CI_DIR/refused-appdb.log"
    expect_rc 2 "$rc" "restore targeting the application database"
    rc=0
    drill "$KEY" "$OUT" "$CI_RESTORE_TARGET" "yes" \
      > "$BACKUP_CI_DIR/refused-noconfirm.log" 2>&1 || rc=$?
    cat "$BACKUP_CI_DIR/refused-noconfirm.log"
    expect_rc 2 "$rc" "restore without destructive authorisation"
    python3 "$FIXTURE" counts --database "$CI_SOURCE_DB" --expected "$EXPECTED"
    python3 "$FIXTURE" sentinel --target "$CI_RESTORE_TARGET" --expect present
    ;;

  restore)
    rc=0
    drill "$KEY" "$OUT" "$CI_RESTORE_TARGET" \
      "DROP-AND-RESTORE:$CI_RESTORE_TARGET" > "$BACKUP_CI_DIR/restore.log" 2>&1 || rc=$?
    cat "$BACKUP_CI_DIR/restore.log"
    expect_rc 0 "$rc" "restore with the correct key"
    python3 "$FIXTURE" verify-restored --target "$CI_RESTORE_TARGET" --expected "$EXPECTED"
    python3 "$FIXTURE" counts --database "$CI_SOURCE_DB" --expected "$EXPECTED"
    ;;

  cleanup)
    # Runs with `if: always()`. Removes every ephemeral key and working file.
    rm -rf "$BACKUP_CI_DIR"
    echo "ephemeral keys, archives and logs removed from $BACKUP_CI_DIR"
    ;;

  *)
    echo "usage: $0 {backup|restore-wrong-key|restore-tampered|restore-refused|restore|cleanup}"
    exit 2
    ;;
esac
