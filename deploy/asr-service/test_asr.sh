#!/usr/bin/env bash
# --------------------------------------------------------------------------- #
# Test script — run on asr-host after deployment
# --------------------------------------------------------------------------- #
set -euo pipefail
BASE="http://localhost:8000"

echo "=== Health Check ==="
curl -s "${BASE}/health" | python3 -m json.tool

echo ""
echo "=== Model List ==="
curl -s "${BASE}/v1/models" | python3 -m json.tool

echo ""
echo "=== Transcription Test ==="
# Download a test audio file if not present
TEST_AUDIO="/tmp/test_audio.wav"
if [ ! -f "$TEST_AUDIO" ]; then
    curl -sL "https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/BAC009S0764W0121.wav" -o "$TEST_AUDIO"
fi
echo "Audio: $(file "$TEST_AUDIO")"

curl -s "${BASE}/v1/audio/transcriptions" \
    -F "file=@${TEST_AUDIO}" \
    -F "model=sensevoice" \
    -F "response_format=verbose_json" \
    | python3 -m json.tool

echo ""
echo "=== All tests passed ==="
