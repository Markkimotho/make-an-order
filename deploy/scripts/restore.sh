#!/usr/bin/env bash
# Restore the database from a backup: stop the app, restore, start the app.
#
#   deploy/scripts/restore.sh                      list the backups
#   deploy/scripts/restore.sh mao-20261126-031500.sql.gz
#   deploy/scripts/restore.sh /media/usb/mao-20261126-031500.sql.gz
#
# A file name alone means a backup in the backups volume. A path to a file on
# this computer (a USB disk) is mounted into the container read-only.
# Before restoring, the current database is saved as a "prerestore" backup.
set -euo pipefail

cd "$(dirname "$0")/.."                                   # deploy/
compose=${COMPOSE:-"docker compose"}

if [ ! -f .env ]; then
  echo "deploy/.env not found. Run this on the server, from the install folder." >&2
  exit 2
fi
db_name="$(grep -E '^MYSQL_DB=' .env | tail -n1 | cut -d= -f2- | tr -d '[:space:]"')"
db_name="${db_name:-mao}"

if [ $# -lt 1 ]; then
  echo "Backups on the server:"
  $compose run --rm -T app flask backup list
  echo
  echo "Usage: $0 <backup file name or path>"
  exit 2
fi

source="$1"
mount_args=()
target="$source"
if [ -f "$source" ]; then
  dir="$(cd "$(dirname "$source")" && pwd)"
  target="/restore/$(basename "$source")"
  mount_args=(-v "$dir:/restore:ro")
fi

echo "This will:"
echo "  1. stop the app, the backup loop and the jobs loop (tills stop working)"
echo "  2. save the current database as a 'prerestore' backup"
echo "  3. replace ALL data in database '$db_name' with $(basename "$source")"
echo "  4. start everything again (migrations run on start)"
read -r -p "Type the database name ($db_name) to continue: " typed
if [ "$typed" != "$db_name" ]; then
  echo "Cancelled: the name did not match. Nothing was changed."
  exit 1
fi

$compose stop app backup jobs

if $compose run --rm -T ${mount_args[@]+"${mount_args[@]}"} app \
     flask backup restore "$target" --yes --confirm-name "$typed"; then
  $compose up -d app backup jobs
  echo "Restore finished. Check the app, then tell the tills to reload."
else
  echo
  echo "RESTORE FAILED. The app is left stopped so nobody sells on a half-restored database."
  echo "Fix the problem and run this script again, or put the old data back with the"
  echo "newest '-prerestore' backup:  $0 <that file name>"
  echo "Start the app without restoring: (cd deploy && $compose up -d)"
  exit 1
fi
