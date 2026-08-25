#!/usr/bin/env bash
# Start, stop, or restart LMS Request. Tracks the pid in lmsrequest.pid.
#
# Note: do not use `pkill -f uvicorn...` to stop this -- the pattern matches the
# shell running the command too, so it kills your own session. Hence the pidfile.
set -euo pipefail
cd "$(dirname "$0")"

# Local overrides without touching committed files. .env is gitignored; it takes
# the same LMSREQUEST_* variables as the container.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PIDFILE=lmsrequest.pid
PORT=$(.venv/bin/python -c "import tomllib;print(tomllib.load(open('config.toml','rb'))['server']['port'])")
BIND=$(.venv/bin/python -c "import tomllib;print(tomllib.load(open('config.toml','rb'))['server']['bind'])")

stop() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
    kill "$(cat $PIDFILE)"; sleep 1
  fi
  # Belt and braces: whatever still holds the port.
  local pid
  pid=$(ss -H -tlnp "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)
  [ -n "${pid:-}" ] && kill "$pid" && sleep 1 || true
  rm -f "$PIDFILE"
}

start() {
  # --no-proxy-headers: uvicorn trusts X-Forwarded-For from 127.0.0.1 by
  # default and rewrites the client address before the app sees it, which
  # silently overrode LMS Request's own trust_forwarded_for setting.
  # `python -m uvicorn` rather than .venv/bin/uvicorn: console scripts bake in
  # an absolute shebang, so they break if this folder is ever moved.
  nohup .venv/bin/python -m uvicorn lmsrequest.app:app --host "$BIND" --port "$PORT" \
    --no-proxy-headers >> lmsrequest.log 2>&1 &
  echo $! > "$PIDFILE"
  sleep 2
  echo "LMS Request on http://$(hostname -I | awk '{print $1}'):$PORT  (pid $(cat $PIDFILE))"
}

case "${1:-restart}" in
  start) start ;;
  stop) stop; echo stopped ;;
  restart) stop; start ;;
  log) tail -f lmsrequest.log ;;
  *) echo "usage: $0 {start|stop|restart|log}"; exit 1 ;;
esac
