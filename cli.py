#!/usr/bin/env python3
"""
BTC.KILLER CLI — Full terminal dashboard
Cosmetic only — bot logic unchanged.
"""
from __future__ import annotations
import csv, io, json, math, os, random, subprocess, sys, threading, time
from collections import deque
from datetime import datetime, date
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────────
BOT_DIR    = Path(__file__).resolve().parent
CONFIG     = BOT_DIR / "bot_config.json"
POS_FILE   = BOT_DIR / "current_position.json"
TRADES_DIR = BOT_DIR / "trades"

# ── Banner ───────────────────────────────────────────────────────────────────
_BANNER_LINES = [
    r"__/\\\\\\\\\\\\\____/\\\\\\\\\\\\\\\________/\\\\\\\\\____________/\\\________/\\\________/\\\\\\_____/\\\\\\__________________________________________________/\\\\\\\\\__/\\\______________/\\\\\\\\\\\_",
    r" _\/\\\/////////\\\_\///////\\\/////______/\\\////////____________\/\\\_____/\\\//________\////\\\____\////\\\_______________________________________________/\\\////////__\/\\\_____________\/////\\\///__",
    r"  _\/\\\_______\/\\\_______\/\\\_________/\\\/_____________________\/\\\__/\\\//______/\\\____\/\\\_______\/\\\_____________________________________________/\\\/___________\/\\\_________________\/\\\_____",
    r"   _\/\\\\\\\\\\\\\\________\/\\\________/\\\_______________________\/\\\\\\//\\\_____\///_____\/\\\_______\/\\\________/\\\\\\\\___/\\/\\\\\\\_____________/\\\_____________\/\\\_________________\/\\\_____",
    r"    _\/\\\/////////\\\_______\/\\\_______\/\\\_______________________\/\\\//_\//\\\_____/\\\____\/\\\_______\/\\\______/\\\/////\\\_\/\\\/////\\\___________\/\\\_____________\/\\\_________________\/\\\_____",
    r"     _\/\\\_______\/\\\_______\/\\\_______\//\\\______________________\/\\\____\//\\\___\/\\\____\/\\\_______\/\\\_____/\\\\\\\\\\\__\/\\\___\///____________\//\\\____________\/\\\_________________\/\\\_____",
    r"      _\/\\\_______\/\\\_______\/\\\________\///\\\____________________\/\\\_____\//\\\__\/\\\____\/\\\_______\/\\\____\//\\///////___\/\\\____________________\///\\\__________\/\\\_________________\/\\\_____",
    r"       _\/\\\\\\\\\\\\\/________\/\\\__________\////\\\\\\\\\___________\/\\\______\//\\\_\/\\\__/\\\\\\\\\__/\\\\\\\\\__\//\\\\\\\\\\_\/\\\______________________\////\\\\\\\\\_\/\\\\\\\\\\\\\\\__/\\\\\\\\\\\_ ",
    r"        _\/////////////__________\///______________\/////////____________\///________\///__\///__\/////////__\/////////____\//////////__\///__________________________\/////////__\///////////////__\///////////__",
]

# ── Shared state ─────────────────────────────────────────────────────────────
bot_process: subprocess.Popen | None = None
bot_log_buffer: deque[str] = deque(maxlen=400)
state_lock = threading.Lock()
app_state: dict = {
    "btc_price": None, "balance": None, "bot_running": False,
    "market_ticker": None, "secs_remaining": None,
    "yes_ask": None, "no_ask": None,
    "target_price": None, "target_dir": None,
    "our_yes_prob": 0.5, "our_no_prob": 0.5,
    "conviction": 0.0, "confidence": 0.0,
    "signals": [], "position": None,
    "yes_ev": 0.0, "no_ev": 0.0,
    "price_history": [],
    "trend_1h": None, "trend_6h": None, "trend_24h": None,
    "weekly_range_pct": None, "volatility": None,
    "watching_active": False, "watching_dir": None, "watching_thresh": None,
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def read_cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    except Exception:
        return {}

def write_cfg(updates: dict) -> None:
    cfg = read_cfg()
    cfg.update(updates)
    CONFIG.write_text(json.dumps(cfg, indent=2))

def read_position() -> dict | None:
    try:
        if POS_FILE.exists():
            d = json.loads(POS_FILE.read_text())
            return d if d.get("ticker") else None
    except Exception:
        pass
    return None

def load_trades_today() -> list[dict]:
    path = TRADES_DIR / f"trades_{date.today().isoformat()}.csv"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []

def compute_stats(trades: list[dict]) -> dict:
    pnl = wins = losses = 0
    win_total = loss_total = 0.0
    for t in trades:
        raw = t.get("pnl", "pending")
        if raw == "pending":
            continue
        try:
            v = float(raw)
            pnl += v
            if v > 0:
                wins += 1; win_total += v
            else:
                losses += 1; loss_total += abs(v)
        except Exception:
            pass
    total    = wins + losses
    avg_win  = win_total  / wins    if wins    else 0.0
    avg_loss = loss_total / losses  if losses  else 0.0
    pf       = win_total  / loss_total if loss_total else 0.0
    wr       = wins / total if total else 0.0
    ev       = (wr * avg_win) - ((1 - wr) * avg_loss) if total else 0.0
    return dict(pnl=pnl, wins=wins, losses=losses, total=total,
                avg_win=avg_win, avg_loss=avg_loss,
                profit_factor=pf, win_rate=wr, ev=ev)

# ── Bot process ───────────────────────────────────────────────────────────────
_bot_notify_cb = None   # set by the App to surface crash toasts

def _stream(proc: subprocess.Popen) -> None:
    """Drain bot stdout into the log buffer; detect and report crashes."""
    try:
        for line in iter(proc.stdout.readline, b""):
            t = line.decode("utf-8", errors="replace").rstrip()
            if t:
                bot_log_buffer.append(t)
    except Exception:
        pass
    # Process ended — log exit code so the user can see why it stopped
    code = proc.poll()
    if code is None:
        # Still running somehow (shouldn't happen here), just return
        return
    with state_lock:
        app_state["bot_running"] = False
    if code == 0:
        bot_log_buffer.append("── bot exited cleanly (exit 0) ──")
    else:
        bot_log_buffer.append(f"❌ BOT CRASHED — exit code {code} — check log above for traceback")
        if _bot_notify_cb:
            try:
                _bot_notify_cb(f"Bot crashed (exit {code})")
            except Exception:
                pass

def start_bot() -> None:
    global bot_process
    if bot_process and bot_process.poll() is None:
        return
    bot_log_buffer.clear()
    env = os.environ.copy(); env["PYTHONUNBUFFERED"] = "1"
    bot_process = subprocess.Popen(
        [sys.executable, "-u", str(BOT_DIR / "bot.py")],
        cwd=str(BOT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    threading.Thread(target=_stream, args=(bot_process,), daemon=True).start()
    with state_lock:
        app_state["bot_running"] = True

def stop_bot() -> None:
    global bot_process
    if bot_process and bot_process.poll() is None:
        bot_process.terminate(); bot_process = None
    with state_lock:
        app_state["bot_running"] = False

# ── Claude CLI helper ────────────────────────────────────────────────────────
# Uses the local `claude` CLI (Claude Code) — no API key needed, uses your subscription.
_PROTECTED_SETTINGS = {"daily_loss_limit", "hard_stop_balance", "hard_stop_enabled"}

# Normalize common Claude key-name mistakes → actual bot_config.json keys
_CFG_KEY_ALIASES: dict[str, str] = {
    "always_open_window":  "always_open",
    "always_close_window": "always_close",
    "open_window":         "always_open",
    "close_window":        "always_close",
    "max_market_wager":    "max_session_wager",
    "max_wager":           "max_session_wager",
    "max_bet":             "max_session_wager",
    "wager_limit":         "max_session_wager",
    "kelly_frac":          "kelly_fraction",
    "kelly_f":             "kelly_fraction",
    "loss_limit":          "daily_loss_limit",
    "max_price":           "always_max_price",
    "price_limit":         "always_max_price",
    "price_cap":           "always_max_price",
    "trigger":             "trigger_method",
    "min_conviction":      "min_bet",
}

def _claude_ask(query: str, state: dict, cfg: dict, log_cb, apply_cb,
                firepower: bool = False, fire_cb=None,
                trades: list | None = None, recent_log: list | None = None) -> None:
    """Call `claude -p` subprocess with full bot context. Runs in a daemon thread."""

    sigs_txt = "\n".join(
        f"  {sg.get('name','?')}: {sg.get('yes_prob',0.5)*100:.0f}%  "
        f"str={sg.get('strength',0):.2f}  {sg.get('reason','')}"
        for sg in state.get("signals", [])
    ) or "  none yet"

    # Full config — only block the two hard-limit fields
    safe_cfg = {k: v for k, v in cfg.items() if k not in _PROTECTED_SETTINGS}

    # Trade history — last 50 trades today
    if trades:
        trades_txt = "\n".join(
            f"  {t.get('timestamp','?')[:19]}  {t.get('ticker','?')}  "
            f"{t.get('side','?').upper():3}  qty={t.get('contracts','?')}  "
            f"entry={t.get('price','?')}  pnl={t.get('pnl','pending')}"
            for t in trades[-50:]
        )
    else:
        trades_txt = "  no trades today yet"

    # Recent bot log — last 40 lines for context
    log_txt = "\n".join(f"  {l}" for l in (recent_log or [])[-40:]) or "  (empty)"

    # Position
    pos = state.get("position") or {}
    if pos:
        pos_txt = (f"  {pos.get('side','?').upper()} {pos.get('contracts','?')} contracts  "
                   f"entry={pos.get('entry','?')}  cost=${pos.get('cost','?')}")
    else:
        pos_txt = "  none"

    prompt = f"""You are an AI trading assistant embedded directly inside a live Kalshi BTC prediction market bot. You are NOT a chatbot — you are a co-pilot with full read/write access to the bot's settings and state. You monitor performance, flag issues, recommend adjustments, and execute changes when asked.

=== LIVE MARKET ===
BTC price      : ${state.get('btc_price') or 0:,.2f}
Strike         : ${state.get('target_price') or 0:,.2f}  ({state.get('target_dir','')})
Time remaining : {int((state.get('secs_remaining') or 0)//60)}m {int((state.get('secs_remaining') or 0)%60)}s
YES ask        : {(state.get('yes_ask') or 0)*100:.0f}¢    NO ask: {(state.get('no_ask') or 0)*100:.0f}¢
YES EV         : {state.get('yes_ev', 0):.3f}    NO EV: {state.get('no_ev', 0):.3f}
Our YES prob   : {state.get('our_yes_prob',0.5)*100:.0f}%   NO prob: {state.get('our_no_prob',0.5)*100:.0f}%
Conviction     : {state.get('conviction',0):.2f}   Confidence: {state.get('confidence',0):.2f}
Bot running    : {state.get('bot_running', False)}
Balance        : ${state.get('balance') or 0:.2f}
Watching       : {state.get('watching_active', False)}  dir={state.get('watching_dir','—')}  thresh={state.get('watching_thresh','—')}

=== SIGNALS ===
{sigs_txt}

=== CURRENT POSITION ===
{pos_txt}

=== ALL BOT SETTINGS (you can change any of these) ===
{json.dumps(safe_cfg, indent=2)}

=== TODAY'S TRADE HISTORY ===
{trades_txt}

=== RECENT BOT LOG (last 40 lines) ===
{log_txt}

=== PROTECTED — NEVER MODIFY ===
daily_loss_limit and hard_stop_balance are hard safety limits. Do NOT suggest or apply changes to these two fields under any circumstances.

=== FIREPOWER ===
Firepower enabled: {firepower}
If firepower is ON and conditions strongly warrant a trade, you may include FIRE_BOT on its own line at the very end. Only do this if the user explicitly asks, or conditions are exceptionally clear AND firepower is enabled.

=== YOUR ROLE ===
You are an active trading co-pilot, not a chatbot. Be direct, specific, and data-driven.
- Proactively flag anything concerning you see in the state, log, or trade history
- Reference specific numbers from the data above, don't speak in generalities
- Keep responses tight: 3-6 sentences max unless the user asks for detail
- No markdown formatting (no **, no #, no bullet symbols)
- If changing a setting, include EXACTLY at the end: APPLY:{{"key": value, ...}}
- Only include APPLY if making a change; only include FIRE_BOT if firing a trade

=== USER ===
{query}"""

    log_cb("[dim #4a9eff]⟳ thinking…[/]")
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or "claude CLI returned non-zero"
            log_cb(f"[#ff3b5c]{err}[/]")
            return

        response = proc.stdout.strip()

        # Extract and strip FIRE_BOT directive
        fire_requested = False
        lines = response.splitlines()
        clean_lines = []
        for ln in lines:
            if ln.strip() == "FIRE_BOT":
                fire_requested = True
            else:
                clean_lines.append(ln)
        response = "\n".join(clean_lines).strip()

        # Split off any APPLY: line
        if "APPLY:" in response:
            parts    = response.rsplit("APPLY:", 1)
            display  = parts[0].strip()
            try:
                changes = json.loads(parts[1].strip())
                changes = {_CFG_KEY_ALIASES.get(k, k): v for k, v in changes.items()}
                safe    = {k: v for k, v in changes.items() if k not in _PROTECTED_SETTINGS}
                if safe:
                    apply_cb(safe)
                    keys = ", ".join(safe.keys())
                    log_cb(f"[bold #00ff88]◈[/] {display}")
                    log_cb(f"[bold #ffc837]⚙  applied → {keys}[/]")
                    if fire_requested and firepower and fire_cb:
                        fire_cb()
                    return
            except Exception:
                pass  # fall through to show raw response

        log_cb(f"[bold #00ff88]◈[/] {response}")

        # Handle FIRE_BOT after displaying response
        if fire_requested and firepower and fire_cb:
            fire_cb()
        elif fire_requested and not firepower:
            log_cb("[dim #ff3b5c]⚠  Claude wanted to fire but firepower is OFF.[/]")

    except FileNotFoundError:
        log_cb("[#ff3b5c]'claude' not found — is Claude Code installed and on PATH?[/]")
    except subprocess.TimeoutExpired:
        log_cb("[#ff3b5c]timed out waiting for Claude[/]")
    except Exception as e:
        log_cb(f"[#ff3b5c]error: {e}[/]")

# ── Background updater ────────────────────────────────────────────────────────
def background_updater() -> None:
    last_bal = last_mkt = last_sig = 0
    try:
        from bot import get_balance as _bal, find_current_market as _mkt
        from signals import start_feed_thread, btc_state, get_signal, get_candles_context
        start_feed_thread()
        _ok = True
    except Exception as e:
        print(f"[cli] import error: {e}"); _ok = False

    while True:
        now = time.time()
        if _ok:
            price   = btc_state.get("price")
            history = [p for _, p in btc_state.get("price_history", [])[-300:]]
            with state_lock:
                app_state["btc_price"]     = price
                app_state["price_history"] = history

        if _ok and now - last_bal > 30:
            try:
                with state_lock: app_state["balance"] = _bal()
                last_bal = now
            except Exception: pass

        if _ok and now - last_mkt > 3:
            try:
                market, secs = _mkt()
                if market:
                    strike = market.get("floor_strike") or market.get("cap_strike")
                    stype  = market.get("strike_type", "")
                    tdir   = ("above" if "greater" in stype else
                              "below" if "less"    in stype else stype)
                    with state_lock:
                        app_state.update(dict(
                            market_ticker  = market["ticker"],
                            secs_remaining = secs,
                            yes_ask = float(market.get("yes_ask_dollars", 0)),
                            no_ask  = float(market.get("no_ask_dollars",  0)),
                            target_price = strike,
                            target_dir   = tdir,
                        ))
                last_mkt = now
            except Exception: pass

        if _ok and now - last_sig > 3:
            try:
                with state_lock:
                    strike = app_state.get("target_price")
                    stype  = app_state.get("target_dir", "")
                    secs   = app_state.get("secs_remaining")
                    ya     = app_state.get("yes_ask", 0.5) or 0.5
                    na     = app_state.get("no_ask",  0.5) or 0.5
                if strike:
                    sig = get_signal(strike_price=strike, strike_type=stype,
                                     mins_remaining=secs/60 if secs else None,
                                     yes_ask=ya, no_ask=na)
                    from bot import calc_conviction
                    conv, _, _ = calc_conviction(sig, ya, na)
                    ctx = {}
                    try: ctx = get_candles_context()
                    except Exception: pass
                    with state_lock:
                        app_state.update(dict(
                            our_yes_prob = sig["our_yes_prob"],
                            our_no_prob  = sig["our_no_prob"],
                            conviction   = conv,
                            confidence   = sig.get("confidence", 0),
                            signals      = sig.get("signals", []),
                            yes_ev       = sig["our_yes_prob"] - ya,
                            no_ev        = sig["our_no_prob"]  - na,
                        ))
                        app_state.update(ctx)
                last_sig = now
            except Exception: pass

        # Watching state from log
        for line in list(bot_log_buffer)[-10:]:
            if "WATCHING" in line:
                parts = line.split()
                try:
                    wd = "YES" if "YES" in line else "NO"
                    # grab threshold from "threshold=<X>"
                    thresh = next((p.split("=")[1] for p in parts if "threshold" in p), None)
                    with state_lock:
                        app_state["watching_active"] = True
                        app_state["watching_dir"]    = wd
                        app_state["watching_thresh"] = thresh
                except Exception: pass
                break
        else:
            with state_lock:
                app_state["watching_active"] = False

        with state_lock:
            app_state["position"] = read_position()
            if app_state["bot_running"] and bot_process and bot_process.poll() is not None:
                app_state["bot_running"] = False

        time.sleep(1)

# ── Braille chart ─────────────────────────────────────────────────────────────
def _braille_chart(values: list[float], width: int, height: int,
                   target: float | None = None,
                   dist_label: str | None = None,
                   dist_top: bool = True) -> "Text":
    from rich.text import Text

    if not values or width < 4 or height < 2:
        return Text("  — waiting for price feed —", style="dim #1a2535")

    px_w = width * 2
    px_h = height * 4
    v_min = min(values); v_max = max(values)
    spread = v_max - v_min
    # tight padding: show 10% extra so data fills 80%+ of chart height
    pad   = max(spread * 0.12, spread * 0.5 if spread < 20 else 20)
    v_min -= pad; v_max += pad
    # Guard against zero-spread (flat price / only 1-2 samples) → avoid div/0
    if v_max == v_min:
        v_min -= 50; v_max += 50

    def to_px(v: float) -> int:
        r = int((v_max - v) / (v_max - v_min) * (px_h - 1))
        return max(0, min(px_h - 1, r))

    n       = len(values)
    sampled = [values[min(int(i * n / px_w), n - 1)] for i in range(px_w)]

    price_g  = [[False] * px_w for _ in range(px_h)]
    target_g = [[False] * px_w for _ in range(px_h)]

    # Price line with fill to bottom for area effect
    for i, v in enumerate(sampled):
        r = to_px(v)
        price_g[r][i] = True
        if i > 0:
            pr = to_px(sampled[i - 1])
            lo, hi = min(r, pr), max(r, pr)
            for row in range(lo, hi + 1):
                price_g[row][i] = True

    # Target line — dashed pixels if on-chart only
    target_on_chart  = target is not None and v_min < target < v_max
    target_above     = target is not None and target >= v_max   # anchor to top
    target_below     = target is not None and target <= v_min   # anchor to bottom

    if target_on_chart:
        tr = to_px(target)
        for c in range(px_w):
            if (c // 3) % 2 == 0:
                target_g[tr][c] = True
                if 0 < tr < px_h - 1:
                    target_g[tr - 1][c] = True

    DOT = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]]

    # Current price arrow position
    cur_row   = to_px(values[-1]) // 4 if values else 0
    cur_price = values[-1] if values else 0
    mid_row   = height // 2
    tgt_row   = to_px(target) // 4 if target_on_chart else -1

    result = Text()
    for cr in range(height):

        # ── Off-chart target band: bypass pixel grid, write directly ──────────
        # This guarantees the yellow bar is always visible regardless of what
        # the price line pixels are doing in the same character rows.
        if cr == 0 and target_above:
            result.append("▓" * width, style="bold #ffc837")
            result.append(f" ▲ strike ${target:,.0f}", style="bold #ffc837")
            result.append("\n")
            continue
        if cr == 1 and target_above:
            result.append("░" * width, style="#7a5500")
            result.append(f" {dist_label or ''}", style="bold #ffc837")
            result.append("\n")
            continue
        if cr == height - 1 and target_below:
            result.append("▓" * width, style="bold #ffc837")
            result.append(f" ▼ strike ${target:,.0f}", style="bold #ffc837")
            result.append("\n")
            continue
        if cr == height - 2 and target_below:
            result.append("░" * width, style="#7a5500")
            result.append(f" {dist_label or ''}", style="bold #ffc837")
            result.append("\n")
            continue

        # ── Normal pixel rendering ─────────────────────────────────────────────
        for cc in range(width):
            pb = tb = 0
            for dr in range(4):
                for dc in range(2):
                    pr = cr * 4 + dr; pc = cc * 2 + dc
                    if price_g[pr][pc]:  pb |= DOT[dr][dc]
                    if target_g[pr][pc]: tb |= DOT[dr][dc]
            combined = pb | tb
            ch = chr(0x2800 + combined)
            if   combined == 0:  result.append(ch, style="#0d1520")
            elif tb and not pb:  result.append(ch, style="#ffc837")
            elif pb and not tb:
                col_frac = (cc + 1) / width
                if col_frac > 0.85:    result.append(ch, style="bold #00ff88")
                elif col_frac > 0.5:   result.append(ch, style="#00cc66")
                else:                  result.append(ch, style="#007744")
            else:                result.append(ch, style="bold white")

        # Side labels
        if cr == 0:
            result.append(f" {v_max:,.0f}", style="dim #667788")
        elif cr == cur_row and cr != mid_row:
            result.append(f" ◀ ${cur_price:,.0f}", style="bold #00ff88")
        elif cr == tgt_row and cr != mid_row:
            result.append(f" ── target ${target:,.0f}", style="#ffc837")
        elif cr == mid_row and dist_label:
            arrow = "▲" if dist_top else "▼"
            result.append(f" {arrow} {dist_label} to strike", style="bold #ffc837")
        elif cr == cur_row:
            result.append(f" ◀ ${cur_price:,.0f}", style="bold #00ff88")
        elif cr == height - 1:
            result.append(f" {v_min:,.0f}", style="dim #667788")

        result.append("\n")

    return result

# ── stdout redirect ───────────────────────────────────────────────────────────
class _BufferWriter(io.TextIOBase):
    def write(self, text: str) -> int:
        s = text.rstrip("\n")
        if s: bot_log_buffer.append(s)
        return len(text)
    def flush(self): pass

# ── Textual ───────────────────────────────────────────────────────────────────
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Button, RichLog, Footer, Input
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual import on
from rich.console import RenderableType
from rich.text import Text
from rich.align import Align

class BrailleChart(Widget):
    prices:     reactive[list[float]]  = reactive(list,  layout=True)
    target:     reactive[float | None] = reactive(None,  layout=True)
    dist_label: reactive[str | None]   = reactive(None,  layout=True)
    dist_top:   reactive[bool]         = reactive(True,  layout=True)

    def render(self) -> RenderableType:
        w = self.size.width - 2
        h = self.size.height - 2
        if w < 2 or h < 2: return Text("")
        return _braille_chart(self.prices, w, h, self.target,
                              self.dist_label, self.dist_top)


class ASCIIBanner(Widget):
    """Banner with live price-wave animation through the underscore baselines."""
    _TICK  = 0.07
    _frame: int = 0

    def on_mount(self) -> None:
        self.set_interval(self._TICK, self._step)

    def _step(self) -> None:
        self._frame += 1
        self.refresh()

    def render(self) -> RenderableType:
        f      = self._frame
        result = Text(no_wrap=True, overflow="crop")

        for line in _BANNER_LINES:
            for c, ch in enumerate(line):
                if ch == '_':
                    # Two-frequency wave → realistic price-like ripple
                    wave = (math.sin(c * 0.11 + f * 0.22) * 0.6 +
                            math.sin(c * 0.25 + f * 0.13) * 0.4)
                    if   wave >  0.55: result.append(ch, style="bold #00ff88")
                    elif wave >  0.20: result.append(ch, style="#00cc55")
                    elif wave > -0.20: result.append(ch, style="#006633")
                    elif wave > -0.55: result.append(ch, style="#003311")
                    else:              result.append(ch, style="#001a08")
                elif ch == '/':
                    result.append(ch, style="#00bb55")
                elif ch == '\\':
                    result.append(ch, style="#009944")
                elif ch == ' ':
                    result.append(ch)
                else:
                    result.append(ch, style="#004422")
            result.append("\n")

        return Align.center(result, vertical="middle")


CSS = """
Screen { background: #060a12; layout: vertical; }

/* ── Banner ── */
#banner {
    height: 11; background: #060a12;
    border-bottom: solid #1a2535;
    content-align: center middle;
}
#main-row { height: 1fr; }

/* ─────────────────────────────────────────────
   LEFT — Settings panel
───────────────────────────────────────────── */
#settings {
    width: 30; background: #080c14;
    border-right: solid #1a2535; padding: 0 1;
    overflow-y: auto; scrollbar-size: 1 1;
}
.sec  { color: #ffc837; text-style: bold; height: 1; margin: 1 0 0 0; padding: 0; }
.lbl  { color: #445566; height: 1; margin: 0; padding: 0; }
.tr   { height: 3; margin: 0; }
.tog  {
    width: 1fr; height: 3; min-width: 0;
    background: #080c14; border: solid #1a2535; color: #334455;
}
.tog:hover { background: #101825; color: #778899; }
.ton  {
    width: 1fr; height: 3; min-width: 0;
    background: #001a10; border: solid #00883a; color: #00ff88; text-style: bold;
}
.ton:hover { background: #002818; }
Input {
    height: 3; background: #080c14; border: solid #1a2535;
    color: #cce8ff; margin: 0; padding: 0 1;
}
Input:focus { border: solid #ffc837; color: #fff; }
.prev     { color: #2a3a4a; height: 1; }
.sub-wrap { padding: 0; margin: 0; height: auto; }
#toggle-btn {
    width: 1fr; height: 3; margin: 0 1 1 0;
}
#toggle-btn.start-state {
    background: #001a10; color: #00ff88; border: solid #00883a;
}
#toggle-btn.start-state:hover { background: #003320; }
#toggle-btn.stop-state {
    background: #1a0008; color: #ff3b5c; border: solid #882030;
}
#toggle-btn.stop-state:hover { background: #2a0010; }
#setup-btn {
    width: 1fr; height: 3; background: #0a0f1a;
    color: #ffc837; border: solid #443300; margin: 2 0 1 0;
}
#setup-btn:hover { background: #1a1500; color: #ffe066; }
#bot-status { height: 2; color: #aaa; }
#log-header { height: 1; color: #ffc837; padding: 0 1; margin: 1 0 0 0; }

/* ─────────────────────────────────────────────
   CENTER — Chart + live info
───────────────────────────────────────────── */
#center { width: 1fr; background: #060a12; padding: 0 1; }

BrailleChart {
    height: 22; border: solid #1a2535;
    background: #060a12; margin: 0 0 1 0;
}
#claude-header {
    height: 1; color: #4a9eff; padding: 0 1; margin: 1 0 0 0;
}
#claude-log {
    height: 1fr; border: solid #1a2535;
    background: #080c14; scrollbar-size: 1 1; margin: 0 0 0 0;
}
#claude-input {
    height: 3; background: #080c14;
    border: solid #1a3355; color: #cce8ff; margin: 0;
}
#claude-input:focus { border: solid #4a9eff; }
#mkt-row {
    height: 4; border: solid #1a2535; background: #080c14;
    padding: 0 2; margin: 0 0 1 0;
    content-align: center middle; text-align: center;
}
#watch-banner {
    height: 2; color: #ffc837; background: #110e00;
    border: solid #553300; padding: 0 1; margin: 0 0 1 0;
    content-align: center middle; text-align: center;
}
#odds-row {
    height: 6; border: solid #1a2535;
    background: #080c14; padding: 0 2; margin: 0 0 1 0;
    content-align: center middle; text-align: center;
}
#pos-panel {
    height: 4; border: solid #ffc837;
    background: #0d0e05; padding: 0 2;
    content-align: center middle; text-align: center;
    margin: 0 0 1 0;
}

/* ─────────────────────────────────────────────
   RIGHT — Signals + Log + Trades
───────────────────────────────────────────── */
#right {
    width: 52; background: #060a12;
    border-left: solid #1a2535; padding: 0 1;
}
#macro-row {
    height: 2; border: solid #1a2535; background: #080c14;
    padding: 0 1; margin: 0 0 1 0;
}
#sig-panel {
    height: 13; border: solid #1a2535;
    background: #080c14; padding: 1 1; margin: 0 0 1 0;
}
#bot-log {
    height: 1fr; border: solid #1a2535;
    background: #080c14; scrollbar-size: 1 1; margin: 0 0 1 0;
}
#trade-stats {
    height: 5; border: solid #1a2535;
    background: #080c14; padding: 0 1; margin: 0 0 1 0;
}
#trade-list {
    height: 12; border: solid #1a2535;
    background: #080c14; padding: 0 1;
}

Footer { background: #080c14; color: #1a2535; }
"""


class BTCKillerApp(App):
    CSS = CSS
    BINDINGS = [
        ("s", "start_bot", "Start"),
        ("x", "stop_bot",  "Stop"),
        ("r", "setup",     "Setup"),
        ("q", "quit",      "Quit"),
    ]

    _log_n: int = 0
    _log_last: str = ""
    _monitor_tick: int = 0
    _MONITOR_EVERY: int = 900   # auto-monitor every 15 minutes (~1 per market window)

    # Settings state
    _top_mode:    str  = "smart"
    _aggr:        int  = 1
    _wager_mode:  str  = "dollar"
    _loss_mode:   str  = "daily_loss"
    _loss_period: str  = "daily"
    _kelly_on:    bool = False
    _always_entry:str  = "signal"
    _firepower:   bool = False   # must be explicitly enabled each session
    _tg_on:       bool = False

    def compose(self) -> ComposeResult:
        yield ASCIIBanner(id="banner")
        with Horizontal(id="main-row"):

            # ── LEFT: settings ──────────────────────────────────────────────
            with ScrollableContainer(id="settings"):
                yield Static("◈ BOT", classes="sec")
                with Horizontal(classes="tr"):
                    yield Button("▶  Start", id="toggle-btn", classes="start-state")
                yield Static("", id="bot-status")

                yield Static("◈ MODE", classes="sec")
                with Horizontal(classes="tr", id="mode-row"):
                    yield Button("Smart",  id="mode-smart",  classes="ton")
                    yield Button("Always", id="mode-always", classes="tog")
                with Vertical(id="smart-wrap", classes="sub-wrap"):
                    yield Static("Aggressiveness", classes="lbl")
                    with Horizontal(classes="tr", id="aggr-row"):
                        yield Button("Selective",  id="aggr-0", classes="tog")
                        yield Button("Balanced",   id="aggr-1", classes="ton")
                        yield Button("Aggressive", id="aggr-2", classes="tog")
                with Vertical(id="always-wrap", classes="sub-wrap"):
                    yield Static("Open at ≥ X mins left", classes="lbl")
                    yield Input(value="6.0", id="always-open")
                    yield Static("Close at ≤ X mins left", classes="lbl")
                    yield Input(value="3.0", id="always-close")
                    yield Static("Max price (¢)", classes="lbl")
                    yield Input(value="0.75",  id="always-price")

                yield Static("◈ WAGER", classes="sec")
                with Horizontal(classes="tr", id="wager-row"):
                    yield Button("$ Fixed",   id="wager-dollar", classes="ton")
                    yield Button("% Balance", id="wager-pct",    classes="tog")
                yield Static("Min bet", classes="lbl")
                yield Input(value="1.00", id="min-bet")
                yield Static("Max bet", classes="lbl")
                yield Input(value="5.00", id="max-bet")
                yield Static("", id="wager-preview", classes="prev")

                yield Static("◈ KELLY", classes="sec")
                with Horizontal(classes="tr", id="kelly-row"):
                    yield Button("ON",  id="kelly-on",  classes="tog")
                    yield Button("OFF", id="kelly-off", classes="ton")
                with Vertical(id="kelly-wrap", classes="sub-wrap"):
                    yield Static("Fraction (0.05 – 1.0)", classes="lbl")
                    yield Input(value="0.50", id="kelly-frac")

                yield Static("◈ RISK", classes="sec")
                with Horizontal(classes="tr", id="risk-row"):
                    yield Button("Daily Loss", id="risk-daily",    classes="ton")
                    yield Button("Hard Stop",  id="risk-hardstop", classes="tog")
                with Vertical(id="daily-wrap", classes="sub-wrap"):
                    yield Static("Period", classes="lbl")
                    with Horizontal(classes="tr", id="period-row"):
                        yield Button("Daily",  id="per-daily",  classes="ton")
                        yield Button("Hourly", id="per-hourly", classes="tog")
                        yield Button("Weekly", id="per-weekly", classes="tog")
                    yield Static("Loss limit ($)", classes="lbl")
                    yield Input(value="50", id="loss-limit")
                with Vertical(id="hardstop-wrap", classes="sub-wrap"):
                    yield Static("Floor balance ($)", classes="lbl")
                    yield Input(value="20", id="hard-stop-amt")

                yield Static("◈ TELEGRAM", classes="sec")
                with Horizontal(classes="tr", id="tg-row"):
                    yield Button("ON",  id="tg-on",  classes="tog")
                    yield Button("OFF", id="tg-off", classes="ton")
                with Vertical(id="tg-wrap", classes="sub-wrap"):
                    yield Static("Bot token", classes="lbl")
                    yield Input(placeholder="123456:ABC-...", id="tg-token", password=True)
                    yield Static("Allowed user IDs (comma-sep)", classes="lbl")
                    yield Input(placeholder="123456789,987654321", id="tg-users")

                yield Button("⚙  Setup / Reconfigure", id="setup-btn")

            # ── CENTER ──────────────────────────────────────────────────────
            with Vertical(id="center"):
                yield BrailleChart(id="chart")
                yield Static("", id="mkt-row")
                yield Static("", id="watch-banner")
                yield Static("", id="odds-row")
                yield Static("", id="pos-panel")
                yield Static(
                    "[bold #4a9eff]◈ CLAUDE[/]  [dim]trading co-pilot — monitors, advises, adjusts[/]",
                    id="claude-header", markup=True,
                )
                yield RichLog(id="claude-log", highlight=False, markup=True,
                              wrap=True, auto_scroll=True)
                yield Input(placeholder="ask claude...", id="claude-input")

            # ── RIGHT ────────────────────────────────────────────────────────
            with Vertical(id="right"):
                yield Static("", id="macro-row")
                yield Static("", id="sig-panel")
                yield Static("[bold #ffc837]◈ BOT LOG[/]  [dim]starts when bot is running[/]",
                             id="log-header", markup=True)
                yield RichLog(id="bot-log", highlight=False, markup=True,
                              wrap=False, auto_scroll=True)
                yield Static("", id="trade-stats")
                yield Static("", id="trade-list")

        yield Footer()

    # ── Mount ────────────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        global _bot_notify_cb
        sys.stdout = _BufferWriter()
        # Wire up crash toast so the App can surface it from the stream thread
        _bot_notify_cb = lambda msg: self.call_from_thread(
            self.notify, msg, severity="error", timeout=10
        )
        # Hide sub-panels that are only shown when their parent mode is active
        self.query_one("#always-wrap").display  = False
        self.query_one("#kelly-wrap").display   = False
        self.query_one("#hardstop-wrap").display = False
        self.query_one("#tg-wrap").display      = False
        self._load_settings()
        self.set_interval(1.0, self._tick)

    def on_unmount(self) -> None:
        sys.stdout = sys.__stdout__

    # ── Settings load ────────────────────────────────────────────────────────
    def _load_settings(self) -> None:
        cfg = read_cfg()
        if not cfg:
            return
        AGGR = {"selective": 0, "balanced": 1, "aggressive": 2}
        mode = cfg.get("mode", "balanced")

        if mode == "always":
            self._top_mode = "always"
            self._tog2("mode-smart", "mode-always")
            self.query_one("#smart-wrap").display = False
            self.query_one("#always-wrap").display = True
        else:
            self._aggr = AGGR.get(mode, 1)
            self._tog_grp("aggr-row", f"aggr-{self._aggr}")

        wm = cfg.get("wager_mode", "dollar")
        self._wager_mode = wm
        if wm == "percent":
            self._tog2("wager-dollar", "wager-pct")
        self._inp("#min-bet",    str(cfg.get("min_bet", 1.0)))
        self._inp("#max-bet",    str(cfg.get("max_session_wager", 5.0)))

        ke = bool(cfg.get("kelly_enabled", False))
        self._kelly_on = ke
        if ke:
            self._tog2("kelly-off", "kelly-on")
            self.query_one("#kelly-wrap").display = True
        self._inp("#kelly-frac", str(cfg.get("kelly_fraction", 0.5)))

        hs = bool(cfg.get("hard_stop_enabled", False))
        self._loss_mode = "hard_stop" if hs else "daily_loss"
        if hs:
            self._tog2("risk-daily", "risk-hardstop")
            self.query_one("#daily-wrap").display = False
            self.query_one("#hardstop-wrap").display = True
        self._inp("#loss-limit",   str(cfg.get("daily_loss_limit", 50)))
        self._inp("#hard-stop-amt",str(cfg.get("hard_stop_balance", 20)))

        lp = cfg.get("loss_period", "daily")
        self._loss_period = lp
        self._tog_grp("period-row", f"per-{lp}")

        self._inp("#always-open",  str(cfg.get("always_open",     6.0)))
        self._inp("#always-close", str(cfg.get("always_close",    3.0)))
        self._inp("#always-price", str(cfg.get("always_max_price", 0.75)))
        self._always_entry = "signal"

        tg_on = bool(cfg.get("telegram_enabled", False))
        if tg_on:
            self._tog2("tg-off", "tg-on")
            self.query_one("#tg-wrap").display = True
        else:
            self._tog2("tg-on", "tg-off")
            self.query_one("#tg-wrap").display = False
        self._inp("#tg-token", str(cfg.get("telegram_token", "")))
        users_raw = cfg.get("telegram_allowed_users", [])
        users_str = ",".join(str(u) for u in users_raw) if isinstance(users_raw, list) else str(users_raw)
        self._inp("#tg-users", users_str)


    def _inp(self, sel: str, val: str) -> None:
        try: self.query_one(sel, Input).value = val
        except Exception: pass

    def _tog2(self, off_id: str, on_id: str) -> None:
        """Flip exactly two buttons."""
        try:
            b = self.query_one(f"#{off_id}", Button)
            b.remove_class("ton"); b.add_class("tog")
        except Exception: pass
        try:
            b = self.query_one(f"#{on_id}", Button)
            b.remove_class("tog"); b.add_class("ton")
        except Exception: pass

    def _tog_grp(self, row_id: str, active_id: str) -> None:
        try:
            for btn in self.query_one(f"#{row_id}").query(Button):
                btn.remove_class("ton"); btn.add_class("tog")
            b = self.query_one(f"#{active_id}", Button)
            b.remove_class("tog"); b.add_class("ton")
        except Exception: pass

    def _val(self, sel: str, default: float) -> float:
        """Safe float read from an Input widget."""
        try:
            v = self.query_one(sel, Input).value
            return float(v) if v else default
        except Exception:
            return default

    def _str(self, sel: str) -> str:
        """Safe string read from an Input widget."""
        try:
            return self.query_one(sel, Input).value.strip()
        except Exception:
            return ""

    def _save(self) -> None:
        AGGR = ["selective", "balanced", "aggressive"]
        try:
            write_cfg({
                "mode":               "always" if self._top_mode == "always" else AGGR[self._aggr],
                "wager_mode":         self._wager_mode,
                "min_bet":            self._val("#min-bet",       1.0),
                "max_session_wager":  self._val("#max-bet",       5.0),
                "kelly_enabled":      self._kelly_on,
                "kelly_fraction":     self._val("#kelly-frac",    0.5),
                "hard_stop_enabled":  self._loss_mode == "hard_stop",
                "hard_stop_balance":  self._val("#hard-stop-amt", 20.0),
                "daily_loss_limit":   self._val("#loss-limit",    50.0),
                "loss_period":        self._loss_period,
                "always_open":        self._val("#always-open",   6.0),
                "always_close":       self._val("#always-close",  3.0),
                "always_max_price":   self._val("#always-price",  0.75),
                "trigger_method":     self._always_entry,
                "telegram_enabled":   self._tg_on,
                "telegram_token":     self._str("#tg-token"),
                "telegram_allowed_users": [
                    u.strip() for u in self._str("#tg-users").split(",") if u.strip()
                ],
            })
            self.notify("✓ Saved", severity="information", timeout=1)
        except Exception as e:
            self.notify(f"Save failed: {e}", severity="error", timeout=4)

    # ── Main tick ────────────────────────────────────────────────────────────
    def _tick(self) -> None:
        with state_lock:
            s = dict(app_state)

        trades = load_trades_today()
        stats  = compute_stats(trades)

        # Bot status + toggle button
        running = s.get("bot_running", False)
        sc = "#00ff88" if running else "#ff3b5c"
        st = "● RUNNING" if running else "○ STOPPED"
        bal = s.get("balance") or 0
        pnl = stats["pnl"]
        pc  = "#00ff88" if pnl >= 0 else "#ff3b5c"
        self.query_one("#bot-status", Static).update(
            f"[{sc}]{st}[/]  [dim]bal[/] [white]${bal:.2f}[/]"
            f"  [dim]P&L[/] [{pc}]{'+' if pnl>=0 else ''}${pnl:.2f}[/]"
        )
        btn = self.query_one("#toggle-btn", Button)
        if running:
            btn.label = "■  Stop"
            btn.remove_class("start-state"); btn.add_class("stop-state")
        else:
            btn.label = "▶  Start"
            btn.remove_class("stop-state"); btn.add_class("start-state")

        # Wager preview
        try:
            mb = float(self.query_one("#max-bet", Input).value or 5)
            if self._wager_mode == "percent" and bal:
                self.query_one("#wager-preview").update(f"[dim]≈ ${mb/100*bal:.2f} per trade[/]")
        except Exception: pass

        # Chart + distance label
        hist = s.get("price_history", [])
        btc  = s.get("btc_price") or 0
        tgt  = s.get("target_price")
        tdir = s.get("target_dir", "") or ""
        if hist:
            chart = self.query_one("#chart", BrailleChart)
            chart.prices = hist
            chart.target = tgt
            if btc and tgt:
                dist_signed = btc - tgt          # negative when BTC is below strike
                chart.dist_label = f"${dist_signed:+,.2f}"
                chart.dist_top   = tgt > btc   # target above → label at top
            else:
                chart.dist_label = None

        # Market row
        ticker = s.get("market_ticker") or "—"
        secs   = int(s.get("secs_remaining") or 0)
        m, ss  = secs // 60, secs % 60
        tc     = "#ff3b5c" if secs < 60 else "#ffc837" if secs < 180 else "#00ff88"

        tgt_s = f"${tgt:,.0f}" if tgt else "—"

        if btc and tgt:
            dist    = btc - tgt
            on_side = (tdir == "above" and dist > 0) or (tdir == "below" and dist < 0)
            dc      = "#00ff88" if on_side else "#ff3b5c"
            arrow   = "▲" if dist > 0 else "▼"
            dist_dollars = f"${dist:+,.2f}"
            side_lbl = "above" if dist > 0 else "below"
            ok_lbl   = "✓ on-side" if on_side else "✗ wrong-side"
            dist_str = (
                f"[bold {dc}]{arrow}  {dist_dollars}  {side_lbl} strike[/]"
                f"   [dim]—[/]   [{dc}]{ok_lbl}[/]"
            )
        else:
            dist_dollars = "—"
            dist_str = "[dim]waiting for price data…[/]"

        self.query_one("#mkt-row", Static).update(
            f"[bold #4a9eff]{ticker[-40:]}[/]   [{tc}]⏱  {m}:{ss:02d}[/]   "
            f"[dim]STRIKE[/] [bold white]{tgt_s}[/]   [dim]BTC[/] [bold white]${btc:,.0f}[/]\n"
            f"\n"
            f"  {dist_str}"
        )

        # Watching banner
        wb = self.query_one("#watch-banner", Static)
        if s.get("watching_active"):
            wd = s.get("watching_dir","?")
            wt = s.get("watching_thresh","?")
            wb.update(f"  ⏳ WATCHING [bold]{wd}[/] — waiting for price to drop below [bold]{wt}[/]")
            wb.display = True
        else:
            wb.display = False

        # Odds + conviction + prob bar
        ya  = s.get("yes_ask", 0) or 0
        na  = s.get("no_ask",  0) or 0
        yev = s.get("yes_ev",  0)
        nev = s.get("no_ev",   0)
        yp  = s.get("our_yes_prob", 0.5)
        np_ = s.get("our_no_prob",  0.5)
        conv= s.get("conviction", 0.0)

        yev_c = "#00ff88" if yev > 0.02 else "#ff3b5c" if yev < -0.02 else "#ffc837"
        nev_c = "#00ff88" if nev > 0.02 else "#ff3b5c" if nev < -0.02 else "#ffc837"

        # Center-out bars — same logic as signal bars
        HALF = 14
        lean_yes = yp >= 0.5
        bar_c    = "#00ff88" if lean_yes else "#ff3b5c"
        conv_c   = "#00ff88" if conv >= 0.65 else "#ffc837" if conv >= 0.4 else "#334455"

        prob_fill = min(HALF, int(abs(yp - 0.5) * 2 * HALF))
        conv_fill = min(HALF, int(conv * HALF))

        def center_bar(fill: int, color: str) -> Text:
            t = Text(no_wrap=True)
            if lean_yes:
                t.append("░" * HALF,            style="#1a2535")
                t.append("█" * fill,             style=f"bold {color}")
                t.append("░" * (HALF - fill),    style="#1a2535")
            else:
                t.append("░" * (HALF - fill),    style="#1a2535")
                t.append("█" * fill,             style=f"bold {color}")
                t.append("░" * HALF,             style="#1a2535")
            return t

        odds = Text(no_wrap=True)
        odds.append("NO ",         style="dim")
        odds.append(f"{na*100:.0f}¢  ", style="bold #ffc837")
        odds.append(f"EV {nev:+.3f}   ", style=nev_c)
        odds.append_text(center_bar(prob_fill, bar_c))
        odds.append("   ")
        odds.append("YES ",        style="dim")
        odds.append(f"{ya*100:.0f}¢  ", style="bold #00ff88")
        odds.append(f"EV {yev:+.3f}", style=yev_c)
        odds.append("\n\n")
        odds.append("PROB  NO ",   style="dim")
        odds.append(f"{np_*100:.0f}%   ", style="#ff3b5c")
        odds.append("CONV  ",      style="dim")
        odds.append_text(center_bar(conv_fill, conv_c))
        odds.append(f"  {conv:.2f}   ", style="bold white")
        odds.append("YES ",        style="dim")
        odds.append(f"{yp*100:.0f}%", style="#00ff88")

        self.query_one("#odds-row", Static).update(odds)

        # Position panel
        pos = s.get("position")
        pp  = self.query_one("#pos-panel", Static)
        if pos and pos.get("ticker"):
            side  = pos.get("side", "?").upper()
            sc2   = "#00ff88" if side == "YES" else "#ffc837"
            pmins = pos.get("mins_remaining", 0)
            ptc   = "#ff3b5c" if pmins < 2 else "#ffc837" if pmins < 4 else "white"
            qty   = pos.get("contracts", 0)
            cost  = pos.get("cost", 0)
            entry = pos.get("entry", 0)
            pp.update(
                f"[bold {sc2}]● OPEN POSITION — {side}[/]\n"
                f"[dim]  entry [/][white]{entry:.2f}¢[/]"
                f"  [dim]qty [/][white]{qty}[/]"
                f"  [dim]cost [/][white]${cost:.2f}[/]"
                f"  [dim]expires [/][{ptc}]⏱ {pmins:.1f} min[/]"
            )
            pp.display = True
        else:
            pp.display = False

        # Macro
        def tc_(v, lbl):
            if v is None: return f"[dim]{lbl} —[/]"
            c = "#00ff88" if v > 0 else "#ff3b5c"
            return f"[dim]{lbl}[/] [{c}]{v:+.2f}%[/]"

        rng = s.get("weekly_range_pct")
        vol = s.get("volatility")
        rng_s = f"[dim]7D[/] [white]{rng*100:.0f}%[/]" if rng is not None else "[dim]7D —[/]"
        vol_s = f"[dim]vol[/] [white]{vol:.1f}x[/]" if vol is not None else ""
        self.query_one("#macro-row", Static).update(
            f"  {tc_(s.get('trend_1h'),'1H')}  {tc_(s.get('trend_6h'),'6H')}"
            f"  {tc_(s.get('trend_24h'),'24H')}  {rng_s}  {vol_s}"
        )

        # Signals — yes_prob (0-1) + strength (0-1), center-out bar
        raw  = s.get("signals", [])
        HALF = 12   # half-bar width each side = 24 total
        sig_text = Text(no_wrap=True, overflow="crop")
        sig_text.append("◈ SIGNALS\n", style="bold #ffc837")
        for sg in raw[:7]:
            nm   = sg.get("name",     "?")[:13]
            yp   = sg.get("yes_prob", 0.5)
            str_ = sg.get("strength", 0.0)
            rsn  = sg.get("reason",   "")[:28]
            pct  = int(yp * 100)
            fill = min(HALF, int(abs(yp - 0.5) * 2 * HALF * (0.4 + str_ * 0.6)))
            pc   = ("#00ff88" if yp > 0.55 else
                    "#ff3b5c" if yp < 0.45 else "#ffc837")
            sig_text.append(f" {nm:<13} ", style="dim")
            if yp >= 0.5:
                sig_text.append("░" * HALF,        style="#1a2535")
                sig_text.append("█" * fill,         style=f"bold {pc}")
                sig_text.append("░" * (HALF - fill),style="#1a2535")
            else:
                sig_text.append("░" * (HALF - fill),style="#1a2535")
                sig_text.append("█" * fill,         style=f"bold {pc}")
                sig_text.append("░" * HALF,         style="#1a2535")
            sig_text.append(f" {pct}%\n", style=pc)
            sig_text.append(f"   {rsn}\n", style="dim #334455")
        if not raw:
            sig_text.append(" waiting for market data…", style="dim")
        self.query_one("#sig-panel", Static).update(sig_text)

        # Trade stats
        pf_c = "#00ff88" if stats["profit_factor"] >= 1 else "#ff3b5c"
        ev_c = "#00ff88" if stats["ev"] >= 0 else "#ff3b5c"
        wr_c = "#00ff88" if stats["win_rate"] >= 0.5 else "#ff3b5c"
        self.query_one("#trade-stats", Static).update(
            f"[dim]TODAY[/]  "
            f"[#00ff88]W {stats['wins']}[/] / [#ff3b5c]L {stats['losses']}[/]"
            f"  [{wr_c}]{stats['win_rate']*100:.0f}% WR[/]\n"
            f"[dim]avg W[/] [#00ff88]+${stats['avg_win']:.2f}[/]"
            f"  [dim]avg L[/] [#ff3b5c]-${stats['avg_loss']:.2f}[/]\n"
            f"[dim]P.Factor[/] [{pf_c}]{stats['profit_factor']:.2f}[/]"
            f"  [dim]EV[/] [{ev_c}]{stats['ev']:+.3f}[/]"
            f"  [{pc}]P&L {'+' if pnl>=0 else ''}${pnl:.2f}[/]"
        )

        # Trade list
        rows = ["[dim] TIME   SIDE  QTY   ENTRY    P&L[/]"]
        for t in reversed(trades[-10:]):
            ts   = (t.get("timestamp","")[-8:] or "")[:5]
            sd   = t.get("side","?").upper()
            sc4  = "#00ff88" if sd=="YES" else "#ffc837"
            qty  = t.get("contracts","?")
            px   = float(t.get("price", 0) or 0)
            raw_pnl = t.get("pnl","?")
            if raw_pnl == "pending":
                pnl_s = "[#ffc837]pend[/]"
            else:
                try:
                    pv = float(raw_pnl)
                    xc = "#00ff88" if pv > 0 else "#ff3b5c"
                    pnl_s = f"[{xc}]{'+' if pv>0 else ''}${pv:.2f}[/]"
                except Exception:
                    pnl_s = raw_pnl
            rows.append(f" [dim]{ts}[/] [{sc4}]{sd:<3}[/] [white]{qty:>3}[/]  [dim]{px:.2f}[/]  {pnl_s}")
        self.query_one("#trade-list", Static).update("\n".join(rows))

        # Bot log (append-only, scroll-friendly)
        lines = list(bot_log_buffer)
        # Handle deque rollover: if _log_n exceeds current buffer size, reset to
        # show the whole buffer again (avoids permanent freeze after 400 lines)
        if self._log_n > len(lines):
            self._log_n = 0
        new = lines[self._log_n:]
        if new:
            log = self.query_one("#bot-log", RichLog)
            for l in new:
                if "FIRE" in l or "✅" in l or "CONFIRMED" in l:
                    log.write(f"[bold #00ff88]{l}[/]")
                elif "⚠" in l or "WATCHING" in l or "waiting" in l:
                    log.write(f"[#ffc837]{l}[/]")
                elif "KELLY" in l or "SKIP" in l or "edge" in l:
                    log.write(f"[#4a9eff]{l}[/]")
                elif "❌" in l or "ERROR" in l or "KILL" in l:
                    log.write(f"[bold #ff3b5c]{l}[/]")
                else:
                    log.write(f"[dim]{l}[/dim]")
            self._log_n = len(lines)

        # ── Auto-monitor: Claude proactively checks in every 5 min ───────────
        self._monitor_tick += 1
        if self._monitor_tick >= self._MONITOR_EVERY:
            self._monitor_tick = 0
            self._auto_monitor()

    def _auto_monitor(self) -> None:
        """Fire a background Claude call to proactively monitor the session."""
        with state_lock:
            snap = dict(app_state)
        if not snap.get("bot_running") and not snap.get("market_ticker"):
            return   # nothing to monitor yet
        log = self.query_one("#claude-log", RichLog)
        log.write("[dim #4a9eff]◈ auto-monitor check…[/]")
        cfg_snap    = read_cfg()
        trades_snap = load_trades_today()
        log_snap    = list(bot_log_buffer)
        fp = self._firepower
        threading.Thread(
            target=_claude_ask,
            args=(
                "Proactive monitor check. Review all current data and flag anything worth acting on. Be concise.",
                snap, cfg_snap,
                lambda txt: self.call_from_thread(log.write, txt),
                lambda c:   self.call_from_thread(self._apply_settings, c),
                fp,
                lambda: self.call_from_thread(self._fire_bot_from_claude),
                trades_snap,
                log_snap,
            ),
            daemon=True,
        ).start()

    # ── Button handlers ──────────────────────────────────────────────────────
    @on(Button.Pressed, "#toggle-btn")
    def _h_toggle(self):
        with state_lock:
            running = app_state.get("bot_running", False)
        if running:
            stop_bot(); self.notify("Bot stopped.", severity="warning")
        else:
            start_bot(); self.notify("Bot starting…", severity="information")

    @on(Button.Pressed, "#mode-smart")
    def _m_smart(self):
        self._top_mode = "smart"; self._tog2("mode-always","mode-smart")
        self.query_one("#smart-wrap").display = True
        self.query_one("#always-wrap").display = False; self._save()
    @on(Button.Pressed, "#mode-always")
    def _m_always(self):
        self._top_mode = "always"; self._tog2("mode-smart","mode-always")
        self.query_one("#smart-wrap").display = False
        self.query_one("#always-wrap").display = True; self._save()

    @on(Button.Pressed, "#aggr-0")
    def _a0(self): self._aggr=0; self._tog_grp("aggr-row","aggr-0"); self._save()
    @on(Button.Pressed, "#aggr-1")
    def _a1(self): self._aggr=1; self._tog_grp("aggr-row","aggr-1"); self._save()
    @on(Button.Pressed, "#aggr-2")
    def _a2(self): self._aggr=2; self._tog_grp("aggr-row","aggr-2"); self._save()

    @on(Button.Pressed, "#wager-dollar")
    def _wd(self): self._wager_mode="dollar";  self._tog2("wager-pct",    "wager-dollar"); self._save()
    @on(Button.Pressed, "#wager-pct")
    def _wp(self): self._wager_mode="percent"; self._tog2("wager-dollar", "wager-pct");    self._save()

    @on(Button.Pressed, "#kelly-on")
    def _kon(self):
        self._kelly_on=True;  self._tog2("kelly-off","kelly-on")
        self.query_one("#kelly-wrap").display=True;  self._save()
    @on(Button.Pressed, "#kelly-off")
    def _koff(self):
        self._kelly_on=False; self._tog2("kelly-on","kelly-off")
        self.query_one("#kelly-wrap").display=False; self._save()

    @on(Button.Pressed, "#risk-daily")
    def _rd(self):
        self._loss_mode="daily_loss"; self._tog2("risk-hardstop","risk-daily")
        self.query_one("#daily-wrap").display=True
        self.query_one("#hardstop-wrap").display=False; self._save()
    @on(Button.Pressed, "#risk-hardstop")
    def _rh(self):
        self._loss_mode="hard_stop";  self._tog2("risk-daily","risk-hardstop")
        self.query_one("#daily-wrap").display=False
        self.query_one("#hardstop-wrap").display=True;  self._save()

    @on(Button.Pressed, "#per-daily")
    def _lpd(self): self._loss_period="daily";  self._tog_grp("period-row","per-daily");  self._save()
    @on(Button.Pressed, "#per-hourly")
    def _lph(self): self._loss_period="hourly"; self._tog_grp("period-row","per-hourly"); self._save()
    @on(Button.Pressed, "#per-weekly")
    def _lpw(self): self._loss_period="weekly"; self._tog_grp("period-row","per-weekly"); self._save()

    @on(Button.Pressed, "#tg-on")
    def _tg_enable(self):
        self._tg_on = True;  self._tog2("tg-off", "tg-on")
        self.query_one("#tg-wrap").display = True;  self._save()
    @on(Button.Pressed, "#tg-off")
    def _tg_disable(self):
        self._tg_on = False; self._tog2("tg-on", "tg-off")
        self.query_one("#tg-wrap").display = False; self._save()

    @on(Button.Pressed, "#setup-btn")
    def _h_setup(self): self.action_setup()

    def _apply_settings(self, changes: dict) -> None:
        """Write Claude-requested setting changes and reload UI."""
        changes = {_CFG_KEY_ALIASES.get(k, k): v for k, v in changes.items()}
        safe = {k: v for k, v in changes.items() if k not in _PROTECTED_SETTINGS}
        if not safe:
            return
        try:
            write_cfg(safe)
            self._load_settings()
            keys = ", ".join(safe.keys())
            self.notify(f"⚙ Applied: {keys}", severity="information", timeout=4)
        except Exception as e:
            self.notify(f"Apply failed: {e}", severity="error")

    def _update_claude_header(self) -> None:
        fp_tag = (
            "  [bold #ff4500]🔥 FIREPOWER ON[/]" if self._firepower else ""
        )
        self.query_one("#claude-header", Static).update(
            f"[bold #4a9eff]◈ CLAUDE[/]  [dim]trading co-pilot — monitors, advises, adjusts[/]{fp_tag}"
        )

    @on(Input.Submitted, "#claude-input")
    def _claude_submit(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return
        event.input.value = ""
        log = self.query_one("#claude-log", RichLog)

        # Handle firepower toggle locally — no need to round-trip Claude
        ql = query.lower()
        if "enable firepower" in ql or "firepower on" in ql:
            self._firepower = True
            self._update_claude_header()
            log.write("[bold #ff4500]🔥 Firepower ENABLED — Claude can now fire trades.[/]")
            log.write("[dim #ffc837]Type 'disable firepower' or 'firepower off' to revoke.[/]")
            return
        if "disable firepower" in ql or "firepower off" in ql:
            self._firepower = False
            self._update_claude_header()
            log.write("[bold #00ff88]✓ Firepower DISABLED.[/]")
            return

        log.write(f"[dim #4a9eff]▶[/] {query}")
        with state_lock:
            snap = dict(app_state)
        cfg_snap  = read_cfg()
        trades_snap = load_trades_today()
        log_snap  = list(bot_log_buffer)
        fp = self._firepower
        threading.Thread(
            target=_claude_ask,
            args=(
                query, snap, cfg_snap,
                lambda txt: self.call_from_thread(log.write, txt),
                lambda c:   self.call_from_thread(self._apply_settings, c),
                fp,
                lambda: self.call_from_thread(self._fire_bot_from_claude),
                trades_snap,
                log_snap,
            ),
            daemon=True,
        ).start()

    def _fire_bot_from_claude(self) -> None:
        """Called when Claude issues FIRE_BOT and firepower is enabled."""
        log = self.query_one("#claude-log", RichLog)
        log.write("[bold #ff4500]🔥 FIRE_BOT — Claude is starting the bot![/]")
        start_bot()
        self.notify("🔥 Claude fired the bot!", severity="warning")

    @on(Input.Submitted)
    def _inp_submit(self, event: Input.Submitted) -> None:
        if event.input.id != "claude-input":
            self._save()

    def action_start_bot(self):
        with state_lock:
            running = app_state.get("bot_running", False)
        if running:
            stop_bot();  self.notify("Bot stopped.",  severity="warning")
        else:
            start_bot(); self.notify("Bot starting…", severity="information")
    def action_stop_bot(self): stop_bot(); self.notify("Bot stopped.", severity="warning")

    def action_setup(self) -> None:
        stop_bot()
        setup = BOT_DIR / "setup.sh"
        if setup.exists():
            import subprocess as _sp
            try:
                # macOS: open a new Terminal window running setup.sh
                _sp.Popen([
                    "osascript", "-e",
                    f'tell application "Terminal" to do script "bash {setup}"'
                ])
                self.notify("Opening setup in Terminal…", severity="information")
            except Exception:
                self.notify(f"Run: bash {setup}", severity="warning", timeout=8)
        else:
            self.notify("setup.sh not found — run from BTC_KILLER_CLI folder", severity="error")


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=background_updater, daemon=True).start()
    time.sleep(0.4)
    BTCKillerApp().run()
