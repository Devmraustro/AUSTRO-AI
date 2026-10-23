#!/bin/bash
# REFUSED - this script no longer restores anything.
#
# The previous version dropped and recreated DB_NAME, which defaults to the
# APPLICATION database, using a `postgres` superuser assumption and no
# integrity gate. Restores now go through scripts/pg_restore_drill.py, which
# requires an explicit disposable target, exact destructive authorisation,
# explicit administrative credentials, and full archive authentication before
# any database change. See DISASTER_RECOVERY.md.
echo "restore.sh is disabled: use scripts/pg_restore_drill.py with RESTORE_TARGET_DB," >&2
echo "RESTORE_CONFIRM_DESTRUCTIVE, RESTORE_ADMIN_USER and RESTORE_ADMIN_PASSWORD." >&2
exit 2
