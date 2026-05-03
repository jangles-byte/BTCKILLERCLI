#!/bin/bash
# run.sh — use this if you haven't run setup.sh yet
# After setup, just type: btc-killer
cd "$(dirname "$0")"
if [ ! -d "venv" ] || [ ! -f ".env" ]; then
    echo "Run setup first:  bash setup.sh"
    exit 1
fi
CERT_PATH=$(venv/bin/python3 -c "import certifi; print(certifi.where())" 2>/dev/null)
if [ ! -f "$CERT_PATH" ]; then CERT_PATH="/etc/ssl/cert.pem"; fi
export SSL_CERT_FILE="$CERT_PATH"
export REQUESTS_CA_BUNDLE="$CERT_PATH"
exec venv/bin/python3 cli.py
