#!/usr/bin/env bash
set -euo pipefail
umask 077

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
logs="$root/data/chaser"
mkdir -p "$logs"
cd "$root/integrations/supplier-chaser"

running() {
    local pid
    [[ -s "$logs/$1.pid" ]] && read -r pid < "$logs/$1.pid" &&
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 -- "-$pid" 2>/dev/null
}

stop() {
    for task in api worker; do
        if running "$task"; then
            kill -TERM -- "-$(cat "$logs/$task.pid")"
            for ((attempt = 0; attempt < 100; attempt++)); do
                running "$task" || break
                sleep 0.1
            done
            if running "$task"; then
                printf 'Supplier Chaser %s is still stopping. Try again shortly.\n' "$task" >&2
                return 1
            fi
        fi
        rm -f -- "$logs/$task.pid"
    done
}

case "${1:-start}" in
    stop) stop; exit ;;
    restart) stop ;;
    start) ;;
    *) printf 'Usage: %s [start|stop|restart]\n' "$0" >&2; exit 2 ;;
esac

if running api && running worker; then
    printf 'Supplier Chaser is running at http://127.0.0.1:8000.\n'
    exit
fi

# Process-level settings also take precedence over inherited environment variables.
export SUPPLIER_CHASER_ENV_FILE=.env MAIL_MODE=fixture DRY_RUN=true OUTBOUND_MAIL_ENABLED=false
pixi run db-local
pixi run db-upgrade
if [[ ! -f .env ]]; then
    pixi run demo-seed
fi

start() {
    local task=$1
    shift
    if running "$task"; then return; fi
    nohup setsid pixi run "$@" >> "$logs/$task.log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$logs/$task.pid"
}

start api uvicorn supplier_chaser.web.app:create_app --factory --host 127.0.0.1 --port 8000
start worker worker
printf 'Starting Supplier Chaser at http://127.0.0.1:8000. Logs: %s\n' "$logs"
