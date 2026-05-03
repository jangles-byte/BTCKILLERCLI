#!/bin/bash
# push.sh — push BTC.KILLER CLI to GitHub (safe, no credentials)

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

echo ""
echo -e "${BOLD}  BTC.KILLER — Push to GitHub${NC}"
echo ""

# ── Safety check: make sure nothing sensitive is staged ───────────────────
DANGEROUS=(".env" "*.pem" "*.key" "bot_config.json")
FOUND=""
for pattern in "${DANGEROUS[@]}"; do
    while IFS= read -r -d '' file; do
        FOUND="$FOUND\n    $file"
    done < <(find . -maxdepth 2 -name "$pattern" -not -path "./.git/*" -print0 2>/dev/null)
done

if [ -n "$FOUND" ]; then
    echo -e "${YELLOW}  Sensitive files detected (excluded by .gitignore):${NC}"
    echo -e "$FOUND"
    echo ""
    # Verify they're actually ignored
    while IFS= read -r -d '' file; do
        rel="${file#./}"
        if git check-ignore -q "$rel" 2>/dev/null; then
            echo -e "  ${GREEN}✓ $rel is gitignored — safe${NC}"
        else
            echo -e "  ${RED}✗ $rel is NOT gitignored — aborting!${NC}"
            echo -e "  ${RED}  Add it to .gitignore before pushing.${NC}"
            exit 1
        fi
    done < <(find . -maxdepth 2 \( -name "*.env" -o -name "*.pem" -o -name "*.key" -o -name "bot_config.json" \) -not -path "./.git/*" -print0 2>/dev/null)
    echo ""
fi

# ── Init git if needed ─────────────────────────────────────────────────────
if [ ! -d ".git" ]; then
    git init
    git branch -M main
    echo -e "  ${GREEN}✓ Git repo initialized${NC}"
fi

# ── Set remote ─────────────────────────────────────────────────────────────
REPO_URL="https://github.com/jangles-byte/BTCKILLERCLI.git"
CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [ -z "$CURRENT_REMOTE" ]; then
    git remote add origin "$REPO_URL"
    echo -e "  ${GREEN}✓ Remote: $REPO_URL${NC}"
else
    git remote set-url origin "$REPO_URL"
    echo -e "  ${GREEN}✓ Remote: $REPO_URL${NC}"
fi

echo ""

# ── Stage, commit, push ────────────────────────────────────────────────────
git add .

# Show what's being committed
echo -e "  Files to commit:"
git diff --cached --name-only | sed 's/^/    /'
echo ""

read -p "  Commit message [initial commit]: " MSG
MSG="${MSG:-initial commit}"

git commit -m "$MSG" 2>/dev/null || echo -e "  ${YELLOW}Nothing new to commit${NC}"
git push -u origin main

echo ""
echo -e "  ${GREEN}${BOLD}✓ Pushed to GitHub${NC}"
echo ""
