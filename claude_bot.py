"""
ClaudeBot — Autonomous AI trading agent for Kalshi BTC 15M markets.
Claude IS the bot. No signal engine. No conviction scoring.
Claude sees raw market data, price history, and account state — then decides.

Hard limits enforced here (wallet floor, daily loss) — Claude cannot override.
"""

import os
import subprocess
import time
import json
import base64
import requests
import csv
import threading
from pathlib import Path
from datetime import datetime, date, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
from signals import start_feed_thread, get_signal   # still used for raw price feed

load_dotenv()

API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
BASE_URL         = "https://api.elections.kalshi.com/trade-api/v2"
MAX_CONTRACTS    = int(os.getenv("MAX_CONTRACTS_PER_TRADE", 200))

with open(PRIVATE_KEY_PATH, "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)

BOT_DIR = Path(__file__).resolve().parent

# ── Shared strategy log (read chat advice, write bot decisions) ───────────────
_STRATEGY_LOG  = BOT_DIR / "claude_strategy_log.json"
_BOT_DECISIONS = BOT_DIR / "claude_bot_decisions.json"
_MAX_LOG       = 15

def _read_strategy_log() -> list:
    try:
        if _STRATEGY_LOG.exists():
            return json.loads(_STRATEGY_LOG.read_text()).get("entries", [])
    except Exception:
        pass
    return []

def _write_bot_decision(decision_str: str, context: str) -> None:
    """Log a bot decision so the chat Claude can read it."""
    try:
        existing = []
        if _BOT_DECISIONS.exists():
            existing = json.loads(_BOT_DECISIONS.read_text()).get("decisions", [])
        existing.append({
            "ts":       datetime.now().isoformat(timespec="seconds"),
            "decision": decision_str,
            "context":  context,
        })
        existing = existing[-_MAX_LOG:]
        _BOT_DECISIONS.write_text(json.dumps({"decisions": existing}, indent=2))
        # Also append to shared log so chat sees it
        entries = _read_strategy_log()
        entries.append({
            "ts":      datetime.now().isoformat(timespec="seconds"),
            "source":  "bot",
            "user":    "",
            "content": f"{decision_str}  [{context}]",
        })
        entries = entries[-_MAX_LOG:]
        _STRATEGY_LOG.write_text(json.dumps({"entries": entries}, indent=2))
    except Exception:
        pass

def _format_chat_log_for_bot() -> str:
    """Format recent co-pilot chat exchanges for the bot prompt."""
    entries = _read_strategy_log()
    if not entries:
        return "  (no co-pilot advice yet — chat with Claude in the dashboard to send strategy)"
    lines = []
    for e in entries[-10:]:
        ts  = e.get("ts", "")[-8:]
        src = e.get("source", "?").upper()
        if src == "CHAT":
            user = e.get("user", "")
            resp = e.get("content", "")
            if user:
                lines.append(f"  [{ts}] USER TOLD CO-PILOT: {user}")
            lines.append(f"  [{ts}] CO-PILOT SAID: {resp}")
        elif src == "BOT":
            lines.append(f"  [{ts}] YOU (BOT) PREVIOUSLY DECIDED: {e.get('content','')}")
    return "\n".join(lines) or "  (no entries)"

# ── Auth ──────────────────────────────────────────────────────────────────

def sign_request(method, path):
    ts  = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}{path}".encode()
    sig = PRIVATE_KEY.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type":            "application/json",
    }

# ── Kalshi API ─────────────────────────────────────────────────────────────

def load_config():
    p = BOT_DIR / "bot_config.json"
    return json.load(open(p)) if p.exists() else {}

def get_balance():
    path = "/trade-api/v2/portfolio/balance"
    r = requests.get(BASE_URL + "/portfolio/balance",
                     headers=sign_request("GET", path), timeout=5)
    return r.json().get("balance", 0) / 100

def find_current_market():
    path = "/trade-api/v2/markets"
    r = requests.get(
        BASE_URL + "/markets",
        headers=sign_request("GET", path),
        params={"series_ticker": "KXBTC15M", "status": "open", "limit": 5},
        timeout=10,
    )
    r.raise_for_status()
    markets = r.json().get("markets", [])
    now = datetime.now(timezone.utc)
    best, best_diff = None, float("inf")
    for m in markets:
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        diff  = (close - now).total_seconds()
        if 0 < diff < best_diff:
            best_diff, best = diff, m
    return best, best_diff

def get_market_prices(ticker):
    path = "/trade-api/v2/markets/" + ticker
    r = requests.get(BASE_URL + "/markets/" + ticker,
                     headers=sign_request("GET", path), timeout=5)
    m = r.json().get("market", r.json())
    return float(m.get("yes_ask_dollars", 0.5)), float(m.get("no_ask_dollars", 0.5))

def place_order(ticker, side, price_dollars, num_contracts):
    path  = "/trade-api/v2/portfolio/orders"
    price = min(float(price_dollars) + 0.04, 0.99)
    key   = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    body  = json.dumps({
        "ticker": ticker, "action": "buy", "side": side,
        "type": "limit", "count": num_contracts, key: f"{price:.4f}",
    })
    try:
        r = requests.post(BASE_URL + "/portfolio/orders",
                          headers=sign_request("POST", path), data=body, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": {"details": str(e)}}

def sell_position(ticker, side, num_contracts, price_dollars):
    path = "/trade-api/v2/portfolio/orders"
    key  = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    body = json.dumps({
        "ticker": ticker, "action": "sell", "side": side,
        "type": "limit", "count": num_contracts, key: f"{float(price_dollars):.4f}",
    })
    try:
        r = requests.post(BASE_URL + "/portfolio/orders",
                          headers=sign_request("POST", path), data=body, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": {"details": str(e)}}

def get_settled_pnl(ticker, side, contracts, entry_price):
    try:
        path = "/trade-api/v2/markets/" + ticker
        r    = requests.get(BASE_URL + "/markets/" + ticker,
                            headers=sign_request("GET", path), timeout=5)
        market = r.json().get("market", r.json())
        result = market.get("result", "")
        if not result:
            return None
        won = (result == side)
        return contracts * (1.0 - entry_price) if won else -(contracts * entry_price)
    except Exception as e:
        print(f"  Settlement error: {e}")
        return None

def write_bot_status(status, direction=None, mins_remaining=None):
    try:
        with open(BOT_DIR / "bot_status.json", "w") as f:
            json.dump({
                "status": status, "direction": direction,
                "mins_remaining": mins_remaining, "updated_at": time.time(),
                "mode": "claude_bot",
            }, f)
    except Exception:
        pass

# ── Trade logging ──────────────────────────────────────────────────────────

def get_trades_file():
    folder = BOT_DIR / "trades"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"trades_{datetime.now().strftime('%Y-%m-%d')}.csv"

def log_trade(ticker, side, price, contracts, note="claude_bot"):
    f   = get_trades_file()
    new = not f.exists()
    with open(f, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp", "ticker", "side", "price", "contracts", "pnl"])
        w.writerow([datetime.now().isoformat(), ticker, side, price, contracts, "pending"])

def update_trade_pnl(ticker, pnl):
    folder = BOT_DIR / "trades"
    if not folder.exists():
        return
    for f in sorted(folder.glob("trades_*.csv"), reverse=True):
        rows, updated = [], False
        with open(f, newline="") as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames
            for row in reader:
                if row.get("ticker") == ticker and row.get("pnl") == "pending":
                    row["pnl"] = f"{pnl:.4f}" if pnl is not None else "expired"
                    updated = True
                rows.append(row)
        if updated:
            with open(f, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
            return

# ── Price history (read from signals module state) ─────────────────────────
# We pull from the shared signal state so Claude sees the live price feed.

def get_price_history():
    try:
        from signals import _price_history  # deque maintained by signals.py
        return list(_price_history)[-60:]   # last 60 readings
    except Exception:
        return []

def get_btc_price():
    try:
        from signals import _last_price
        return _last_price
    except Exception:
        return None

def get_raw_signals():
    """Get raw signal data for Claude's context — not for decision-making, just context."""
    try:
        return get_signal.__doc__  # placeholder — we pass raw sig dict instead
    except Exception:
        return {}

# ── Claude decision engine ─────────────────────────────────────────────────

def ask_claude(ticker, mins_rem, yes_ask, no_ask, strike, strike_dir,
               balance, budget, pnl_today, daily_loss_limit,
               wallet_floor, wallet_floor_enabled,
               already_traded, our_side, our_entry, our_contracts,
               price_history, btc_price, raw_sig):
    """
    Ask Claude for a trading decision. Returns one of:
      ('hold',)
      ('trade', 'yes'|'no', dollars)
      ('sell',)
    """
    # Price history summary for Claude
    if price_history and len(price_history) >= 3:
        prices     = [p for p in price_history if p]
        p_min      = min(prices)
        p_max      = max(prices)
        p_change   = prices[-1] - prices[0] if len(prices) > 1 else 0
        p_last5    = prices[-5:] if len(prices) >= 5 else prices
        trend_txt  = f"min=${p_min:,.0f}  max=${p_max:,.0f}  Δ={p_change:+,.0f}  last5={[f'${p:,.0f}' for p in p_last5]}"
    else:
        trend_txt  = "building..."

    # Position context
    if already_traded and our_side:
        pos_txt = (f"OPEN: {our_side.upper()}  {our_contracts} contracts  "
                   f"entry={our_entry*100:.0f}¢  "
                   f"current={'YES ' + f'{yes_ask*100:.0f}¢' if our_side=='yes' else 'NO ' + f'{no_ask*100:.0f}¢'}")
    else:
        pos_txt = "None — no trade placed yet this market"

    # Raw signal summary
    sigs = raw_sig.get("signals", []) if isinstance(raw_sig, dict) else []
    sigs_txt = "\n".join(
        f"  {s.get('name','?')}: {s.get('yes_prob',0.5)*100:.0f}%  str={s.get('strength',0):.2f}  {s.get('reason','')}"
        for s in sigs[:8]
    ) or "  loading..."

    # Full signal breakdown
    pos_safety      = raw_sig.get("pos_safety", 0)
    safe_side       = raw_sig.get("safe_side", "unknown")
    sig_agreement   = raw_sig.get("signal_agreement", 0)
    sig_direction   = raw_sig.get("signal_direction", "unknown")
    our_yes_prob    = raw_sig.get("our_yes_prob", 0.5)
    our_no_prob     = raw_sig.get("our_no_prob", 0.5)
    confidence      = raw_sig.get("confidence", 0)
    distance        = raw_sig.get("distance", 0)
    time_factor     = raw_sig.get("time_factor", 0)

    # Position P&L estimate
    if already_traded and our_side and our_entry and our_contracts:
        current_price = yes_ask if our_side == "yes" else no_ask
        unrealized    = our_contracts * (current_price - our_entry)
        pos_pnl_txt   = f"unrealized P&L ≈ ${unrealized:+.2f}"
    else:
        pos_pnl_txt = ""

    prompt = f"""You are ClaudeBot — a fully autonomous AI trading agent on Kalshi BTC 15-minute prediction markets. You ARE the bot. You make ALL decisions.

You have access to web search and other tools if you need quick external context (e.g. macro news, BTC sentiment). Use them if relevant before deciding.

=== LIVE MARKET ===
Ticker    : {ticker}
Strike    : ${strike:,.0f}  ({strike_dir})
BTC price : ${btc_price:,.2f}  ({"ABOVE" if btc_price and btc_price > strike else "BELOW"} strike by ${abs((btc_price or 0) - strike):,.0f})
YES ask   : {yes_ask*100:.0f}¢    NO ask: {no_ask*100:.0f}¢
Time left : {mins_rem:.1f} minutes

=== BTC PRICE HISTORY ===
{trend_txt}

=== ALL SIGNALS ===
Individual signals (name / YES probability / strength / reason):
{sigs_txt}

Aggregated signal output:
  Our YES probability : {our_yes_prob*100:.0f}%   NO probability: {our_no_prob*100:.0f}%
  Signal agreement    : {sig_agreement:.2f}  (1.0 = all signals agree)
  Signal direction    : {sig_direction}
  Positional safety   : {pos_safety:.2f}  (how far BTC is from strike; higher = safer)
  Safe side           : {safe_side}  (which side the price favors)
  Distance to strike  : ${distance:,.0f}
  Time factor         : {time_factor:.2f}  (1.0 = close to expiry, 0 = lots of time)
  Signal confidence   : {confidence:.2f}

=== ACCOUNT ===
Balance        : ${balance:.2f}
Budget left    : ${budget:.2f}  (max you can spend this market)
Day P&L        : ${pnl_today:+.2f}
Daily loss lim : ${daily_loss_limit:.2f}  [HARD STOP]
Wallet floor   : ${wallet_floor:.2f}  [HARD STOP — floor active: {wallet_floor_enabled}]

=== CURRENT POSITION ===
{pos_txt}  {pos_pnl_txt}

=== CO-PILOT CHAT LOG (recent advice from the dashboard Claude + user) ===
This is what the trading co-pilot and the user have been discussing. This is YOUR strategy context.
Read it carefully — act on the advice, flag if you disagree, incorporate the reasoning.
{_format_chat_log_for_bot()}

=== YOUR PHILOSOPHY ===
Goal: maximize profit, minimize loss. Quality over quantity. Never guess.

What to use:
- Positional safety + safe_side: is BTC clearly on one side of the strike?
- Signal agreement + signal_direction: do signals agree, and which way?
- Individual signal breakdown: which signals are high-strength and why?
- Price momentum: is BTC moving toward or away from the strike?
- Time factor: late in the market, high time factor = less room for reversals
- Current odds: is YES or NO cheap relative to the true probability?

What NOT to use:
- EV (expected value) — this just rises as price drops, not a real edge signal
- Do not trade just because a metric looks good in isolation

When to sit out:
- Signals are mixed or weak (low agreement, low confidence)
- BTC is hovering near the strike with no clear direction
- There isn't enough time left to recover if wrong
- You genuinely don't know — sitting out IS a valid, smart decision

When to trade:
- Strong signal agreement pointing one direction
- BTC is clearly on the profitable side with time left
- Price is reasonable for the probability (not overpriced)
- Multiple high-strength signals confirm the same thesis

Position management:
- If already in a position: HOLD unless something has clearly changed against you
- SELL to cut losses if signals have flipped and you're wrong-sided with time left

=== RESPOND WITH EXACTLY ONE LINE ===
No explanation. No preamble. Just the decision:
  TRADE_YES <dollars>      (e.g. TRADE_YES 2.50)
  TRADE_NO <dollars>       (e.g. TRADE_NO 1.75)
  HOLD
  SELL"""

    try:
        print(f"  🤖 ClaudeBot: thinking... ({mins_rem:.1f}min left)")
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=45,
        )
        if proc.returncode != 0:
            print(f"  🤖 ClaudeBot CLI error — defaulting HOLD")
            return ("hold",)

        raw  = proc.stdout.strip()
        # Take last non-empty line to skip any preamble
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        resp  = lines[-1].upper() if lines else "HOLD"
        print(f"  🤖 ClaudeBot says: {resp}")

        ctx = (f"ticker={ticker} btc=${btc_price:,.0f} strike=${strike:,.0f} "
               f"YES={yes_ask*100:.0f}¢ NO={no_ask*100:.0f}¢ {mins_rem:.1f}min")

        if resp.startswith("TRADE_YES"):
            parts = resp.split()
            try:    amt = float(parts[1])
            except: amt = budget * 0.5
            amt = min(amt, budget)
            _write_bot_decision(f"TRADE_YES ${amt:.2f}", ctx)
            return ("trade", "yes", amt)
        elif resp.startswith("TRADE_NO"):
            parts = resp.split()
            try:    amt = float(parts[1])
            except: amt = budget * 0.5
            amt = min(amt, budget)
            _write_bot_decision(f"TRADE_NO ${amt:.2f}", ctx)
            return ("trade", "no", amt)
        elif resp.startswith("SELL"):
            _write_bot_decision("SELL", ctx)
            return ("sell",)
        else:
            _write_bot_decision("HOLD", ctx)
            return ("hold",)

    except subprocess.TimeoutExpired:
        print("  🤖 ClaudeBot: timed out — HOLD")
        return ("hold",)
    except Exception as e:
        print(f"  🤖 ClaudeBot error: {e} — HOLD")
        return ("hold",)

# ── Main loop ──────────────────────────────────────────────────────────────

def run_claude_bot():
    print("=" * 60)
    print("🤖 CLAUDE BOT — Autonomous AI trading agent")
    print("   Claude makes ALL decisions. Hard limits enforced here.")
    print("=" * 60)

    print("\nStarting price feed...")
    try:
        start_feed_thread()
    except Exception as e:
        print(f"  Feed error (continuing): {e}")
    print("Waiting 20 seconds for data to stabilize...")
    time.sleep(20)

    pnl_today         = 0.0
    killed            = False
    open_trades       = []
    last_market       = None
    last_mkt_time     = 0
    last_market_traded = None   # ticker of last market we placed a trade in

    market_state = {
        "ticker":    None,
        "traded":    False,
        "our_side":  None,
        "our_contracts": 0,
        "our_entry": 0.0,
    }

    while True:
        try:
            if killed:
                print("🤖 ClaudeBot: STOP — limit hit.")
                write_bot_status("killed")
                return

            cfg = load_config()

            # If claude_mode was turned off externally, exit gracefully
            if not cfg.get("claude_mode", False):
                print("🤖 ClaudeBot: claude_mode disabled in config — stopping.")
                write_bot_status("killed")
                return

            daily_loss_limit    = float(cfg.get("daily_loss_limit", 50.0))
            hard_stop_enabled   = bool(cfg.get("hard_stop_enabled", False))
            hard_stop_balance   = float(cfg.get("hard_stop_balance", 0.0))
            max_market_wager    = float(cfg.get("max_session_wager", 5.0))
            min_bet             = max(float(cfg.get("min_bet", 0.25)), 0.01)

            # Fetch balance
            try:
                balance = get_balance()
            except Exception:
                balance = 0.0

            # ── Hard stops ────────────────────────────────────────────────
            if hard_stop_enabled and balance <= hard_stop_balance:
                print(f"🤖 ClaudeBot: HARD STOP — balance ${balance:.2f} ≤ floor ${hard_stop_balance:.2f}")
                killed = True
                write_bot_status("killed")
                return

            if pnl_today <= -daily_loss_limit:
                print(f"🤖 ClaudeBot: DAILY LOSS LIMIT hit (${pnl_today:.2f}) — stopping")
                killed = True
                write_bot_status("killed")
                return

            # ── Settle open trades ────────────────────────────────────────
            still_open = []
            for t in open_trades:
                pnl = get_settled_pnl(t["ticker"], t["side"],
                                      t["contracts"], t["entry_price"])
                if pnl is not None:
                    pnl_today += pnl
                    update_trade_pnl(t["ticker"], pnl)
                    print(f"  SETTLED: {t['ticker']} → "
                          f"{'WIN' if pnl>0 else 'LOSS'} ${pnl:+.2f} | "
                          f"Day P&L: ${pnl_today:+.2f}")
                    pos_file = BOT_DIR / "current_position.json"
                    if pos_file.exists():
                        try:
                            pos = json.load(open(pos_file))
                            if pos.get("ticker") == t["ticker"]:
                                pos_file.unlink()
                        except Exception:
                            pass
                else:
                    still_open.append(t)
            open_trades = still_open

            # ── Refresh market every 10s ──────────────────────────────────
            now = time.time()
            if not last_market or (now - last_mkt_time) > 10:
                try:
                    last_market, secs_remaining = find_current_market()
                    last_mkt_time = now
                except Exception as e:
                    print(f"  Market refresh error: {e} — retrying in 10s")
                    time.sleep(10)
                    continue
            elif last_market:
                close = datetime.fromisoformat(
                    last_market["close_time"].replace("Z", "+00:00"))
                secs_remaining = (close - datetime.now(timezone.utc)).total_seconds()

            if not last_market or secs_remaining <= 0:
                print("🤖 No active market — waiting 15s")
                write_bot_status("idle")
                time.sleep(15)
                continue

            ticker   = last_market["ticker"]
            mins_rem = secs_remaining / 60
            yes_ask  = float(last_market.get("yes_ask_dollars", 0.5))
            no_ask   = float(last_market.get("no_ask_dollars",  0.5))
            strike   = last_market.get("floor_strike") or last_market.get("cap_strike") or 0
            s_type   = last_market.get("strike_type", "")
            strike_dir = ("above" if "greater" in s_type else
                          "below" if "less"    in s_type else s_type)

            # ── New market reset ──────────────────────────────────────────
            if ticker != market_state.get("ticker"):
                market_state = {
                    "ticker":        ticker,
                    "traded":        False,
                    "our_side":      None,
                    "our_contracts": 0,
                    "our_entry":     0.0,
                }
                print(f"\n  🤖 ── New market: {ticker} ──")

            # ── Already traded last market — skip until next ──────────────
            if ticker == last_market_traded:
                write_bot_status("traded", direction=market_state["our_side"],
                                 mins_remaining=mins_rem)
                print(f"  🤖 {ticker} | {mins_rem:.1f}min | holding position")
                time.sleep(30)
                continue

            # ── Budget ────────────────────────────────────────────────────
            budget = max_market_wager   # full budget per market

            # ── Get live price data ───────────────────────────────────────
            btc_price    = get_btc_price() or 0.0
            price_history = get_price_history()

            # ── Get raw signals (for Claude's context, not for deciding) ──
            try:
                raw_sig = get_signal(strike_price=strike, strike_type=s_type,
                                     mins_remaining=mins_rem,
                                     yes_ask=yes_ask, no_ask=no_ask)
            except Exception:
                raw_sig = {}

            # ── Ask Claude ────────────────────────────────────────────────
            decision = ask_claude(
                ticker, mins_rem, yes_ask, no_ask, strike, strike_dir,
                balance, budget, pnl_today, daily_loss_limit,
                hard_stop_balance, hard_stop_enabled,
                market_state["traded"], market_state["our_side"],
                market_state["our_entry"], market_state["our_contracts"],
                price_history, btc_price, raw_sig,
            )

            # ── Execute decision ──────────────────────────────────────────
            if decision[0] == "trade" and not market_state["traded"]:
                _, trade_side, trade_dollars = decision
                try:
                    live_yes, live_no = get_market_prices(ticker)
                    ask_price = live_yes if trade_side == "yes" else live_no
                except Exception:
                    ask_price = yes_ask if trade_side == "yes" else no_ask

                if ask_price > 0 and trade_dollars >= min_bet:
                    num_contracts = max(1, min(int(trade_dollars / ask_price), MAX_CONTRACTS))
                    cost = num_contracts * ask_price
                    print(f"\n  🤖 CLAUDE BOT FIRE: {trade_side.upper()} "
                          f"{num_contracts} contracts @ ${ask_price:.3f}  cost=${cost:.2f}")
                    result = place_order(ticker, trade_side, ask_price, num_contracts)
                    if "error" not in result:
                        log_trade(ticker, trade_side, ask_price, num_contracts)
                        open_trades.append({
                            "ticker": ticker, "side": trade_side,
                            "contracts": num_contracts, "entry_price": ask_price,
                        })
                        last_market_traded           = ticker
                        market_state["traded"]       = True
                        market_state["our_side"]     = trade_side
                        market_state["our_contracts"] = num_contracts
                        market_state["our_entry"]    = ask_price
                        # Write position file for dashboard
                        try:
                            with open(BOT_DIR / "current_position.json", "w") as f:
                                json.dump({
                                    "ticker": ticker, "side": trade_side,
                                    "contracts": num_contracts, "entry": ask_price,
                                    "cost": cost, "mins_remaining": mins_rem,
                                }, f)
                        except Exception:
                            pass
                        write_bot_status("traded", direction=trade_side, mins_remaining=mins_rem)
                    else:
                        print(f"  🤖 Order error: {result}")
                else:
                    print(f"  🤖 Trade skipped — bad price or below min bet")

            elif decision[0] == "sell" and market_state["traded"] and market_state["our_side"]:
                try:
                    live_yes, live_no = get_market_prices(ticker)
                    sell_side  = market_state["our_side"]
                    sell_price = live_yes if sell_side == "yes" else live_no
                    sell_qty   = market_state["our_contracts"]
                    if sell_qty > 0:
                        print(f"\n  🤖 CLAUDE BOT SELL: {sell_side.upper()} "
                              f"{sell_qty} @ ${sell_price:.3f}")
                        sell_position(ticker, sell_side, sell_qty, sell_price)
                        market_state["traded"] = False
                except Exception as e:
                    print(f"  🤖 Sell error: {e}")

            else:
                print(f"  🤖 HOLD — {mins_rem:.1f}min left")
                write_bot_status("building", mins_remaining=mins_rem)

            time.sleep(30)   # Claude decides every 30 seconds

        except KeyboardInterrupt:
            print("\n🤖 ClaudeBot: shutting down.")
            write_bot_status("killed")
            return
        except BaseException as e:
            import traceback
            print(f"\n⚠ ClaudeBot loop error ({type(e).__name__}): {e}")
            traceback.print_exc()
            print("  Recovering in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    while True:
        try:
            run_claude_bot()
            print("🤖 ClaudeBot stopped.")
            break
        except KeyboardInterrupt:
            print("\n🤖 Shutting down.")
            break
        except BaseException as e:
            print(f"\n❌ ClaudeBot crashed: {e}")
            import json
            cfg = json.load(open(BOT_DIR / "bot_config.json")) if (BOT_DIR / "bot_config.json").exists() else {}
            if not cfg.get("claude_mode", False):
                break
            print("Restarting in 15s...")
            time.sleep(15)
