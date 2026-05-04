#!/bin/bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

clear
echo -e "${GREEN}"
cat << 'ART'

__╱╲╲╲╲╲╲╲╲╲╲╲╲╲____╱╲╲╲╲╲╲╲╲╲╲╲╲╲╲╲________╱╲╲╲╲╲╲╲╲╲____________╱╲╲╲________╱╲╲╲________╱╲╲╲╲╲╲_____╱╲╲╲╲╲╲_________________________________
 _╲╱╲╲╲╱╱╱╱╱╱╱╱╱╲╲╲_╲╱╱╱╱╱╱╱╲╲╲╱╱╱╱╱______╱╲╲╲╱╱╱╱╱╱╱╱____________╲╱╲╲╲_____╱╲╲╲╱╱________╲╱╱╱╱╲╲╲____╲╱╱╱╱╲╲╲_________________________________
  _╲╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲_________╱╲╲╲╱_____________________╲╱╲╲╲__╱╲╲╲╱╱______╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲_________________________________
   _╲╱╲╲╲╲╲╲╲╲╲╲╲╲╲╲________╲╱╲╲╲________╱╲╲╲_______________________╲╱╲╲╲╲╲╲╱╱╲╲╲_____╲╱╱╱_____╲╱╲╲╲_______╲╱╲╲╲________╱╲╲╲╲╲╲╲╲___╱╲╲╱╲╲╲╲╲╲╲__
    _╲╱╲╲╲╱╱╱╱╱╱╱╱╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲_______________________╲╱╲╲╲╱╱_╲╱╱╲╲╲_____╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲______╱╲╲╲╱╱╱╱╱╲╲╲_╲╱╲╲╲╱╱╱╱╱╲╲╲_
     _╲╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲_______╲╱╱╲╲╲______________________╲╱╲╲╲____╲╱╱╲╲╲___╲╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲_____╱╲╲╲╲╲╲╲╲╲╲╲__╲╱╲╲╲___╲╱╱╱__
      _╲╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲________╲╱╱╱╲╲╲____________________╲╱╲╲╲_____╲╱╱╲╲╲__╲╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲____╲╱╱╲╲╱╱╱╱╱╱╱___╲╱╲╲╲_________
       _╲╱╲╲╲╲╲╲╲╲╲╲╲╲╲╱________╲╱╲╲╲__________╲╱╱╱╱╲╲╲╲╲╲╲╲╲___________╲╱╲╲╲______╲╱╱╲╲╲_╲╱╲╲╲__╱╲╲╲╲╲╲╲╲╲__╱╲╲╲╲╲╲╲╲╲__╲╱╱╲╲╲╲╲╲╲╲╲╲_╲╱╲╲╲_________
        _╲╱╱╱╱╱╱╱╱╱╱╱╱╱__________╲╱╱╱______________╲╱╱╱╱╱╱╱╱╱____________╲╱╱╱________╲╱╱╱__╲╱╱╱__╲╱╱╱╱╱╱╱╱╱__╲╱╱╱╱╱╱╱╱╱____╲╱╱╱╱╱╱╱╱╱╱__╲╱╱╱__________
ART
echo -e "${NC}"

# ── If already configured, ask to reconfigure ─────────────────────────────
if [ -f ".env" ]; then
    echo -e "  ${GREEN}✓ Existing config found${NC}"
    read -p "  Run a fresh setup? [y/N] " REDO
    REDO="${REDO:-N}"
    if [[ ! "$REDO" =~ ^[Yy]$ ]]; then
        # Just make sure deps + command are current, then launch
        echo -e "  ${CYAN}Updating dependencies...${NC}"
        venv/bin/python3 -m pip install -r requirements.txt --quiet --upgrade 2>/dev/null || true
        _register_command
        echo ""
        echo -e "  ${GREEN}${BOLD}Ready.${NC} Launching..."
        echo ""
        export PATH="$HOME/.local/bin:$PATH"
        exec "$HOME/.local/bin/btc-killer"
    fi
    echo ""
fi

# ── Python ─────────────────────────────────────────────────────────────────
PYTHON=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" &>/dev/null; then
        VER=$("$c" --version 2>&1 | awk '{print $2}')
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "${MINOR:-0}" -ge 10 ]; then PYTHON="$c"; break; fi
    fi
done
[ -z "$PYTHON" ] && { echo -e "${RED}  Python 3.10+ not found — install from python.org${NC}"; exit 1; }

# ── Venv + deps ────────────────────────────────────────────────────────────
[ ! -d "venv" ] && "$PYTHON" -m venv venv
echo -e "  ${CYAN}Installing dependencies...${NC}"
venv/bin/python3 -m pip install -r requirements.txt --quiet --upgrade
echo -e "  ${GREEN}✓ Done${NC}"
echo ""

# ── API keys ───────────────────────────────────────────────────────────────
echo -e "  ${BOLD}API Keys${NC}"
echo -e "  ${CYAN}kalshi.com → Settings → API  |  coinalyze.net (free)${NC}"
echo ""
read -p "  Kalshi API Key ID:        " KALSHI_KEY_ID
read -p "  Kalshi private key path:  " KALSHI_PEM
KALSHI_PEM="${KALSHI_PEM/#\~/$HOME}"
while [ ! -f "$KALSHI_PEM" ]; do
    echo -e "  ${RED}  not found: $KALSHI_PEM${NC}"
    read -p "  Kalshi private key path:  " KALSHI_PEM
    KALSHI_PEM="${KALSHI_PEM/#\~/$HOME}"
done
read -p "  Coinalyze API Key:        " COINALYZE_KEY
read -p "  Daily loss limit [\$50]:   " DAILY_LOSS;   DAILY_LOSS="${DAILY_LOSS:-50}"
read -p "  Max contracts/trade [200]: " MAX_C;        MAX_C="${MAX_C:-200}"

echo ""
echo -e "  ${BOLD}Telegram Alerts${NC} ${CYAN}(optional — press Enter to skip)${NC}"
echo -e "  ${CYAN}Create a bot via @BotFather, then message it to get your user ID${NC}"
echo ""
read -p "  Telegram bot token:       " TG_TOKEN
TG_ENABLED="false"
TG_USERS=""
if [ -n "$TG_TOKEN" ]; then
    read -p "  Allowed user IDs (comma-separated): " TG_USERS
    TG_ENABLED="true"
    echo -e "  ${GREEN}✓ Telegram enabled${NC}"
else
    echo -e "  ${YELLOW}  Skipping Telegram (can add later via setup)${NC}"
fi

cat > .env << EOF
KALSHI_API_KEY_ID=$KALSHI_KEY_ID
KALSHI_PRIVATE_KEY_PATH=$KALSHI_PEM
COINALYZE_API_KEY=$COINALYZE_KEY
DAILY_LOSS_LIMIT=$DAILY_LOSS
MAX_CONTRACTS_PER_TRADE=$MAX_C
TELEGRAM_ENABLED=$TG_ENABLED
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_ALLOWED_USERS=$TG_USERS
EOF
echo ""
echo -e "  ${GREEN}✓ Config saved${NC}"
echo ""

# ── Register btc-killer command ────────────────────────────────────────────
_register_command() {
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/btc-killer" << EOF2
#!/bin/bash
CERT_PATH=\$(${DIR}/venv/bin/python3 -c "import certifi; print(certifi.where())" 2>/dev/null)
[ ! -f "\$CERT_PATH" ] && CERT_PATH="/etc/ssl/cert.pem"
export SSL_CERT_FILE="\$CERT_PATH"
export REQUESTS_CA_BUNDLE="\$CERT_PATH"
exec ${DIR}/venv/bin/python3 ${DIR}/cli.py "\$@"
EOF2
    chmod +x "$HOME/.local/bin/btc-killer"
    for RC in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
        if [ -f "$RC" ] && ! grep -q '.local/bin' "$RC" 2>/dev/null; then
            printf '\n# btc-killer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
        fi
    done
    # Also handle missing .zshrc
    if [ ! -f "$HOME/.zshrc" ] && ! grep -q '.local/bin' "$HOME/.zshrc" 2>/dev/null; then
        printf '\n# btc-killer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.zshrc"
    fi
}
_register_command
echo -e "  ${GREEN}✓ 'btc-killer' registered — works from any terminal after this${NC}"
echo ""
echo -e "  ${GREEN}${BOLD}Setup complete.${NC}"
echo ""
export PATH="$HOME/.local/bin:$PATH"
exec "$HOME/.local/bin/btc-killer"
