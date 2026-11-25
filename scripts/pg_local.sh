#!/usr/bin/env bash
# 用户态 PostgreSQL 生命周期管理（无需 root / docker）。
# 用法: scripts/pg_local.sh start|stop|status|psql
set -euo pipefail

H="$HOME"
PGBIN="$H/pgsql/usr/lib/postgresql/15/bin"
export LD_LIBRARY_PATH="$H/pgsql/usr/lib/x86_64-linux-gnu:$H/pgsql/usr/lib/postgresql/15/lib"
PGDATA="$H/pgdata"
SOCKET=/tmp
PORT=55432

case "${1:-status}" in
  start)
    if [ -S "$SOCKET/.s.PGSQL.$PORT" ]; then echo "already running"; exit 0; fi
    "$PGBIN/pg_ctl" -D "$PGDATA" -l /tmp/pg.log -w start
    ;;
  stop)
    "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop
    ;;
  status)
    "$PGBIN/pg_isready" -h "$SOCKET" -p "$PORT" -U pguser
    ;;
  psql)
    exec "$PGBIN/psql" -h "$SOCKET" -p "$PORT" -U pguser -d bench "${@:2}"
    ;;
  *)
    echo "usage: $0 start|stop|status|psql" >&2; exit 1 ;;
esac
