#!/usr/bin/env bash
# Supervisor do netmon, chamado pelo cron a cada minuto e no @reboot.
#
#  - o flock garante instancia unica: se ninguem segura o lock, este processo
#    vira o netmon; se alguem ja segura, seguimos para a checagem de saude
#  - se a API nao responder 3 minutos seguidos, mata o processo travado e o
#    minuto seguinte o reergue

set -u
DIR="/home/orangepi/netmon"
LOCK="$DIR/netmon.lock"
LOG="$DIR/netmon.log"
PIDF="$DIR/netmon.pid"
FAIL="$DIR/health.fail"
PY="/usr/bin/python3"

exec 9>"$LOCK" || exit 1

if flock -n 9; then
    rm -f "$FAIL"
    exec "$PY" "$DIR/netmon.py" >> "$LOG" 2>&1
fi

# --- ja existe uma instancia: checa se ela ainda responde -------------------
saudavel=1
for porta in 666 8666; do
    if "$PY" - "$porta" <<'EOF' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:%s/api/health" % sys.argv[1], timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
EOF
    then
        saudavel=0
        break
    fi
done

if [ "$saudavel" -eq 0 ]; then
    rm -f "$FAIL"
    exit 0
fi

n=$(cat "$FAIL" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$FAIL"
echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG falha de saude ($n/3)" >> "$LOG"

if [ "$n" -ge 3 ] && [ -f "$PIDF" ]; then
    pid=$(cat "$PIDF")
    echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG matando processo travado $pid" >> "$LOG"
    kill -9 "$pid" 2>/dev/null
    rm -f "$FAIL" "$PIDF"
fi
exit 0
