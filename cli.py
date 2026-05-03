#!/usr/bin/env python3
"""
BTC.KILLER — Terminal Dashboard
Runs standalone — replaces the web dashboard entirely.

Install deps (once):
    venv/bin/python3 -m pip install textual

Run:
    venv/bin/python3 cli.py
    — or double-click  3_START_CLI.command  (created alongside this file)
"""

from __future__ import annotations
import csv
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, date
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────
BOT_DIR    = Path(__file__).resolve().parent
CONFIG     = BOT_DIR / "bot_config.json"
POS_FILE   = BOT_DIR / "current_position.json"
STATUS_FILE= BOT_DIR / "bot_status.json"
TRADES_DIR = BOT_DIR / "trades"

# ── ASCII banner ───────────────────────────────────────────────────────────
ASCII_BANNER = r"""

__╱╲╲╲╲╲╲╲╲╲╲╲╲╲____╱╲╲╲╲╲╲╲╲╲╲╲╲╲╲╲________╱╲╲╲╲╲╲╲╲╲____________╱╲╲╲________╱╲╲╲________╱╲╲╲╲╲╲_____╱╲╲╲╲╲╲_________________________________
 _╲╱╲╲╲╱╱╱╱╱╱╱╱╱╲╲╲_╲╱╱╱╱╱╱╱╲╲╲╱╱╱╱╱______╱╲╲╲╱╱╱╱╱╱╱╱____________╲╱╲╲╲_____╱╲╲╲╱╱________╲╱╱╱╱╲╲╲____╲╱╱╱╱╲╲╲_________________________________
  _╲╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲_________╱╲╲╲╱_____________________╲╱╲╲╲__╱╲╲╲╱╱______╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲_________________________________
   _╲╱╲╲╲╲╲╲╲╲╲╲╲╲╲╲________╲╱╲╲╲________╱╲╲╲_______________________╲╱╲╲╲╲╲╲╱╱╲╲╲_____╲╱╱╱_____╲╱╲╲╲_______╲╱╲╲╲________╱╲╲╲╲╲╲╲╲___╱╲╲╱╲╲╲╲╲╲╲__
    _╲╱╲╲╲╱╱╱╱╱╱╱╱╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲_______________________╲╱╲╲╲╱╱_╲╱╱╲╲╲_____╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲______╱╲╲╲╱╱╱╱╱╲╲╲_╲╱╲╲╲╱╱╱╱╱╲╲╲_
     _╲╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲_______╲╱╱╲╲╲______________________╲╱╲╲╲____╲╱╱╲╲╲___╲╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲_____╱╲╲╲╲╲╲╲╲╲╲╲__╲╱╲╲╲___╲╱╱╱__
      _╲╱╲╲╲_______╲╱╲╲╲_______╲╱╲╲╲________╲╱╱╱╲╲╲____________________╲╱╲╲╲_____╲╱╱╲╲╲__╲╱╲╲╲____╲╱╲╲╲_______╲╱╲╲╲____╲╱╱╲╲╱╱╱╱╱╱╱___╲╱╲╲╲_________
       _╲╱╲╲╲╲╲╲╲╲╲╲╲╲╲╱________╲╱╲╲╲__________╲╱╱╱╱╲╲╲╲╲╲╲╲╲___________╲╱╲╲╲______╲╱╱╲╲╲_╲╱╲╲╲__╱╲╲╲╲╲╲╲╲╲__╱╲╲╲╲╲╲╲╲╲__╲╱╱╲╲╲╲╲╲╲╲╲╲_╲╱╲╲╲_________
        _╲╱╱╱╱╱╱╱╱╱╱╱╱╱__________╲╱╱╱______________╲╱╱╱╱╱╱╱╱╱____________╲╱╱╱________╲╱╱╱__╲╱╱╱__╲╱╱╱╱╱╱╱╱╱__╲╱╱╱╱╱╱╱╱╱____╲╱╱╱╱╱╱╱╱╱╱__╲╱╱╱__________
""".strip("\n")

# ── shared state ───────────────────────────────────────────────────────────
bot_process: subprocess.Popen | None = None
bot_log_buffer: deque[str] = deque(maxlen=300)
state_lock = threading.Lock()
app_state: dict = {
    "btc_price":    None,
    "balance":      None,
    "bot_running":  False,
    "market_ticker": None,
    "secs_remaining": None,
    "yes_ask": None,
    "no_ask":  None,
    "target_price": None,
    "target_dir":   None,
    "our_yes_prob": 0.5,
    "our_no_prob":  0.5,
    "conviction":   0.0,
    "signals":      [],
    "position":     None,
}

# ── helpers ────────────────────────────────────────────────────────────────
def _stream_bot_output(proc: subprocess.Popen) -> None:
    try:
        for line in iter(proc.stdout.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                bot_log_buffer.append(text)
    except Exception:
        pass

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
            return json.loads(POS_FILE.read_text())
    except Exception:
        pass
    return None

def load_trades_today() -> list[dict]:
    """Return today's trades from CSV."""
    today = date.today().isoformat()
    path  = TRADES_DIR / f"trades_{today}.csv"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []

def compute_pnl(trades: list[dict]) -> tuple[float, int, int]:
    """Returns (pnl, wins, losses) from trade list."""
    pnl = wins = losses = 0
    for t in trades:
        raw = t.get("pnl", "pending")
        if raw == "pending":
            continue
        try:
            v = float(raw)
            pnl += v
            if v > 0:
                wins += 1
            else:
                losses += 1
        except Exception:
            pass
    return pnl, wins, losses

def start_bot() -> None:
    global bot_process
    if bot_process and bot_process.poll() is None:
        return
    bot_log_buffer.clear()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    bot_process = subprocess.Popen(
        [sys.executable, "-u", str(BOT_DIR / "bot.py")],
        cwd=str(BOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    threading.Thread(target=_stream_bot_output, args=(bot_process,), daemon=True).start()
    with state_lock:
        app_state["bot_running"] = True

def stop_bot() -> None:
    global bot_process
    if bot_process and bot_process.poll() is None:
        bot_process.terminate()
        bot_process = None
    with state_lock:
        app_state["bot_running"] = False

def background_updater() -> None:
    """Runs in a daemon thread — keeps app_state fresh."""
    last_balance = 0
    last_market  = 0
    last_signal  = 0

    # Lazy import bot helpers (they load .env / keys)
    try:
        from bot import get_balance as _get_balance, find_current_market as _get_market, sign_request
        from signals import start_feed_thread, btc_state, get_signal, get_candles_context
        start_feed_thread()
        _has_kalshi = True
    except Exception as e:
        print(f"[cli] Kalshi import error: {e}")
        _has_kalshi = False

    while True:
        now = time.time()

        # BTC price
        if _has_kalshi:
            with state_lock:
                app_state["btc_price"] = btc_state.get("price")

        # Balance every 30s
        if _has_kalshi and now - last_balance > 30:
            try:
                bal = _get_balance()
                with state_lock:
                    app_state["balance"] = bal
                last_balance = now
            except Exception:
                pass

        # Market every 3s
        if _has_kalshi and now - last_market > 3:
            try:
                market, secs = _get_market()
                if market:
                    strike = market.get("floor_strike") or market.get("cap_strike")
                    stype  = market.get("strike_type", "")
                    tdir   = ("above" if "greater" in stype
                              else "below" if "less" in stype
                              else stype)
                    with state_lock:
                        app_state["market_ticker"]  = market["ticker"]
                        app_state["secs_remaining"] = secs
                        app_state["yes_ask"]        = float(market.get("yes_ask_dollars", 0))
                        app_state["no_ask"]         = float(market.get("no_ask_dollars", 0))
                        app_state["target_price"]   = strike
                        app_state["target_dir"]     = tdir
                last_market = now
            except Exception:
                pass

        # Signals every 3s
        if _has_kalshi and now - last_signal > 3:
            try:
                with state_lock:
                    strike = app_state.get("target_price")
                    stype  = app_state.get("target_dir","")
                    secs   = app_state.get("secs_remaining")
                    ya     = app_state.get("yes_ask", 0.5)
                    na     = app_state.get("no_ask",  0.5)
                if strike:
                    sig = get_signal(
                        strike_price=strike,
                        strike_type=stype,
                        mins_remaining=secs / 60 if secs else None,
                        yes_ask=ya, no_ask=na,
                    )
                    from bot import calc_conviction
                    conv, _, _ = calc_conviction(sig, ya, na)
                    with state_lock:
                        app_state["our_yes_prob"] = sig["our_yes_prob"]
                        app_state["our_no_prob"]  = sig["our_no_prob"]
                        app_state["conviction"]   = conv
                        app_state["signals"]      = sig.get("signals", [])
                last_signal = now
            except Exception:
                pass

        # Position file
        pos = read_position()
        with state_lock:
            app_state["position"] = pos

        # Bot process health
        with state_lock:
            if app_state["bot_running"] and bot_process and bot_process.poll() is not None:
                app_state["bot_running"] = False

        time.sleep(1)


# ── stdout → log buffer redirect ───────────────────────────────────────────
import io

class _BufferWriter(io.TextIOBase):
    """Captures all print() output into bot_log_buffer once Textual is running."""
    def write(self, text: str) -> int:
        stripped = text.rstrip("\n")
        if stripped:
            bot_log_buffer.append(stripped)
        return len(text)
    def flush(self) -> None:
        pass

# ── Textual App ────────────────────────────────────────────────────────────
from textual.app import App, ComposeResult
from textual.widgets import Static, Button, RichLog, Sparkline, Footer
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual import on

CSS = """
Screen {
    background: #080c14;
    layout: vertical;
}

#banner {
    color: #00ff88;
    text-align: center;
    height: auto;
    padding: 0 0 1 0;
    border-bottom: solid #1a2535;
    background: #080c14;
    width: 100%;
}

#main-row {
    height: 1fr;
}

#left {
    width: 30;
    background: #0b0f1a;
    border-right: solid #1a2535;
    padding: 0 1;
}

#right {
    width: 1fr;
    background: #080c14;
    padding: 0 1;
}

#stats {
    height: auto;
    border: solid #1a2535;
    padding: 0 1;
    margin: 0 0 1 0;
    background: #0b0f1a;
}

#position {
    height: auto;
    border: solid #1a2535;
    padding: 0 1;
    margin: 0 0 1 0;
    background: #0b0f1a;
}

#market {
    height: auto;
    border: solid #1a2535;
    padding: 0 1;
    margin: 0 0 1 0;
    background: #0b0f1a;
}

#controls {
    height: 3;
    margin: 0 0 0 0;
}

#start-btn {
    width: 1fr;
    background: #002218;
    color: #00ff88;
    border: solid #00883a;
    margin: 0 1 0 0;
    height: 3;
}

#start-btn:hover {
    background: #00883a;
    color: #000;
}

#stop-btn {
    width: 1fr;
    background: #220010;
    color: #ff3b5c;
    border: solid #882030;
    height: 3;
}

#stop-btn:hover {
    background: #882030;
    color: #fff;
}

#btc-chart {
    height: 10;
    border: solid #1a2535;
    margin: 0 0 1 0;
    color: #00ff88;
    background: #0b0f1a;
}

#signals {
    height: auto;
    border: solid #1a2535;
    padding: 0 1;
    margin: 0 0 1 0;
    background: #0b0f1a;
}

#bot-log {
    border: solid #1a2535;
    background: #0b0f1a;
    height: 1fr;
    scrollbar-size: 1 1;
    scrollbar-color: #1a2535;
}

Footer {
    background: #0b0f1a;
    color: #334455;
}
"""


class BTCKillerCLI(App):
    CSS = CSS

    BINDINGS = [
        ("s", "start_bot",  "Start Bot"),
        ("x", "stop_bot",   "Stop Bot"),
        ("q", "quit",       "Quit"),
    ]

    _btc_history: list[float] = []
    _log_last_line: str = ""
    _log_lines_written: int = 0

    def compose(self) -> ComposeResult:
        yield Static(ASCII_BANNER, id="banner")
        with Horizontal(id="main-row"):
            with Vertical(id="left"):
                yield Static("", id="stats")
                yield Static("", id="position")
                yield Static("", id="market")
                with Horizontal(id="controls"):
                    yield Button("▶ Start", id="start-btn")
                    yield Button("■ Stop",  id="stop-btn")
            with Vertical(id="right"):
                yield Sparkline([], id="btc-chart", summary_function=max)
                yield Static("", id="signals")
                yield RichLog(id="bot-log", highlight=False,
                              markup=True, wrap=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        # Redirect all print() output into the log panel
        sys.stdout = _BufferWriter()
        log = self.query_one("#bot-log", RichLog)
        log.write("[dim]BTC.KILLER CLI — waiting for data...[/dim]")
        self.set_interval(2.0, self._refresh)

    # ── periodic refresh ───────────────────────────────────────────────────
    def _refresh(self) -> None:
        with state_lock:
            s = dict(app_state)

        trades = load_trades_today()
        pnl, wins, losses = compute_pnl(trades)
        total = wins + losses

        # ── stats panel ───────────────────────────────────────────────────
        running    = s.get("bot_running", False)
        status_c   = "#00ff88" if running else "#ff3b5c"
        status_t   = "● RUNNING" if running else "○ STOPPED"
        btc        = s.get("btc_price") or 0
        bal        = s.get("balance") or 0
        pnl_c      = "#00ff88" if pnl >= 0 else "#ff3b5c"
        pnl_s      = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        wr         = f"{wins/total*100:.0f}%" if total else "—"
        conv       = s.get("conviction", 0.0)
        conv_bar   = "█" * int(conv * 10) + "░" * (10 - int(conv * 10))
        conv_c     = "#00ff88" if conv >= 0.6 else "#ffc837" if conv >= 0.4 else "#334455"

        self.query_one("#stats", Static).update(
            f"[bold #ffc837]◈ STATS[/]\n"
            f" Status   [{status_c}]{status_t}[/]\n"
            f" BTC      [bold white]${btc:,.0f}[/]\n"
            f" Balance  [white]${bal:.2f}[/]\n"
            f" P&L      [{pnl_c}]{pnl_s}[/]\n"
            f" Trades   [white]{len(trades)}[/]  W[#00ff88]{wins}[/] L[#ff3b5c]{losses}[/]\n"
            f" Win rate [white]{wr}[/]\n"
            f" Conviction [{conv_c}]{conv_bar}[/] {conv:.2f}"
        )

        # ── position panel ────────────────────────────────────────────────
        pos = s.get("position")
        if pos and pos.get("ticker"):
            side   = pos.get("side", "?").upper()
            side_c = "#00ff88" if side == "YES" else "#ffc837"
            mins   = pos.get("mins_remaining", 0)
            self.query_one("#position", Static).update(
                f"[bold #ffc837]◈ POSITION[/]\n"
                f" [dim]{pos.get('ticker','')[-24:]}[/dim]\n"
                f" Side     [{side_c}]{side}[/]\n"
                f" Entry    [white]${pos.get('entry',0):.3f}[/]\n"
                f" Qty      [white]{pos.get('contracts',0)} contracts[/]\n"
                f" Time left [{'#ff3b5c' if mins < 2 else 'white'}]{mins:.1f} min[/]"
            )
        else:
            self.query_one("#position", Static).update(
                "[bold #ffc837]◈ POSITION[/]\n"
                " [dim]no open position[/dim]"
            )

        # ── market panel ──────────────────────────────────────────────────
        ticker = s.get("market_ticker")
        secs   = s.get("secs_remaining") or 0
        ya     = s.get("yes_ask") or 0
        na     = s.get("no_ask") or 0
        tgt    = s.get("target_price")
        tdir   = s.get("target_dir", "")
        if ticker:
            self.query_one("#market", Static).update(
                f"[bold #ffc837]◈ MARKET[/]\n"
                f" [dim]{ticker[-24:]}[/dim]\n"
                f" Time    [white]{int(secs)//60}m {int(secs)%60:02d}s[/]\n"
                f" Strike  [white]${tgt:,.0f} {tdir}[/]\n"
                f" YES ask [#00ff88]${ya:.3f}[/]  NO ask [#ffc837]${na:.3f}[/]"
            )
        else:
            self.query_one("#market", Static).update(
                "[bold #ffc837]◈ MARKET[/]\n [dim]fetching...[/dim]"
            )

        # ── BTC sparkline ─────────────────────────────────────────────────
        if btc:
            self._btc_history = (self._btc_history + [float(btc)])[-180:]
            chart = self.query_one("#btc-chart", Sparkline)
            chart.data = self._btc_history

        # ── signals panel ─────────────────────────────────────────────────
        raw_sigs = s.get("signals", [])
        yp = s.get("our_yes_prob", 0.5)
        np_ = s.get("our_no_prob", 0.5)
        yes_bar = "█" * int(yp * 14) + "░" * (14 - int(yp * 14))
        no_bar  = "█" * int(np_ * 14) + "░" * (14 - int(np_ * 14))
        yc = "#00ff88" if yp > 0.55 else "#334455"
        nc = "#ffc837" if np_ > 0.55 else "#334455"
        sig_lines = [
            "[bold #ffc837]◈ SIGNALS[/]",
            f" YES [{yc}]{yes_bar}[/] {yp:.2f}",
            f" NO  [{nc}]{no_bar}[/] {np_:.2f}",
        ]
        if raw_sigs:
            for sig in raw_sigs[:4]:
                name = sig.get("name", "?")[:18]
                val  = sig.get("value", 0)
                sc   = "#00ff88" if val > 0.5 else "#ff3b5c" if val < -0.5 else "#334455"
                sig_lines.append(f"  [dim]{name:<18}[/] [{sc}]{val:+.2f}[/]")
        self.query_one("#signals", Static).update("\n".join(sig_lines))

        # ── bot log ───────────────────────────────────────────────────────
        lines = list(bot_log_buffer)
        if lines and (not lines or lines[-1] != self._log_last_line):
            new_lines = lines[self._log_lines_written:]
            if new_lines:
                log = self.query_one("#bot-log", RichLog)
                for l in new_lines:
                    if "FIRE" in l or "✅" in l:
                        log.write(f"[bold #00ff88]{l}[/]")
                    elif "CONFIRMED" in l:
                        log.write(f"[#00ff88]{l}[/]")
                    elif "⚠" in l or "RETRY" in l or "WATCHING" in l or "waiting" in l:
                        log.write(f"[#ffc837]{l}[/]")
                    elif "❌" in l or "ERROR" in l or "KILL" in l:
                        log.write(f"[bold #ff3b5c]{l}[/]")
                    elif "KELLY" in l or "SKIP" in l:
                        log.write(f"[#4a9eff]{l}[/]")
                    else:
                        log.write(f"[dim]{l}[/dim]")
                self._log_lines_written = len(lines)
            if lines:
                self._log_last_line = lines[-1]

    # ── actions ────────────────────────────────────────────────────────────
    def action_start_bot(self) -> None:
        start_bot()
        self.notify("Bot starting…", severity="information")

    def action_stop_bot(self) -> None:
        stop_bot()
        self.notify("Bot stopped.", severity="warning")

    def on_unmount(self) -> None:
        # Restore real stdout when app exits
        sys.stdout = sys.__stdout__

    @on(Button.Pressed, "#start-btn")
    def _on_start(self) -> None:
        self.action_start_bot()

    @on(Button.Pressed, "#stop-btn")
    def _on_stop(self) -> None:
        self.action_stop_bot()


# ── entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Make sure trades dir exists
    TRADES_DIR.mkdir(parents=True, exist_ok=True)

    # Start background data thread
    t = threading.Thread(target=background_updater, daemon=True)
    t.start()

    # Give signals feed a moment to warm up
    time.sleep(0.5)

    BTCKillerCLI().run()
