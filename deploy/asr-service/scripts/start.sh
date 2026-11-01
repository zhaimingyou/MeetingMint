#!/usr/bin/env bash
# ASR Service start/stop/status helper
set -euo pipefail
SERVICE_NAME="asr-service"
VENV="/opt/asr-service/.venv"
export MODELSCOPE_CACHE="/opt/asr-service/models/modelscope"

case "${1:-status}" in
    start)
        echo "Starting ASR service..."
        nohup "${VENV}/bin/funasr-server" \
            --model sensevoice \
            --device cuda:0 \
            --host 0.0.0.0 \
            --port 8000 \
            > /opt/asr-service/logs/server.log 2>&1 &
        echo "PID: $!"
        ;;
    stop)
        echo "Stopping ASR service..."
        pkill -f funasr-server && echo "Stopped." || echo "Not running."
        ;;
    status)
        if pgrep -f funasr-server > /dev/null; then
            echo "ASR service is RUNNING (PID: $(pgrep -f funasr-server))"
            curl -sf http://localhost:8000/health && echo "" || echo "Health check FAILED"
        else
            echo "ASR service is STOPPED"
        fi
        ;;
    restart)
        "$0" stop
        sleep 2
        "$0" start
        ;;
    test)
        echo "=== Health ==="
        curl -s http://localhost:8000/health | python3 -m json.tool
        echo "=== Models ==="
        curl -s http://localhost:8000/v1/models | python3 -m json.tool
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart|test}"
        exit 1
        ;;
esac
