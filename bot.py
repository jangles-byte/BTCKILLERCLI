"""
BTC.KILLER — Kalshi BTC 15M Trading Bot
Timing-aware conviction engine with price threshold gating.
"""

import os
import time
import json
import base64
import requests
import csv
import threading
import math
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
from signals import start_feed_thread, get_signal

load_dotenv()

API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
BASE_URL         = "https://api.elections.kalshi.com/trade-api/v2"
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", 50))
MAX_CONTRACTS    = int(os.getenv("MAX_CONTRACTS_PER_TRADE", 200))

with open(PRIVATE_KEY_PATH, "rb") as f:
    PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)

session = {
    "trades_today":    0,
    "pnl_today":       0.0,
    "market_wagered":  0.0,
    "killed":          False,
    "last_market_ticker": None,
    "_last_eval_ticker":  None,
}

MODE_THRESHOLDS = {
    "selective":  0.65,
    "balanced":   0.50,
    "aggressive": 0.35,
    "always":     0.00,
}

# ── Time-based price thresholds ───────────────────────────────────────────
# Tighter thresholds early (don't buy expensive with lots of time left)
# Format: (min_minutes_remaining, max_price_cents/100)
PRICE_THRESHOLDS = [
    (10.0, 0.30),   # 10+ min left:  only buy if price < 30¢
    (7.0,  0.45),   # 7-10 min left: only buy if price < 45¢
    (5.0,  0.60),   # 5-7 min left:  only buy if price < 60¢
    (3.0,  0.75),   # 3-5 min left:  only buy if price < 75¢
    (1.0,  0.88),   # 1-3 min left:  only buy if price < 88¢
    (0.0,  0.95),   # <1 min left:   basically anything worth it
]

def get_price_threshold(mins_remaining):
    """Return max acceptable buy price given time remaining."""
    for min_mins, max_price in PRICE_THRESHOLDS:
        if mins_remaining >= min_mins:
            return max_price
    return 0.95


# ── Auth ──────────────────────────────────────────────────────────────────

def load_config():
    p = Path(__file__).resolve().parent / "bot_config.json"
    return json.load(open(p)) if p.exists() else {}

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

# ── Kalshi API ────────────────────────────────────────────────────────────

def get_balance():
    """Fetch current Kalshi balance in dollars."""
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
    markets  = r.json().get("markets", [])
    now      = datetime.now(timezone.utc)
    best, best_diff = None, float("inf")
    for m in markets:
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        diff  = (close - now).total_seconds()
        if 0 < diff < best_diff:
            best_diff, best = diff, m
    return best, best_diff

def get_position_for_ticker(ticker):
    """
    Fetch Kalshi portfolio position for a specific ticker.
    Returns the position dict or None.
    """
    try:
        path = "/trade-api/v2/portfolio/positions"
        r = requests.get(
            BASE_URL + "/portfolio/positions",
            headers=sign_request("GET", path),
            params={"ticker": ticker},
            timeout=5,
        )
        positions = r.json().get("market_positions", [])
        for pos in positions:
            if pos.get("ticker") == ticker:
                return pos
    except Exception as e:
        print(f"  Position lookup error: {e}")
    return None

def get_recent_fills(ticker):
    """Fetch recent fills for a ticker to verify order filled."""
    try:
        path = "/trade-api/v2/portfolio/fills"
        r = requests.get(
            BASE_URL + "/portfolio/fills",
            headers=sign_request("GET", path),
            params={"ticker": ticker, "limit": 10},
            timeout=5,
        )
        return r.json().get("fills", [])
    except Exception:
        return []

def sell_position(ticker, side, num_contracts, price_dollars):
    """Sell (close) an existing position."""
    path = "/trade-api/v2/portfolio/orders"
    price_str = f"{float(price_dollars):.4f}"
    key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    body = json.dumps({
        "ticker": ticker, "action": "sell", "side": side,
        "type": "limit", "count": num_contracts, key: price_str,
    })
    r = requests.post(BASE_URL + "/portfolio/orders",
                     headers=sign_request("POST", path), data=body, timeout=10)
    r.raise_for_status()
    return r.json()

def place_order(ticker, side, price_dollars, num_contracts):
    """Place a limit order slightly above ask to guarantee fill."""
    path = "/trade-api/v2/portfolio/orders"
    price = float(price_dollars)
    price = min(price + 0.04, 0.99)
    price_str = f"{price:.4f}"
    key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    body = json.dumps({
        "ticker": ticker, "action": "buy", "side": side,
        "type": "limit", "count": num_contracts, key: price_str,
    })
    r = requests.post(
        BASE_URL + "/portfolio/orders",
        headers=sign_request("POST", path), data=body, timeout=10,
    )
    r.raise_for_status()
    return r.json()

def get_market_prices(ticker):
    """Fetch current YES/NO ask prices for a live market."""
    path = "/trade-api/v2/markets/" + ticker
    r = requests.get(BASE_URL + "/markets/" + ticker,
                     headers=sign_request("GET", path), timeout=5)
    m = r.json().get("market", r.json())
    return float(m.get("yes_ask_dollars", 0.5)), float(m.get("no_ask_dollars", 0.5))

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

# ── Trade logging ─────────────────────────────────────────────────────────

def get_trades_file():
    folder = Path(__file__).resolve().parent / "trades"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"trades_{datetime.now().strftime('%Y-%m-%d')}.csv"

def log_trade(ticker, side, price, contracts, note=""):
    f   = get_trades_file()
    new = not f.exists()
    with open(f, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp", "ticker", "side", "price", "contracts", "pnl"])
        w.writerow([datetime.now().isoformat(), ticker, side,
                    price, contracts, "pending"])

def update_trade_pnl(ticker, pnl):
    folder = Path(__file__).resolve().parent / "trades"
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

# ── Bot status file (read by dashboard) ──────────────────────────────────

def write_bot_status(status, direction=None, max_price=None, conviction=None,
                     mins_remaining=None):
    """Write bot's current internal status so dashboard can display it."""
    status_file = Path(__file__).resolve().parent / "bot_status.json"
    try:
        with open(status_file, "w") as f:
            json.dump({
                "status":       status,        # "watching", "building", "idle", "traded"
                "direction":    direction,
                "max_price":    max_price,
                "conviction":   conviction,
                "mins_remaining": mins_remaining,
                "updated_at":   time.time(),
            }, f)
    except Exception:
        pass

# ── Conviction scoring ────────────────────────────────────────────────────

def calc_conviction(sig, yes_ask, no_ask):
    """
    Returns (conviction 0-1, direction, components dict)
    """
    pos_safety       = sig["pos_safety"]
    safe_side        = sig["safe_side"]
    signal_agreement = sig["signal_agreement"]
    signal_direction = sig["signal_direction"]
    time_factor      = sig["time_factor"]
    our_yes          = sig["our_yes_prob"]
    our_no           = sig["our_no_prob"]

    # Direction
    if pos_safety > 1.5 and safe_side:
        direction  = safe_side
        contrarian = bool(signal_direction and signal_direction != safe_side)
    elif signal_direction:
        direction  = signal_direction
        contrarian = False
    else:
        direction  = "yes" if (our_yes - yes_ask) > (our_no - no_ask) else "no"
        contrarian = False

    # Scoring
    safety_score = min(pos_safety / 2.0, 1.0)
    sig_score    = signal_agreement if direction == signal_direction \
                   else signal_agreement * 0.3
    t_score      = time_factor
    crowd_lean   = yes_ask if direction == "yes" else no_ask

    if crowd_lean > 0.6:
        price_conf = 0.5 + (crowd_lean - 0.5) * 0.5
    elif crowd_lean < 0.3:
        price_conf = safety_score * sig_score
    else:
        price_conf = 0.5

    conviction = (
        safety_score * 0.40 +
        sig_score    * 0.30 +
        t_score      * 0.20 +
        price_conf   * 0.10
    )

    if contrarian and pos_safety > 1.0:
        conviction *= 0.5

    conviction = min(conviction, 1.0)

    return conviction, direction, {
        "safety_score": safety_score,
        "sig_score":    sig_score,
        "t_score":      t_score,
        "price_conf":   price_conf,
        "contrarian":   contrarian,
        "pos_safety":   pos_safety,
        "safe_side":    safe_side,
    }

def required_readings(conviction):
    if conviction >= 0.85:   return 5
    elif conviction >= 0.70: return 10
    elif conviction >= 0.55: return 15
    elif conviction >= 0.40: return 25
    else:                    return 999

def calc_bet_size(conviction, risk_per_trade, min_bet):
    if conviction >= 0.75:   factor = 1.0
    elif conviction >= 0.55: factor = 0.5
    elif conviction >= 0.40: factor = 0.25
    else:                    factor = 0.1
    return max(risk_per_trade * factor, min_bet)

# ── Main loop ─────────────────────────────────────────────────────────────

def run_bot():
    print("=" * 60)
    print("BTC.KILLER — Conviction engine v3 (timing-aware)")
    print("Price thresholds: 30/45/60/75/88/95¢ by time window")
    print("=" * 60)

    print("\nStarting feeds...")
    start_feed_thread()
    print("Waiting 40 seconds for data to load...")
    time.sleep(40)

    open_trades    = []
    last_market    = None
    last_mkt_time  = 0
    secs_remaining = 0
    streak = {"direction": None, "count": 0, "conviction": 0.0}

    market_state = {
        "ticker":         None,
        "traded":         False,
        "our_side":       None,
        "our_contracts":  0,
        "our_entry":      0.0,
        "topup_done":     False,
        "flip_done":      False,
    }

    # Watching state — tracks when we have conviction but price is too high
    # Bot watches every 2s for price to drop into acceptable range before firing
    market_watching = {
        "active":     False,
        "direction":  None,
        "max_price":  0.0,
        "since":      0,
        "ticker":     None,
    }

    while True:
        if session["killed"]:
            print("KILL SWITCH. Stopping.")
            write_bot_status("killed")
            break

        try:
            cfg = load_config()
            mode             = cfg.get("mode", "balanced")
            daily_loss_lim    = float(cfg.get("daily_loss_limit", DAILY_LOSS_LIMIT))
            hard_stop_enabled = bool(cfg.get("hard_stop_enabled", False))
            hard_stop_balance = float(cfg.get("hard_stop_balance", 0.0))
            wager_mode        = cfg.get("wager_mode", "dollar")
            wager_pct         = float(cfg.get("wager_pct", 10.0))
            kelly_enabled     = bool(cfg.get("kelly_enabled", False))
            kelly_fraction    = float(cfg.get("kelly_fraction", 0.5))
            current_bal       = None
            if wager_mode == "percent" or kelly_enabled or hard_stop_enabled:
                try:
                    current_bal = get_balance()
                except Exception:
                    pass
            if wager_mode == "percent" and current_bal:
                max_market_wager = current_bal * wager_pct / 100
            else:
                max_market_wager  = float(cfg.get("max_session_wager", 5.0))
            min_bet           = float(cfg.get("min_bet", 0.25))
            min_bet           = max(min_bet, 0.01)
            min_threshold     = MODE_THRESHOLDS.get(mode, 0.50)
            trigger_method    = cfg.get("trigger_method", "ev")
            # Always-buy time window: fire between always_open and always_close minutes left
            always_open       = float(cfg.get("always_open",  6.0))
            always_close      = float(cfg.get("always_close", 3.0))
            always_max_price  = float(cfg.get("always_max_price", 0.75))

            # ── Settle open trades ───────────────────────────────────────
            still_open = []
            for t in open_trades:
                pnl = get_settled_pnl(t["ticker"], t["side"],
                                      t["contracts"], t["entry_price"])
                if pnl is not None:
                    session["pnl_today"] += pnl
                    update_trade_pnl(t["ticker"], pnl)
                    print(f"  SETTLED: {t['ticker']} → "
                          f"{'WIN' if pnl>0 else 'LOSS'} ${pnl:+.2f} | "
                          f"Day P&L: ${session['pnl_today']:+.2f}")
                    pos_file = Path(__file__).resolve().parent / "current_position.json"
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

            if hard_stop_enabled:
                if current_bal is not None and current_bal <= hard_stop_balance:
                    session["killed"] = True
                    print(f"HARD STOP triggered — balance ${current_bal:.2f} ≤ floor ${hard_stop_balance:.2f}")
                    write_bot_status("killed")
                    break
            else:
                if session["pnl_today"] <= -daily_loss_lim:
                    session["killed"] = True
                    print(f"DAILY LOSS LIMIT hit ${abs(session['pnl_today']):.2f} — stopping")
                    write_bot_status("killed")
                    break

            # ── Refresh market every 10s ─────────────────────────────────
            now = time.time()
            if not last_market or (now - last_mkt_time) > 10:
                try:
                    last_market, secs_remaining = find_current_market()
                    last_mkt_time = now
                except Exception as mkt_err:
                    print(f"  Market refresh failed: {mkt_err} — retrying in 5s")
                    time.sleep(5)
                    continue
            elif last_market:
                close = datetime.fromisoformat(
                    last_market["close_time"].replace("Z", "+00:00"))
                secs_remaining = (close - datetime.now(timezone.utc)).total_seconds()

            if not last_market or secs_remaining <= 0:
                print("No active market — waiting 15s")
                streak = {"direction": None, "count": 0, "conviction": 0.0}
                write_bot_status("idle")
                time.sleep(15)
                continue

            ticker   = last_market["ticker"]
            mins_rem = secs_remaining / 60
            yes_ask  = float(last_market.get("yes_ask_dollars", 0.5))
            no_ask   = float(last_market.get("no_ask_dollars", 0.5))
            strike   = last_market.get("floor_strike") or last_market.get("cap_strike")
            s_type   = last_market.get("strike_type", "")

            # ── One trade per market ─────────────────────────────────────
            if ticker == session["last_market_ticker"]:
                write_bot_status("traded", mins_remaining=mins_rem)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] {ticker} | "
                      f"{mins_rem:.1f}min left | position held — waiting for market to close")
                time.sleep(2)
                continue

            # ── New market reset ─────────────────────────────────────────
            if ticker != session["_last_eval_ticker"]:
                streak = {"direction": None, "count": 0, "conviction": 0.0}
                session["market_wagered"]    = 0.0
                session["_last_eval_ticker"] = ticker
                market_state = {
                    "ticker":        ticker,
                    "traded":        False,
                    "our_side":      None,
                    "our_contracts": 0,
                    "our_entry":     0.0,
                    "topup_done":    False,
                    "flip_done":     False,
                }
                market_watching = {
                    "active":    False,
                    "direction": None,
                    "max_price": 0.0,
                    "since":     0,
                    "ticker":    ticker,
                }
                print(f"\n  ── New market: {ticker} | budget + watching state reset ──")

            # ── SMART MODE: Hard cutoff — no trades before 7.5 min mark ──
            # In smart mode, let signals build in the first half of the market.
            # Only act in the second half (< 7.5 minutes remaining).
            if mode != "always" and mins_rem > 7.5:
                print(f"  SMART HOLD — {mins_rem:.1f}min left, signals building (cutoff: 7.5min)")
                write_bot_status("building", mins_remaining=mins_rem)
                time.sleep(2)
                continue

            # ── Compute signals ──────────────────────────────────────────
            sig = get_signal(strike_price=strike, strike_type=s_type,
                           mins_remaining=mins_rem,
                           yes_ask=yes_ask, no_ask=no_ask)
            conviction, direction, components = calc_conviction(sig, yes_ask, no_ask)

            ask = yes_ask if direction == "yes" else no_ask

            # ── Budget check ─────────────────────────────────────────────
            remaining_budget = max_market_wager - session["market_wagered"]

            # ── Streak tracking ──────────────────────────────────────────
            if direction == streak["direction"]:
                streak["count"] += 1
                streak["conviction"] = max(streak["conviction"] * 0.6 + conviction * 0.4,
                                          conviction * 0.8)
            else:
                streak = {"direction": direction, "count": 1, "conviction": conviction}

            needed         = required_readings(conviction)
            avg_conviction = conviction
            already_traded = market_state["traded"]

            # ── Time-based price threshold for this window ───────────────
            max_price = get_price_threshold(mins_rem)

            print(f"[{datetime.now().strftime('%H:%M:%S')}] {ticker} | "
                  f"{mins_rem:.1f}min ({secs_remaining:.0f}s) | "
                  f"{direction.upper()} conv={conviction:.2f} "
                  f"streak={streak['count']}/{needed} | "
                  f"ask={ask:.2f} threshold=<{max_price:.2f} "
                  f"safety={components['pos_safety']:.2f} "
                  f"budget=${remaining_budget:.2f} "
                  f"{'[TRADED]' if already_traded else ''}"
                  f"{'[WATCHING]' if market_watching['active'] else ''}")

            # ── Last 60s top-up ──────────────────────────────────────────
            if (already_traded
                    and secs_remaining < 60
                    and not market_state["topup_done"]
                    and remaining_budget >= min_bet
                    and components["pos_safety"] >= 2.0
                    and components["safe_side"] == market_state["our_side"]):

                try:
                    live_yes, live_no = get_market_prices(ticker)
                    topup_ask = live_yes if market_state["our_side"] == "yes" else live_no
                    if topup_ask > 0 and topup_ask < 0.95:
                        topup_contracts = int(remaining_budget / topup_ask)
                        topup_contracts = min(topup_contracts, MAX_CONTRACTS)
                        if topup_contracts >= 1:
                            topup_cost = topup_contracts * topup_ask
                            print(f"\n  >>> TOP-UP: BUY {market_state['our_side'].upper()} "
                                  f"| {topup_contracts} @ ${topup_ask:.3f} "
                                  f"| cost ${topup_cost:.2f} "
                                  f"| {secs_remaining:.0f}s left")
                            result = place_order(ticker, market_state["our_side"],
                                               topup_ask, topup_contracts)
                            log_trade(ticker, market_state["our_side"],
                                     topup_ask, topup_contracts, "topup")
                            session["market_wagered"]   += topup_cost
                            session["trades_today"]     += 1
                            market_state["topup_done"]   = True
                except Exception as e:
                    print(f"  Top-up error: {e}")

            # ── Loss mitigation flip ─────────────────────────────────────
            elif (already_traded
                    and not market_state["flip_done"]
                    and secs_remaining > 45
                    and remaining_budget >= min_bet
                    and components["pos_safety"] >= 2.0):

                our_side  = market_state["our_side"]
                flip_side = "no" if our_side == "yes" else "yes"
                safe_side = components["safe_side"]

                if safe_side == flip_side:
                    try:
                        live_yes, live_no = get_market_prices(ticker)
                        current_val = live_yes if our_side == "yes" else live_no
                        loss_pct    = current_val / market_state["our_entry"] \
                                      if market_state["our_entry"] > 0 else 1.0

                        if loss_pct < 0.20:
                            flip_ask = live_no if flip_side == "no" else live_yes
                            if flip_ask > 0:
                                flip_contracts = int(remaining_budget / flip_ask)
                                flip_contracts = min(flip_contracts, MAX_CONTRACTS)
                                if flip_contracts >= 1:
                                    flip_cost = flip_contracts * flip_ask
                                    print(f"\n  >>> FLIP: position worth {loss_pct:.0%} of entry. "
                                          f"BUY {flip_side.upper()} | "
                                          f"{flip_contracts} @ ${flip_ask:.3f}")
                                    result = place_order(ticker, flip_side,
                                                       flip_ask, flip_contracts)
                                    log_trade(ticker, flip_side,
                                             flip_ask, flip_contracts, "flip")
                                    session["market_wagered"]  += flip_cost
                                    session["trades_today"]    += 1
                                    market_state["flip_done"]   = True
                    except Exception as e:
                        print(f"  Flip error: {e}")

            # ── Normal trade logic ────────────────────────────────────────
            if already_traded:
                write_bot_status("traded", direction=market_state["our_side"],
                                mins_remaining=mins_rem)
                time.sleep(2)
                continue

            if remaining_budget < min_bet:
                print(f"  MARKET BUDGET exhausted")
                time.sleep(2)
                continue

            # ── Mode-based conviction check ──────────────────────────────
            if mode == "always":
                in_window = always_close <= mins_rem <= always_open
                if not in_window:
                    if mins_rem > always_open:
                        status_msg = f"  ALWAYS — waiting for window ({always_open:.1f}min) | now {mins_rem:.1f}min"
                    else:
                        status_msg = f"  ALWAYS — window closed ({always_close:.1f}min passed) | {mins_rem:.1f}min left"
                    write_bot_status("building", direction=direction,
                                    conviction=conviction, mins_remaining=mins_rem)
                    print(status_msg)
                    time.sleep(2)
                    continue
                # In window — pick direction by trigger_method
                if trigger_method == "signal":
                    # direction already set by calc_conviction above — use as-is
                    pass
                else:
                    # EV: pick whichever side has better expected value
                    yes_ev = sig["our_yes_prob"] - yes_ask
                    no_ev  = sig["our_no_prob"]  - no_ask
                    direction = "yes" if yes_ev > no_ev else "no"
                ask = yes_ask if direction == "yes" else no_ask
                # Enforce max price for always-buy
                if ask > always_max_price:
                    write_bot_status("watching", direction=direction,
                                    max_price=always_max_price, mins_remaining=mins_rem)
                    print(f"  ALWAYS — in window but price {ask:.0%} > max {always_max_price:.0%}, waiting...")
                    time.sleep(2)
                    continue
            else:
                if avg_conviction < min_threshold:
                    write_bot_status("building", direction=direction,
                                    conviction=conviction, mins_remaining=mins_rem)
                    print(f"  HOLD — conviction {avg_conviction:.2f} < threshold {min_threshold:.2f}")
                    time.sleep(2)
                    continue
                if streak["count"] < needed:
                    write_bot_status("building", direction=direction,
                                    conviction=conviction, mins_remaining=mins_rem)
                    print(f"  BUILDING — {streak['count']}/{needed} (conv {avg_conviction:.2f})")
                    time.sleep(2)
                    continue

            # ── Price threshold gate (timing-based) ──────────────────────
            # We have conviction. Now check if price is acceptable for this time window.
            ask = yes_ask if direction == "yes" else no_ask  # re-fetch after direction finalized

            if ask <= 0:
                time.sleep(2)
                continue

            if ask > 0.95:
                print(f"  SKIP — ask {ask:.2f} too expensive (<5¢ profit per contract)")
                time.sleep(2)
                continue

            if ask < 0.03:
                print(f"  SKIP — ask {ask:.2f} too cheap (near-certain loss)")
                time.sleep(2)
                continue

            # Time-window price check
            if ask > max_price:
                # Price is too high for this time window — start/continue watching
                if not market_watching["active"] or market_watching["direction"] != direction:
                    # New watching session
                    market_watching = {
                        "active":    True,
                        "direction": direction,
                        "max_price": max_price,
                        "since":     time.time(),
                        "ticker":    ticker,
                    }
                    print(f"\n  ⏳ WATCHING {direction.upper()} | "
                          f"price {ask:.0%} > target <{max_price:.0%} for {mins_rem:.1f}min | "
                          f"waiting for price to drop...")
                else:
                    watching_secs = time.time() - market_watching["since"]
                    print(f"  ⏳ WATCHING {direction.upper()} | "
                          f"{ask:.0%} > <{max_price:.0%} | "
                          f"waiting {watching_secs:.0f}s...")

                write_bot_status("watching", direction=direction, max_price=max_price,
                                conviction=avg_conviction, mins_remaining=mins_rem)
                time.sleep(2)
                continue

            # Price is now in range! If we were watching, confirm we still want this direction
            if market_watching["active"] and market_watching["direction"] == direction:
                print(f"  ✅ PRICE IN RANGE: {ask:.0%} ≤ {max_price:.0%} — conviction still {avg_conviction:.2f}")
                market_watching["active"] = False  # Reset watching

            # Clear watching state if we switched direction
            if market_watching["active"] and market_watching["direction"] != direction:
                market_watching["active"] = False

            # ── Bet sizing ───────────────────────────────────────────────
            if kelly_enabled and current_bal and current_bal > 0:
                # Kelly criterion: f* = (p - ask) / (1 - ask), capped at max wager
                p      = sig["our_yes_prob"] if direction == "yes" else sig["our_no_prob"]
                f_star = (p - ask) / (1 - ask) if ask < 1.0 else 0.0
                if f_star <= 0:
                    print(f"  KELLY: no edge (f*={f_star:.3f}, p={p:.2f} ask={ask:.2f}) — skip")
                    time.sleep(2)
                    continue
                kelly_wager = f_star * kelly_fraction * current_bal
                wager = min(kelly_wager, max_market_wager, remaining_budget)
                print(f"  KELLY: p={p:.2f} ask={ask:.2f} f*={f_star:.3f} frac={kelly_fraction} "
                      f"→ ${kelly_wager:.2f} → capped ${wager:.2f}")
            else:
                if avg_conviction >= 0.75:   factor = 1.0
                elif avg_conviction >= 0.55: factor = 0.75
                elif avg_conviction >= 0.40: factor = 0.5
                else:                        factor = 0.25
                wager = min(max_market_wager * factor, remaining_budget)
            contracts = int(wager / ask)
            contracts = min(contracts, MAX_CONTRACTS)

            if contracts < 1:
                print(f"  SKIP — not enough for 1 contract at ${ask:.3f}")
                time.sleep(2)
                continue

            actual_cost   = contracts * ask
            potential_win = contracts * (1.0 - ask)

            # Enforce minimum bet — skip if sized trade is below threshold
            if actual_cost < min_bet:
                print(f"  SKIP — bet ${actual_cost:.2f} below min_bet ${min_bet:.2f} "
                      f"(low conviction factor)")
                time.sleep(2)
                continue

            print(f"\n  >>> FIRE: BUY {direction.upper()} | {contracts} @ ${ask:.3f} | "
                  f"conv={avg_conviction:.2f} streak={streak['count']} | "
                  f"cost=${actual_cost:.2f} win=${potential_win:.2f} | "
                  f"{'[CONTRARIAN]' if components['contrarian'] else ''}")

            # Lock market before placing order
            session["last_market_ticker"] = ticker
            market_state["traded"] = True

            result = place_order(ticker, direction, ask, contracts)

            if "error" in result:
                print(f"  ORDER ERROR: {result['error'].get('details', result['error'])}")
                session["last_market_ticker"] = None
                market_state["traded"] = False
                time.sleep(2)
                continue

            # ── Fill verification via Kalshi positions endpoint ──────────
            time.sleep(2)
            actual_contracts = contracts
            fill_cost        = actual_cost
            confirmed        = False

            def _check_position():
                """Return (confirmed, actual_contracts, fill_cost)."""
                kp = get_position_for_ticker(ticker)
                if kp and abs(kp.get("position", 0)) > 0:
                    sz = abs(kp["position"])
                    fc = kp.get("total_cost", actual_cost * 100) / 100
                    return True, sz, fc
                fills = get_recent_fills(ticker)
                recent = [f for f in fills if f.get("action") == "buy"]
                if recent:
                    sz = sum(f.get("count", 0) for f in recent) or contracts
                    return True, sz, actual_cost
                return False, contracts, actual_cost

            confirmed, actual_contracts, fill_cost = _check_position()
            if confirmed:
                print(f"  ✅ CONFIRMED: {actual_contracts} contracts, cost ${fill_cost:.2f}")
            else:
                print(f"  ⏳ Position not found — waiting 5s and rechecking...")
                time.sleep(5)
                confirmed, actual_contracts, fill_cost = _check_position()
                if confirmed:
                    print(f"  ✅ CONFIRMED (retry): {actual_contracts} contracts, cost ${fill_cost:.2f}")

            if not confirmed:
                # Order didn't fill — unlock this market so we can try again after re-checking signals
                print(f"  ⚠️  Order did not fill — resetting market lock, waiting 30s to re-evaluate signals...")
                market_state["traded"] = False
                session["last_market_ticker"] = None
                time.sleep(30)
                continue

            open_trades.append({
                "ticker": ticker, "side": direction,
                "contracts": actual_contracts, "entry_price": ask,
            })
            log_trade(ticker, direction, ask, actual_contracts)
            session["trades_today"]   += 1
            session["market_wagered"] += fill_cost

            market_state["our_side"]      = direction
            market_state["our_contracts"] = actual_contracts
            market_state["our_entry"]     = ask

            # Write position file for dashboard
            pos_file = Path(__file__).resolve().parent / "current_position.json"
            with open(pos_file, "w") as pf:
                json.dump({
                    "ticker":        ticker,
                    "side":          direction,
                    "contracts":     actual_contracts,
                    "entry":         ask,
                    "mins_remaining": mins_rem,
                    "cost":          fill_cost,
                }, pf)

            write_bot_status("traded", direction=direction, mins_remaining=mins_rem)
            streak = {"direction": None, "count": 0, "conviction": 0.0}

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)

        time.sleep(2)


if __name__ == "__main__":
    run_bot()
