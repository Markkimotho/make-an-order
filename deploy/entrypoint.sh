#!/bin/sh
# Container entrypoint. Modes:
#   web          apply migrations, then serve with gunicorn on :8000 (default)
#   backup-loop  every BACKUP_CHECK_MINUTES, back up if the newest backup is
#                older than BACKUP_EVERY_HOURS, check it, then rotate
#   jobs-loop    payments-sweep every 10 minutes; run-daily-triggers once a
#                day after 06:00 Nairobi time (it is safe to rerun)
#   anything else is run as a command, e.g. `flask backup list`
set -eu

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"; }

mode="${1:-web}"
case "$mode" in
  web)
    flask db upgrade
    exec gunicorn app:app \
      --bind 0.0.0.0:8000 \
      --workers "${WEB_CONCURRENCY:-3}" \
      --timeout "${GUNICORN_TIMEOUT:-120}" \
      --access-logfile - --error-logfile -
    ;;
  backup-loop)
    interval=$(( ${BACKUP_CHECK_MINUTES:-60} * 60 ))
    log "backup loop: checking every ${BACKUP_CHECK_MINUTES:-60} min, backing up every ${BACKUP_EVERY_HOURS:-20} h"
    while true; do
      if flask backup create --if-older-than "${BACKUP_EVERY_HOURS:-20}" \
         && flask backup verify latest \
         && flask backup rotate; then
        :
      else
        log "backup loop: FAILED (the Backup page will warn once the last backup is 26 h old)"
      fi
      sleep "$interval"
    done
    ;;
  jobs-loop)
    last_daily=""
    log "jobs loop: payments-sweep every 10 min, daily triggers after 06:00 Nairobi"
    while true; do
      flask payments-sweep || log "payments-sweep failed"
      today=$(python -c "from services.clock import now_nairobi; n = now_nairobi(); print(n.date() if n.hour >= 6 else '')")
      if [ -n "$today" ] && [ "$today" != "$last_daily" ]; then
        if flask run-daily-triggers; then last_daily="$today"; else log "run-daily-triggers failed"; fi
      fi
      sleep 600
    done
    ;;
  *)
    exec "$@"
    ;;
esac
