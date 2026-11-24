#!/usr/bin/env bash
# Private, throwaway MySQL server for running the test suite locally.
#
# Uses the Homebrew mysqld binary with its own data directory, port and
# socket, so it never touches a MySQL server you already run.
#
#   scripts/local_mysql.sh start   # initialise (first time) and start
#   scripts/local_mysql.sh url     # print TEST_MYSQL_URL for pytest
#   scripts/local_mysql.sh stop    # stop it (data dir is kept)
#   scripts/local_mysql.sh wipe    # stop and delete the data dir
#
#   TEST_MYSQL_URL=$(scripts/local_mysql.sh url) pytest -q
set -euo pipefail

DATADIR="${MAO_MYSQL_DATADIR:-/tmp/mao-mysql}"
PORT="${MAO_MYSQL_PORT:-33071}"
SOCKET="$DATADIR.sock"
PIDFILE="$DATADIR.pid"
LOG="$DATADIR.log"
MYSQLD="${MYSQLD:-$(command -v mysqld || true)}"

die() { echo "local_mysql: $*" >&2; exit 1; }

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    [[ -n "$MYSQLD" ]] || die "mysqld not found (brew install mysql)"
    if running; then echo "already running on port $PORT"; exit 0; fi
    if [[ ! -d "$DATADIR/mysql" ]]; then
      mkdir -p "$DATADIR"
      "$MYSQLD" --no-defaults --initialize-insecure --datadir="$DATADIR" >"$LOG" 2>&1 \
        || die "initialise failed, see $LOG"
    fi
    "$MYSQLD" --no-defaults --datadir="$DATADIR" --port="$PORT" --bind-address=127.0.0.1 \
      --socket="$SOCKET" --pid-file="$PIDFILE" --mysqlx=OFF >>"$LOG" 2>&1 &
    for _ in $(seq 1 60); do
      if mysqladmin --no-defaults -uroot --socket="$SOCKET" ping >/dev/null 2>&1; then
        echo "started on 127.0.0.1:$PORT (data $DATADIR)"; exit 0
      fi
      sleep 0.5
    done
    die "did not become ready, see $LOG"
    ;;
  url)
    echo "mysql+pymysql://root@127.0.0.1:$PORT"
    ;;
  stop)
    if running; then
      mysqladmin --no-defaults -uroot --socket="$SOCKET" shutdown || kill "$(cat "$PIDFILE")"
      echo "stopped"
    else
      echo "not running"
    fi
    ;;
  wipe)
    "$0" stop
    rm -rf "$DATADIR" "$SOCKET" "$PIDFILE" "$LOG"
    echo "wiped $DATADIR"
    ;;
  *)
    die "usage: $0 start|url|stop|wipe"
    ;;
esac
