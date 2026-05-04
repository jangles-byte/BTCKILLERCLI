#!/usr/bin/env python3
"""
BTC.KILLER CLI — Full terminal dashboard
Cosmetic only — bot logic unchanged.
"""
from __future__ import annotations
import csv, io, json, os, subprocess, sys, threading, time
from collections import deque
from datetime import datetime, date
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────────
BOT_DIR    = Path(__file__).resolve().parent
CONFIG     = BOT_DIR / "bot_config.json"
POS_FILE   = BOT_DIR / "current_position.json"
TRADES_DIR = BOT_DIR / "trades"

# ── Alien banner animation ────────────────────────────────────────────────────
# (le, re, ant_left, ant_right)
_EYE_SEQ: list[tuple[str,str,str,str]] = [
    ("◉","◉","✦","✦"), ("◉","◉","✦","✦"), ("◉","◉","✦","✦"),
    ("◉","◉","·","·"), ("◉","◉","✦","✦"), ("◉","◉","✦","·"),
    ("◕","◕","✦","✦"), ("◕","◕","✦","✦"), ("◕","◕","·","✦"),
    ("◉","◉","✦","✦"),
    ("◔","◔","✦","✦"), ("◔","◔","·","✦"), ("◔","◔","✦","✦"),
    ("◉","◉","✦","✦"),
    ("─","─","✦","✦"),                     # blink
    ("◉","◉","✦","✦"), ("◉","◉","✦","✦"),
    ("◉","─","✦","·"), ("◉","─","·","✦"), # wink
    ("◉","◉","✦","✦"),
    ("◎","◎","✦","✦"), ("◎","◎","✦","✦"), # wide
    ("─","─","·","·"),                     # blink
    ("◉","◉","✦","✦"), ("◉","◉","✦","✦"), ("◉","◉","✦","✦"),
]

_BTC_KILLER_LINES = [
    " ██████╗ ████████╗ ██████╗    ██╗  ██╗██╗██╗     ██╗     ███████╗██████╗ ",
    " ██╔══██╗╚══██╔══╝██╔════╝    ██║ ██╔╝██║██║     ██║     ██╔════╝██╔══██╗",
    " ██████╔╝   ██║   ██║         █████╔╝ ██║██║     ██║     █████╗  ██████╔╝",
    " ██╔══██╗   ██║   ██║         ██╔═██╗ ██║██║     ██║     ██╔══╝  ██╔══██╗",
    " ██████╔╝   ██║   ╚██████╗    ██║  ██╗██║███████╗███████╗███████╗██║  ██║",
    " ╚═════╝    ╚═╝    ╚═════╝    ╚═╝  ╚═╝╚═╝╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝",
]

def _alien_lines(le: str, re: str, a0: str, a1: str) -> list[str]:
    """8-line alien body; caller handles per-char colouring."""
    return [
        f"      {a0}         {a1}      ",
        "     /|         |\\     ",
        "    ╔═════════════╗    ",
        f"   ╔╝  {le}       {re}  ╚╗   ",
        "   ║    ╰─────╯    ║   ",
        "   ║   ╱───────╲   ║   ",
        "    ╚═════════════╝    ",
        "    ╱╲           ╱╲    ",
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
def _stream(proc: subprocess.Popen) -> None:
    try:
        for line in iter(proc.stdout.readline, b""):
            t = line.decode("utf-8", errors="replace").rstrip()
            if t:
                bot_log_buffer.append(t)
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
                   target: float | None = None) -> "Text":
    from rich.text import Text

    if not values or width < 4 or height < 2:
        return Text("  — waiting for price feed —", style="dim #1a2535")

    px_w = width * 2
    px_h = height * 4
    v_min = min(values); v_max = max(values)
    pad   = max((v_max - v_min) * 0.08, 30)
    v_min -= pad; v_max += pad

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

    # Target dashed line
    if target is not None and v_min < target < v_max:
        tr = to_px(target)
        for c in range(px_w):
            if (c // 3) % 2 == 0:
                target_g[tr][c] = True
                if 0 < tr < px_h - 1:
                    target_g[tr - 1][c] = True  # slightly thicker

    DOT = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]]

    # Price labels (right side)
    top_label = f" ${v_max + pad:,.0f}"
    bot_label = f" ${v_min + pad:,.0f}"

    # Current price arrow position
    cur_row = to_px(values[-1]) // 4 if values else 0

    result = Text()
    for cr in range(height):
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
                # gradient: newer data is brighter
                col_frac = (cc + 1) / width
                if col_frac > 0.85:    result.append(ch, style="bold #00ff88")
                elif col_frac > 0.5:   result.append(ch, style="#00cc66")
                else:                  result.append(ch, style="#007744")
            else:                result.append(ch, style="bold white")

        # Side labels
        if cr == 0:
            result.append(f" [dim]{v_max:,.0f}[/]", style="")
        elif cr == cur_row:
            btc_now = values[-1] if values else 0
            result.append(f" [bold #00ff88]◀ ${btc_now:,.0f}[/]", style="")
        elif target is not None and cr == to_px(target) // 4:
            result.append(f" [#ffc837]── target ${target:,.0f}[/]", style="")
        elif cr == height - 1:
            result.append(f" [dim]{v_min:,.0f}[/]", style="")

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

class BrailleChart(Widget):
    prices: reactive[list[float]] = reactive(list, layout=True)
    target: reactive[float | None] = reactive(None, layout=True)

    def render(self) -> RenderableType:
        w = self.size.width - 2
        h = self.size.height - 2
        if w < 2 or h < 2: return Text("")
        return _braille_chart(self.prices, w, h, self.target)


class AlienBanner(Widget):
    """Animated alien + BTC KILLER block-text banner."""
    _frame: reactive[int] = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(0.18, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_EYE_SEQ)

    def render(self) -> RenderableType:
        le, re, a0, a1 = _EYE_SEQ[self._frame]
        alien = _alien_lines(le, re, a0, a1)
        # Pad BTC KILLER to 8 rows (blank top + bottom)
        text_rows = [""] + _BTC_KILLER_LINES + [""]

        G  = "bold #00ff88"   # green — BTC KILLER
        C  = "#33ccee"        # cyan  — alien body
        R  = "bold #ff4444"   # red   — eyes
        YB = "bold #ffcc00"   # bright yellow — antenna on
        YD = "#886600"        # dim yellow    — antenna off

        out = Text(justify="left")
        for i in range(8):
            if i > 0:
                out.append("\n")

            # ── BTC KILLER side ──────────────────────────────────────
            tl = text_rows[i] if i < len(text_rows) else ""
            out.append(tl.ljust(74), style=G)
            out.append("  ")   # spacer

            # ── Alien side ───────────────────────────────────────────
            if i == 0:
                # antenna tips: colour each tip char
                out.append("      ", style=C)
                out.append(a0, style=YB if a0 == "✦" else YD)
                out.append("         ", style=C)
                out.append(a1, style=YB if a1 == "✦" else YD)
                out.append("      ", style=C)
            elif i == 3:
                # eye row: colour each eye char
                prefix = "   ╔╝  "
                middle = "       "
                suffix = "  ╚╗   "
                eye_style = R if le not in ("─", " ") else "bold #aaaaaa"
                out.append(prefix, style=C)
                out.append(le, style=eye_style)
                out.append(middle, style=C)
                out.append(re, style=R if re not in ("─", " ") else "bold #aaaaaa")
                out.append(suffix, style=C)
            else:
                out.append(alien[i], style=C)
        return out


CSS = """
Screen { background: #060a12; layout: vertical; }

#banner {
    height: 8; border-bottom: solid #1a2535;
    background: #080c14; padding: 0 2;
}
#main-row { height: 1fr; }

/* ── Settings ── */
#settings {
    width: 27; background: #080c14;
    border-right: solid #1a2535; padding: 0 1;
}
.sec { color: #ffc837; text-style: bold; height: 1; margin: 1 0 0 0; }
.lbl { color: #334455; height: 1; }
.tr  { height: 3; margin: 0 0 0 0; }

.tog {
    width: 1fr; height: 3; min-width: 0;
    background: #0a0f1a; border: solid #1a2535; color: #334455;
}
.tog:hover { background: #141c2a; color: #aaa; }
.ton {
    width: 1fr; height: 3; min-width: 0;
    background: #001a10; border: solid #00883a; color: #00ff88; text-style: bold;
}
.ton:hover { background: #002a18; }

Input {
    height: 3; background: #0a0f1a; border: solid #1a2535;
    color: #fff; margin: 0 0 1 0; padding: 0 1;
}
Input:focus { border: solid #ffc837; }
.prev { color: #334455; height: 1; margin: 0 0 1 0; }
.sub-wrap { padding: 0; margin: 0; }

#start-btn {
    width: 1fr; height: 3; background: #001a10;
    color: #00ff88; border: solid #00883a; margin: 0 1 1 0;
}
#start-btn:hover { background: #003320; }
#stop-btn  {
    width: 1fr; height: 3; background: #1a0008;
    color: #ff3b5c; border: solid #882030; margin: 0 0 1 0;
}
#stop-btn:hover { background: #2a0010; }

/* ── Center ── */
#center { width: 1fr; background: #060a12; padding: 0 1; }

BrailleChart {
    height: 16; border: solid #1a2535;
    background: #060a12; margin: 0 0 1 0;
}
#mkt-row {
    height: 2; border: solid #1a2535; background: #080c14;
    padding: 0 1; margin: 0 0 1 0;
}
#watch-banner {
    height: 2; color: #ffc837; background: #110e00;
    border: solid #553300; padding: 0 1; margin: 0 0 1 0;
}
#odds-row {
    height: 5; border: solid #1a2535;
    background: #080c14; padding: 0 1; margin: 0 0 1 0;
}
#pos-panel {
    height: 3; border: solid #1a2535;
    background: #080c14; padding: 0 1; margin: 0 0 1 0;
}
#macro-row {
    height: 2; border: solid #1a2535;
    background: #080c14; padding: 0 1; margin: 0 0 1 0;
}
#sig-panel {
    height: 1fr; border: solid #1a2535;
    background: #080c14; padding: 0 1;
}

/* ── Right ── */
#right { width: 42; background: #060a12; border-left: solid #1a2535; padding: 0 1; }
#bot-log {
    height: 1fr; border: solid #1a2535;
    background: #080c14; scrollbar-size: 1 1;
}
#trade-stats {
    height: 6; border: solid #1a2535;
    background: #080c14; padding: 0 1; margin: 1 0 0 0;
}
#trade-list {
    height: 10; border: solid #1a2535;
    background: #080c14; padding: 0 1; margin: 1 0 0 0;
}

Footer { background: #080c14; color: #1a2535; }
"""


class BTCKillerApp(App):
    CSS = CSS
    BINDINGS = [
        ("s", "start_bot", "Start"),
        ("x", "stop_bot",  "Stop"),
        ("q", "quit",      "Quit"),
    ]

    _log_n: int = 0
    _log_last: str = ""

    # Settings state
    _top_mode:    str  = "smart"
    _aggr:        int  = 1
    _wager_mode:  str  = "dollar"
    _loss_mode:   str  = "daily_loss"
    _loss_period: str  = "daily"
    _kelly_on:    bool = False
    _tg_on:       bool = False
    _always_entry:str  = "ev"

    def compose(self) -> ComposeResult:
        yield AlienBanner(id="banner")
        with Horizontal(id="main-row"):

            # ── LEFT: settings ──────────────────────────────────────────────
            with ScrollableContainer(id="settings"):
                yield Static("◈ BOT", classes="sec")
                with Horizontal(classes="tr"):
                    yield Button("▶ Start", id="start-btn")
                    yield Button("■ Stop",  id="stop-btn")
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
                    yield Static("Open window (min left)", classes="lbl")
                    yield Input(value="6.0", id="always-open")
                    yield Static("Close window (min left)", classes="lbl")
                    yield Input(value="3.0", id="always-close")
                    yield Static("Max price (¢)", classes="lbl")
                    yield Input(value="75",  id="always-price")
                    yield Static("Entry method", classes="lbl")
                    with Horizontal(classes="tr", id="entry-row"):
                        yield Button("EV",     id="entry-ev",  classes="ton")
                        yield Button("Signal", id="entry-sig", classes="tog")

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
                    yield Button("Enable",  id="tg-enable",  classes="tog")
                    yield Button("Disable", id="tg-disable", classes="ton")
                yield Static("Bot token", classes="lbl")
                yield Input(value="", id="tg-token",  placeholder="@BotFather token", password=True)
                yield Static("Allowed user IDs", classes="lbl")
                yield Input(value="", id="tg-users",  placeholder="123456, 789012")
                yield Static("(Press Enter to save)", classes="prev")

            # ── CENTER ──────────────────────────────────────────────────────
            with Vertical(id="center"):
                yield BrailleChart(id="chart")
                yield Static("", id="mkt-row")
                yield Static("", id="watch-banner")
                yield Static("", id="odds-row")
                yield Static("", id="pos-panel")
                yield Static("", id="macro-row")
                yield Static("", id="sig-panel")

            # ── RIGHT ────────────────────────────────────────────────────────
            with Vertical(id="right"):
                yield RichLog(id="bot-log", highlight=False, markup=True,
                              wrap=False, auto_scroll=True)
                yield Static("", id="trade-stats")
                yield Static("", id="trade-list")

        yield Footer()

    # ── Mount ────────────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        sys.stdout = _BufferWriter()
        # Hide sub-panels that are only shown when their parent mode is active
        self.query_one("#always-wrap").display  = False
        self.query_one("#kelly-wrap").display   = False
        self.query_one("#hardstop-wrap").display = False
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
        self._inp("#max-bet",    str(cfg.get("max_market_wager", 5.0)))

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

        self._inp("#always-open",  str(cfg.get("always_open_window", 6.0)))
        self._inp("#always-close", str(cfg.get("always_close_window", 3.0)))
        self._inp("#always-price", str(cfg.get("always_price_cap", 75)))
        ae = cfg.get("always_entry_method", "ev")
        self._always_entry = ae
        if ae == "signal":
            self._tog2("entry-ev", "entry-sig")

        te = bool(cfg.get("telegram_enabled", False))
        self._tg_on = te
        if te:
            self._tog2("tg-disable", "tg-enable")
        if cfg.get("telegram_token"):
            self._inp("#tg-token", cfg["telegram_token"])
        if cfg.get("telegram_allowed_users"):
            self._inp("#tg-users", ", ".join(str(u) for u in cfg["telegram_allowed_users"]))

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

    def _save(self) -> None:
        AGGR = ["selective", "balanced", "aggressive"]
        try:
            write_cfg({
                "mode":                  "always" if self._top_mode == "always" else AGGR[self._aggr],
                "wager_mode":            self._wager_mode,
                "min_bet":               float(self.query_one("#min-bet",       Input).value or 1),
                "max_market_wager":      float(self.query_one("#max-bet",       Input).value or 5),
                "kelly_enabled":         self._kelly_on,
                "kelly_fraction":        float(self.query_one("#kelly-frac",    Input).value or 0.5),
                "hard_stop_enabled":     self._loss_mode == "hard_stop",
                "hard_stop_balance":     float(self.query_one("#hard-stop-amt", Input).value or 20),
                "daily_loss_limit":      float(self.query_one("#loss-limit",    Input).value or 50),
                "loss_period":           self._loss_period,
                "always_open_window":    float(self.query_one("#always-open",   Input).value or 6),
                "always_close_window":   float(self.query_one("#always-close",  Input).value or 3),
                "always_price_cap":      float(self.query_one("#always-price",  Input).value or 75),
                "always_entry_method":   self._always_entry,
                "telegram_enabled":      self._tg_on,
                "telegram_token":        self.query_one("#tg-token", Input).value.strip(),
                "telegram_allowed_users": [
                    u.strip() for u in self.query_one("#tg-users", Input).value.split(",") if u.strip()
                ],
            })
        except Exception: pass

    # ── Main tick ────────────────────────────────────────────────────────────
    def _tick(self) -> None:
        with state_lock:
            s = dict(app_state)

        trades = load_trades_today()
        stats  = compute_stats(trades)

        # Bot status line
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

        # Wager preview
        try:
            mb = float(self.query_one("#max-bet", Input).value or 5)
            if self._wager_mode == "percent" and bal:
                self.query_one("#wager-preview").update(f"[dim]≈ ${mb/100*bal:.2f} per trade[/]")
        except Exception: pass

        # Chart
        hist = s.get("price_history", [])
        if hist:
            chart = self.query_one("#chart", BrailleChart)
            chart.prices = hist
            chart.target = s.get("target_price")

        # Market row
        ticker = s.get("market_ticker") or "—"
        secs   = int(s.get("secs_remaining") or 0)
        btc    = s.get("btc_price") or 0
        tgt    = s.get("target_price")
        tdir   = s.get("target_dir", "") or ""
        m, ss  = secs // 60, secs % 60
        tc     = "#ff3b5c" if secs < 60 else "#ffc837" if secs < 180 else "#00ff88"

        dist_str = ""
        if btc and tgt:
            dist = btc - tgt
            dc = "#00ff88" if (tdir=="above" and dist>0) or (tdir=="below" and dist<0) else "#ff3b5c"
            dist_str = f"  [{dc}]{'+' if dist>0 else ''}${dist:,.0f} {tdir} target[/]"

        self.query_one("#mkt-row", Static).update(
            f"[#4a9eff]{ticker[-30:]}[/]  [{tc}]{m}:{ss:02d}[/]"
            f"  [dim]strike[/] [white]${tgt:,.0f}[/]"
            f"{dist_str}"
            f"  [bold white]BTC ${btc:,.0f}[/]"
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

        bar_w = 20
        nf = int(np_ * bar_w); yf = bar_w - nf
        prob_bar = f"[#ff3b5c]{'█'*nf}[/][#00ff88]{'█'*yf}[/]"

        cv = int(conv * 14)
        conv_bar = "█" * cv + "░" * (14 - cv)
        conv_c   = "#00ff88" if conv >= 0.65 else "#ffc837" if conv >= 0.4 else "#334455"

        self.query_one("#odds-row", Static).update(
            f" [dim]NO[/]  [bold #ffc837]{na*100:.0f}¢[/]  [{nev_c}]EV {nev:+.3f}[/]"
            f"   {prob_bar}"
            f"   [dim]YES[/] [bold #00ff88]{ya*100:.0f}¢[/]  [{yev_c}]EV {yev:+.3f}[/]\n"
            f" [dim]NO[/] [{nev_c}]{np_*100:.0f}%[/]  "
            f"conv [{conv_c}]{conv_bar}[/] {conv:.2f}  "
            f"[dim]YES[/] [{yev_c}]{yp*100:.0f}%[/]"
        )

        # Position
        pos = s.get("position")
        pp  = self.query_one("#pos-panel", Static)
        if pos and pos.get("ticker"):
            side  = pos.get("side","?").upper()
            sc2   = "#00ff88" if side=="YES" else "#ffc837"
            pmins = pos.get("mins_remaining", 0)
            ptc   = "#ff3b5c" if pmins < 2 else "white"
            pp.update(
                f"  [dim]POS[/] [{sc2}]{side}[/]"
                f"  [dim]entry[/] [white]${pos.get('entry',0):.3f}[/]"
                f"  [dim]qty[/] [white]{pos.get('contracts',0)}[/]"
                f"  [dim]cost[/] [white]${pos.get('cost',0):.2f}[/]"
                f"  [dim]left[/] [{ptc}]{pmins:.1f}m[/]"
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

        # Signals
        raw  = s.get("signals", [])
        sigs = ["[bold #ffc837]◈ SIGNALS[/]"]
        for sg in raw[:8]:
            nm  = sg.get("name","?")[:16]
            val = sg.get("value", 0)
            bl  = min(14, int(abs(val) * 14))
            sc3 = "#00ff88" if val > 0 else "#ff3b5c" if val < 0 else "#334455"
            bar = f"[{sc3}]{'█'*bl}[/]{'░'*(14-bl)}"
            sigs.append(f" [dim]{nm:<16}[/] {bar} [{sc3}]{val:+.2f}[/]")
        self.query_one("#sig-panel", Static).update("\n".join(sigs))

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
        rows = ["[dim] TIME  SIDE  QTY  ENTRY  P&L[/]"]
        for t in reversed(trades[-8:]):
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
        if lines and lines[-1] != self._log_last:
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
            self._log_last = lines[-1]

    # ── Button handlers ──────────────────────────────────────────────────────
    @on(Button.Pressed, "#start-btn")
    def _h_start(self): start_bot(); self.notify("Bot starting…", severity="information")
    @on(Button.Pressed, "#stop-btn")
    def _h_stop(self):  stop_bot();  self.notify("Bot stopped.", severity="warning")

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

    @on(Button.Pressed, "#entry-ev")
    def _ee(self): self._always_entry="ev";     self._tog2("entry-sig","entry-ev");  self._save()
    @on(Button.Pressed, "#entry-sig")
    def _es(self): self._always_entry="signal"; self._tog2("entry-ev", "entry-sig"); self._save()

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

    @on(Button.Pressed, "#tg-enable")
    def _ten(self):  self._tg_on=True;  self._tog2("tg-disable","tg-enable");  self._save()
    @on(Button.Pressed, "#tg-disable")
    def _tdis(self): self._tg_on=False; self._tog2("tg-enable", "tg-disable"); self._save()

    @on(Input.Submitted)
    def _inp_submit(self, _):
        self._save()
        self.notify("Saved", severity="information", timeout=1)

    def action_start_bot(self): start_bot(); self.notify("Bot starting…", severity="information")
    def action_stop_bot(self):  stop_bot();  self.notify("Bot stopped.",   severity="warning")


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=background_updater, daemon=True).start()
    time.sleep(0.4)
    BTCKillerApp().run()
