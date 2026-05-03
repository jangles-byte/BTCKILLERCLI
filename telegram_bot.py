"""
BTC.KILLER — Telegram Bot
Full settings control from your phone.

Commands:
  /start        — Main menu
  /status       — Bot status
  /position     — Open position
  /pnl          — P&L summary
  /market       — Market info
  /startbot     — Start trading
  /stopbot      — Stop trading
  /abort        — Sell position
  /settings     — All settings
  /help         — Command list
"""

import time, json, requests, threading
from pathlib import Path

CFG_PATH = Path(__file__).resolve().parent / "bot_config.json"


class TelegramBot:
    def __init__(self, token, allowed_users, shared_state, state_lock):
        self.token   = token
        self.allowed = set(str(u) for u in allowed_users)
        self.state   = shared_state
        self.lock    = state_lock
        self.base    = f"https://api.telegram.org/bot{token}"
        self.offset  = 0
        self.running = True
        self.dash    = "http://localhost:5050"
        # pending_input[chat_id] = setting key waiting for a value
        self.pending = {}

    # ── Telegram helpers ───────────────────────────────────────────────
    def _send(self, chat_id, text, markup=None, parse_mode="Markdown"):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if markup:
            payload["reply_markup"] = json.dumps(markup)
        try:
            requests.post(f"{self.base}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            print(f"[Telegram] send error: {e}")

    def _answer(self, cq_id, text=""):
        try:
            requests.post(f"{self.base}/answerCallbackQuery",
                          json={"callback_query_id": cq_id, "text": text}, timeout=5)
        except Exception:
            pass

    def _get_updates(self):
        try:
            r = requests.get(f"{self.base}/getUpdates",
                             params={"timeout": 25, "offset": self.offset}, timeout=30)
            return r.json().get("result", [])
        except Exception as e:
            print(f"[Telegram] poll error: {e}")
            return []

    def _auth(self, user_id):
        return not self.allowed or str(user_id) in self.allowed

    def _api(self, endpoint, method="GET", data=None):
        try:
            url = f"{self.dash}{endpoint}"
            r = requests.post(url, json=data or {}, timeout=5) if method == "POST" \
                else requests.get(url, timeout=5)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def _cfg(self):
        try:
            return json.loads(CFG_PATH.read_text())
        except Exception:
            return {}

    def _save_cfg(self, updates):
        """Merge updates into bot_config.json and push to dashboard."""
        cfg = self._cfg()
        cfg.update(updates)
        CFG_PATH.write_text(json.dumps(cfg, indent=2))
        self._api("/api/settings", "POST", updates)

    # ── Menus ──────────────────────────────────────────────────────────
    def _main_menu(self):
        return {"inline_keyboard": [
            [{"text": "📊 Status",        "callback_data": "status"},
             {"text": "📍 Position",       "callback_data": "position"}],
            [{"text": "💰 P&L",            "callback_data": "pnl"},
             {"text": "📈 Market",          "callback_data": "market"}],
            [{"text": "▶ Start Bot",        "callback_data": "startbot"},
             {"text": "⏹ Stop Bot",         "callback_data": "stopbot"}],
            [{"text": "⚙️ Settings",        "callback_data": "settings_menu"},
             {"text": "🚨 ABORT Position",  "callback_data": "abort_confirm"}],
        ]}

    def _settings_menu(self):
        return {"inline_keyboard": [
            [{"text": "🤖 Mode",          "callback_data": "set_mode_menu"},
             {"text": "📊 Kelly",          "callback_data": "set_kelly_menu"}],
            [{"text": "💰 Wager",          "callback_data": "set_wager_menu"},
             {"text": "⏱ Trigger",         "callback_data": "set_trigger_menu"}],
            [{"text": "🛡 Risk / Limits",  "callback_data": "set_risk_menu"},
             {"text": "🚪 Entry",           "callback_data": "set_entry_menu"}],
            [{"text": "« Main Menu",        "callback_data": "main_menu"}],
        ]}

    def _mode_menu(self):
        cfg = self._cfg()
        cur = cfg.get("mode", "balanced")
        def star(m): return " ✓" if cur == m else ""
        return {"inline_keyboard": [
            [{"text": f"🧠 Selective{star('selective')}",   "callback_data": "mode_selective"},
             {"text": f"⚖️ Balanced{star('balanced')}",     "callback_data": "mode_balanced"}],
            [{"text": f"🔥 Aggressive{star('aggressive')}", "callback_data": "mode_aggressive"},
             {"text": f"💥 Always Buy{star('always')}",     "callback_data": "mode_always"}],
            [{"text": "« Settings",                         "callback_data": "settings_menu"}],
        ]}

    def _kelly_menu(self):
        cfg = self._cfg()
        on  = cfg.get("kelly_enabled", False)
        frac = float(cfg.get("kelly_fraction", 0.5))
        status = "🟢 ON" if on else "🔴 OFF"
        return {"inline_keyboard": [
            [{"text": f"Kelly: {status}",  "callback_data": "noop"}],
            [{"text": "✅ Turn ON",         "callback_data": "kelly_on"},
             {"text": "❌ Turn OFF",        "callback_data": "kelly_off"}],
            [{"text": f"Fraction: {frac:.2f}x  ← Set →",
                                            "callback_data": "kelly_set_frac"}],
            [{"text": "« Settings",         "callback_data": "settings_menu"}],
        ]}

    def _wager_menu(self):
        cfg  = self._cfg()
        mode = cfg.get("wager_mode", "dollar")
        maxb = float(cfg.get("max_session_wager", 5.0))
        minb = float(cfg.get("min_bet", 0.25))
        pct  = float(cfg.get("wager_pct", 10.0))
        def star(m): return " ✓" if mode == m else ""
        return {"inline_keyboard": [
            [{"text": f"$ Fixed{star('dollar')}",   "callback_data": "wager_mode_dollar"},
             {"text": f"% Balance{star('percent')}", "callback_data": "wager_mode_percent"}],
            [{"text": f"Max bet: ${maxb:.2f}",       "callback_data": "wager_set_max"},
             {"text": f"Min bet: ${minb:.2f}",       "callback_data": "wager_set_min"}],
            [{"text": f"% amount: {pct:.1f}%",       "callback_data": "wager_set_pct"}],
            [{"text": "« Settings",                   "callback_data": "settings_menu"}],
        ]}

    def _trigger_menu(self):
        cfg    = self._cfg()
        method = cfg.get("trigger_method", "ev")
        t_open = float(cfg.get("always_open", 9.0))
        t_close= float(cfg.get("always_close", 0.5))
        t_time = float(cfg.get("trigger_time", 5.0))
        def star(m): return " ✓" if method == m else ""
        return {"inline_keyboard": [
            [{"text": f"📐 EV-based{star('ev')}",       "callback_data": "trigger_ev"},
             {"text": f"📡 Signal{star('signal')}",     "callback_data": "trigger_signal"}],
            [{"text": f"Open window: {t_open:.1f} min",  "callback_data": "trigger_set_open"},
             {"text": f"Close at: {t_close:.1f} min",    "callback_data": "trigger_set_close"}],
            [{"text": f"Trigger time: {t_time:.1f} min", "callback_data": "trigger_set_time"}],
            [{"text": "« Settings",                       "callback_data": "settings_menu"}],
        ]}

    def _risk_menu(self):
        cfg       = self._cfg()
        limit     = float(cfg.get("daily_loss_limit", 50))
        period    = cfg.get("loss_period", "daily")
        hs_on     = bool(cfg.get("hard_stop_enabled", False))
        hs_bal    = float(cfg.get("hard_stop_balance", 20.0))
        def star(p): return " ✓" if period == p else ""
        mode_label = "🔴 Hard Stop ON" if hs_on else "🟢 Daily Loss Limit ON"
        return {"inline_keyboard": [
            [{"text": mode_label,                      "callback_data": "noop"}],
            [{"text": "📉 Daily Loss Limit",           "callback_data": "risk_mode_daily"},
             {"text": "🛑 Hard Stop",                  "callback_data": "risk_mode_hardstop"}],
            [{"text": f"Loss limit: ${limit:.2f}",     "callback_data": "risk_set_limit"},
             {"text": f"Hard stop at: ${hs_bal:.0f}",  "callback_data": "risk_set_hardstop"}],
            [{"text": f"Daily{star('daily')}",         "callback_data": "risk_period_daily"},
             {"text": f"Hourly{star('hourly')}",       "callback_data": "risk_period_hourly"},
             {"text": f"Weekly{star('weekly')}",       "callback_data": "risk_period_weekly"}],
            [{"text": "« Settings",                    "callback_data": "settings_menu"}],
        ]}

    def _entry_menu(self):
        cfg        = self._cfg()
        early      = cfg.get("allow_early_buy", True)
        early_max  = int(float(cfg.get("early_max_price", 0.75)) * 100)
        always_max = int(float(cfg.get("always_max_price", 0.65)) * 100)
        early_icon = "🟢 ON" if early else "🔴 OFF"
        return {"inline_keyboard": [
            [{"text": f"Early buy: {early_icon}",         "callback_data": "noop"}],
            [{"text": "✅ Early buy ON",                    "callback_data": "entry_early_on"},
             {"text": "❌ Early buy OFF",                   "callback_data": "entry_early_off"}],
            [{"text": f"Early max price: {early_max}¢",    "callback_data": "entry_set_early_max"},
             {"text": f"Always max: {always_max}¢",        "callback_data": "entry_set_always_max"}],
            [{"text": "« Settings",                         "callback_data": "settings_menu"}],
        ]}

    def _abort_confirm_menu(self):
        return {"inline_keyboard": [
            [{"text": "✅ YES — Sell now", "callback_data": "abort_execute"},
             {"text": "❌ Cancel",          "callback_data": "main_menu"}],
        ]}

    # ── Text builders ──────────────────────────────────────────────────
    def _status_text(self):
        with self.lock:
            s = dict(self.state)
        running  = s.get("bot_running", False)
        status   = s.get("bot_status", "idle")
        market   = s.get("market_ticker", "—")
        secs     = s.get("secs_remaining") or 0
        yes_ask  = s.get("yes_ask") or 0
        no_ask   = s.get("no_ask") or 0
        conv     = s.get("conviction") or 0
        conv_dir = s.get("conviction_direction") or "—"
        balance  = s.get("balance")
        btc      = s.get("btc_price")
        icon     = "🟢" if running else "🔴"
        label    = "RUNNING" if running else "STOPPED"
        if s.get("killed"):
            icon, label = "⛔", "KILLED (loss limit)"
        return "\n".join([
            f"*BTC.KILLER Status*",
            f"",
            f"{icon} Bot: *{label}*",
            f"💵 BTC: `{'$'+f'{btc:,.2f}' if btc else '—'}`",
            f"💰 Balance: `{'$'+f'{balance:.2f}' if balance else '—'}`",
            f"",
            f"📈 Market: `{market}`",
            f"⏱ Time left: `{secs/60:.1f} min`",
            f"YES ask: `{round(yes_ask*100)}¢`  |  NO ask: `{round(no_ask*100)}¢`",
            f"🎯 Conviction: `{round(conv*100)}% {conv_dir.upper()}`",
            f"🤖 State: `{status.upper()}`",
        ])

    def _position_text(self):
        with self.lock:
            s = dict(self.state)
        pos    = s.get("current_position")
        market = s.get("market_ticker")
        secs   = s.get("secs_remaining") or 0
        if not pos or pos.get("ticker") != market or secs <= 0:
            return "📍 *No open position for current market.*"
        side      = (pos.get("side") or "").upper()
        price_c   = round((pos.get("price") or 0) * 100)
        contracts = pos.get("contracts") or 0
        cost      = pos.get("cost") or 0
        icon      = "🟢" if pos.get("side") == "yes" else "🔴"
        return (f"📍 *Active Position*\n\n"
                f"{icon} Side: `{side}`\n"
                f"📦 Contracts: `{contracts}`\n"
                f"💲 Buy price: `{price_c}¢`\n"
                f"💸 Total cost: `${cost:.2f}`\n"
                f"⏱ Time left: `{secs/60:.1f} min`\n"
                f"Potential win: `${contracts*(1-(pos.get('price') or 0)):.2f}`")

    def _pnl_text(self):
        with self.lock:
            s = dict(self.state)
        pnl    = s.get("pnl_today", 0)
        wins   = s.get("wins_today", 0)
        losses = s.get("losses_today", 0)
        pf     = s.get("profit_factor", 0)
        ev     = s.get("expected_val", 0)
        period = s.get("pnl_period", "day")
        sign   = "+" if pnl >= 0 else ""
        icon   = "💚" if pnl > 0 else "🔴" if pnl < 0 else "⬜"
        return (f"💰 *P&L — {period.upper()}*\n\n"
                f"{icon} P&L: `{sign}${abs(pnl):.2f}`\n"
                f"🏆 Win rate: `{wins}W / {losses}L` "
                f"({round(wins/(wins+losses)*100) if wins+losses else 0}%)\n"
                f"📊 Profit factor: `{pf:.2f}x`\n"
                f"🎯 Avg EV/trade: `{sign}${abs(ev):.2f}`")

    def _market_text(self):
        with self.lock:
            s = dict(self.state)
        return (f"📈 *Current Market*\n\n"
                f"Ticker: `{s.get('market_ticker','—')}`\n"
                f"⏱ Time left: `{(s.get('secs_remaining') or 0)/60:.1f} min`\n"
                f"🎯 Target: `{'$'+str(int(s['target_price'])) if s.get('target_price') else '—'}` "
                f"({s.get('target_dir','—')})\n"
                f"💵 BTC: `{('$'+'{:,.2f}'.format(s['btc_price'])) if s.get('btc_price') else '—'}`\n\n"
                f"YES ask: `{round((s.get('yes_ask') or 0)*100)}¢`  "
                f"EV: `{(s.get('yes_ev') or 0)*100:+.1f}¢`\n"
                f"NO ask:  `{round((s.get('no_ask') or 0)*100)}¢`  "
                f"EV: `{(s.get('no_ev') or 0)*100:+.1f}¢`")

    def _settings_text(self):
        cfg = self._cfg()
        mode       = cfg.get("mode", "balanced").upper()
        wm         = cfg.get("wager_mode", "dollar")
        maxb       = float(cfg.get("max_session_wager", 5.0))
        minb       = float(cfg.get("min_bet", 0.25))
        kelly_on   = cfg.get("kelly_enabled", False)
        kelly_frac = float(cfg.get("kelly_fraction", 0.5))
        loss_lim   = float(cfg.get("daily_loss_limit", 50))
        loss_per   = cfg.get("loss_period", "daily")
        t_method   = cfg.get("trigger_method", "ev")
        t_open     = float(cfg.get("always_open", 9.0))
        t_close    = float(cfg.get("always_close", 0.5))
        early      = cfg.get("allow_early_buy", True)
        early_max  = int(float(cfg.get("early_max_price", 0.75)) * 100)
        always_max = int(float(cfg.get("always_max_price", 0.65)) * 100)
        wager_str  = f"${maxb:.2f} max / ${minb:.2f} min ({wm})"
        kelly_str  = f"ON — {kelly_frac:.2f}x fraction" if kelly_on else "OFF"
        return (f"⚙️ *All Settings*\n\n"
                f"*Mode:* `{mode}`\n"
                f"*Wager:* `{wager_str}`\n"
                f"*Kelly:* `{kelly_str}`\n"
                f"*Trigger:* `{t_method.upper()}` | window `{t_open:.1f}→{t_close:.1f} min`\n"
                f"*Loss mode:* `{'Hard Stop @ $'+str(int(cfg.get('hard_stop_balance',20))) if cfg.get('hard_stop_enabled') else 'Daily Loss $'+str(int(loss_lim))+' ('+loss_per+')'}`\n"
                f"*Early buy:* `{'ON' if early else 'OFF'}` — max `{early_max}¢`\n"
                f"*Always max price:* `{always_max}¢`")

    # ── Command handlers ───────────────────────────────────────────────
    def handle_command(self, chat_id, text):
        cmd = text.lower().split()[0].lstrip('/')
        if cmd in ("start", "menu"):
            self._send(chat_id, "🎰 *BTC.KILLER* — Choose an action:", self._main_menu())
        elif cmd == "status":
            self._send(chat_id, self._status_text(), self._main_menu())
        elif cmd == "position":
            self._send(chat_id, self._position_text(), self._main_menu())
        elif cmd == "pnl":
            self._send(chat_id, self._pnl_text(), self._main_menu())
        elif cmd == "market":
            self._send(chat_id, self._market_text(), self._main_menu())
        elif cmd == "startbot":
            self.state["_bot_control"] = "start"
            self._send(chat_id, "▶️ *Bot started!*", self._main_menu())
        elif cmd == "stopbot":
            self.state["_bot_control"] = "stop"
            self._send(chat_id, "⏹ *Bot stopped.*", self._main_menu())
        elif cmd == "abort":
            self._send(chat_id, "⚠️ *Sell current position?*", self._abort_confirm_menu())
        elif cmd == "settings":
            self._send(chat_id, self._settings_text(), self._settings_menu())
        elif cmd == "help":
            self._send(chat_id,
                "*Commands*\n\n"
                "/status — Bot status\n/position — Open position\n/pnl — P&L\n"
                "/market — Market info\n/startbot — Start bot\n/stopbot — Stop bot\n"
                "/abort — Sell position\n/settings — All settings\n/help — This list")
        else:
            self._send(chat_id, "Unknown command. Try /help", self._main_menu())

    # ── Text input handler (for pending setting values) ────────────────
    def handle_text_input(self, chat_id, text):
        key = self.pending.pop(chat_id, None)
        if not key:
            self._send(chat_id, "🎰 *BTC.KILLER*", self._main_menu())
            return
        try:
            val = float(text.strip().replace('$','').replace('%','').replace('¢',''))
        except ValueError:
            self._send(chat_id, "❌ Invalid number. Try again.", self._settings_menu())
            return

        label_map = {
            "max_session_wager": ("Max bet",          "$",  self._wager_menu),
            "min_bet":           ("Min bet",           "$",  self._wager_menu),
            "wager_pct":         ("Wager %",           "%",  self._wager_menu),
            "kelly_fraction":    ("Kelly fraction",    "x",  self._kelly_menu),
            "daily_loss_limit":  ("Loss limit",        "$",  self._risk_menu),
            "hard_stop_balance": ("Hard stop floor",   "$",  self._risk_menu),
            "always_open":       ("Open window",       " min", self._trigger_menu),
            "always_close":      ("Close window",      " min", self._trigger_menu),
            "trigger_time":      ("Trigger time",      " min", self._trigger_menu),
            "early_max_price":   ("Early max price",   "¢",  self._entry_menu),
            "always_max_price":  ("Always max price",  "¢",  self._entry_menu),
        }
        label, unit, back_menu = label_map.get(key, (key, "", self._settings_menu))

        # Prices stored as fractions (0.65), entered as cents (65)
        store_val = val / 100 if unit == "¢" else val
        self._save_cfg({key: store_val})
        display = f"{val:.0f}¢" if unit == "¢" else f"{val:.2f}{unit}"
        self._send(chat_id, f"✅ *{label}* set to `{display}`", back_menu())

    # ── Callback handler ───────────────────────────────────────────────
    def handle_callback(self, chat_id, cq_id, data):
        self._answer(cq_id)

        # Clear pending input if they pressed a button instead of typing
        self.pending.pop(chat_id, None)

        if data == "noop":
            return
        elif data == "main_menu":
            self._send(chat_id, "🎰 *BTC.KILLER*", self._main_menu())
        elif data == "status":
            self._send(chat_id, self._status_text(), self._main_menu())
        elif data == "position":
            self._send(chat_id, self._position_text(), self._main_menu())
        elif data == "pnl":
            self._send(chat_id, self._pnl_text(), self._main_menu())
        elif data == "market":
            self._send(chat_id, self._market_text(), self._main_menu())
        elif data == "startbot":
            self.state["_bot_control"] = "start"
            self._send(chat_id, "▶️ *Bot started!*", self._main_menu())
        elif data == "stopbot":
            self.state["_bot_control"] = "stop"
            self._send(chat_id, "⏹ *Bot stopped.*", self._main_menu())
        elif data == "abort_confirm":
            self._send(chat_id, "⚠️ *Sell current position?*", self._abort_confirm_menu())
        elif data == "abort_execute":
            r = self._api("/api/abort", "POST")
            msg = "🚨 *Position sold!*" if r.get("ok") else f"❌ Abort failed: {r.get('error','?')}"
            self._send(chat_id, msg, self._main_menu())

        # ── Settings root ──────────────────────────────────────────────
        elif data == "settings_menu":
            self._send(chat_id, self._settings_text(), self._settings_menu())

        # ── Mode ──────────────────────────────────────────────────────
        elif data == "set_mode_menu":
            self._send(chat_id, "🤖 *Trading Mode*", self._mode_menu())
        elif data.startswith("mode_"):
            mode = data[5:]
            self._save_cfg({"mode": mode})
            self._send(chat_id, f"✅ Mode → *{mode.upper()}*", self._mode_menu())

        # ── Kelly ─────────────────────────────────────────────────────
        elif data == "set_kelly_menu":
            self._send(chat_id, "📊 *Kelly Criterion Sizing*", self._kelly_menu())
        elif data == "kelly_on":
            self._save_cfg({"kelly_enabled": True})
            self._send(chat_id, "✅ Kelly sizing *ON*", self._kelly_menu())
        elif data == "kelly_off":
            self._save_cfg({"kelly_enabled": False})
            self._send(chat_id, "❌ Kelly sizing *OFF*", self._kelly_menu())
        elif data == "kelly_set_frac":
            self.pending[chat_id] = "kelly_fraction"
            cfg = self._cfg()
            cur = float(cfg.get("kelly_fraction", 0.5))
            self._send(chat_id,
                f"📊 *Set Kelly Fraction*\nCurrent: `{cur:.2f}x`\n\n"
                f"Enter a value between `0.05` and `1.0`\n"
                f"_(0.5 = half-Kelly, 1.0 = full Kelly)_")

        # ── Wager ─────────────────────────────────────────────────────
        elif data == "set_wager_menu":
            self._send(chat_id, "💰 *Wager Settings*", self._wager_menu())
        elif data == "wager_mode_dollar":
            self._save_cfg({"wager_mode": "dollar"})
            self._send(chat_id, "✅ Wager mode → *$ Fixed*", self._wager_menu())
        elif data == "wager_mode_percent":
            self._save_cfg({"wager_mode": "percent"})
            self._send(chat_id, "✅ Wager mode → *% Balance*", self._wager_menu())
        elif data == "wager_set_max":
            self.pending[chat_id] = "max_session_wager"
            cfg = self._cfg()
            self._send(chat_id,
                f"💰 *Set Max Bet*\nCurrent: `${float(cfg.get('max_session_wager',5)):.2f}`\n\nEnter dollar amount:")
        elif data == "wager_set_min":
            self.pending[chat_id] = "min_bet"
            cfg = self._cfg()
            self._send(chat_id,
                f"💰 *Set Min Bet*\nCurrent: `${float(cfg.get('min_bet',0.25)):.2f}`\n\nEnter dollar amount:")
        elif data == "wager_set_pct":
            self.pending[chat_id] = "wager_pct"
            cfg = self._cfg()
            self._send(chat_id,
                f"💰 *Set Wager %*\nCurrent: `{float(cfg.get('wager_pct',10)):.1f}%`\n\nEnter percentage (e.g. 10):")

        # ── Trigger ───────────────────────────────────────────────────
        elif data == "set_trigger_menu":
            self._send(chat_id, "⏱ *Trigger Settings*", self._trigger_menu())
        elif data == "trigger_ev":
            self._save_cfg({"trigger_method": "ev"})
            self._send(chat_id, "✅ Trigger → *EV-based*", self._trigger_menu())
        elif data == "trigger_signal":
            self._save_cfg({"trigger_method": "signal"})
            self._send(chat_id, "✅ Trigger → *Signal*", self._trigger_menu())
        elif data == "trigger_set_open":
            self.pending[chat_id] = "always_open"
            cfg = self._cfg()
            self._send(chat_id,
                f"⏱ *Set Open Window*\nCurrent: `{float(cfg.get('always_open',9)):.1f} min`\n\nEnter minutes before close to open window:")
        elif data == "trigger_set_close":
            self.pending[chat_id] = "always_close"
            cfg = self._cfg()
            self._send(chat_id,
                f"⏱ *Set Close Window*\nCurrent: `{float(cfg.get('always_close',0.5)):.1f} min`\n\nEnter minutes before close to stop entering:")
        elif data == "trigger_set_time":
            self.pending[chat_id] = "trigger_time"
            cfg = self._cfg()
            self._send(chat_id,
                f"⏱ *Set Trigger Time*\nCurrent: `{float(cfg.get('trigger_time',5)):.1f} min`\n\nEnter minutes:")

        # ── Risk ──────────────────────────────────────────────────────
        elif data == "set_risk_menu":
            self._send(chat_id, "🛡 *Risk & Limits*", self._risk_menu())
        elif data == "risk_set_limit":
            self.pending[chat_id] = "daily_loss_limit"
            cfg = self._cfg()
            self._send(chat_id,
                f"🛡 *Set Loss Limit*\nCurrent: `${float(cfg.get('daily_loss_limit',50)):.2f}`\n\nEnter dollar amount:")
        elif data.startswith("risk_period_"):
            period = data[12:]
            self._save_cfg({"loss_period": period})
            self._send(chat_id, f"✅ Loss period → *{period.upper()}*", self._risk_menu())

        # ── Entry ─────────────────────────────────────────────────────
        elif data == "set_entry_menu":
            self._send(chat_id, "🚪 *Entry Settings*", self._entry_menu())
        elif data == "entry_early_on":
            self._save_cfg({"allow_early_buy": True})
            self._send(chat_id, "✅ Early buy *ON*", self._entry_menu())
        elif data == "entry_early_off":
            self._save_cfg({"allow_early_buy": False})
            self._send(chat_id, "❌ Early buy *OFF*", self._entry_menu())
        elif data == "entry_set_early_max":
            self.pending[chat_id] = "early_max_price"
            cfg = self._cfg()
            cur = int(float(cfg.get("early_max_price", 0.75)) * 100)
            self._send(chat_id,
                f"🚪 *Set Early Max Price*\nCurrent: `{cur}¢`\n\nEnter price in cents (e.g. 75):")
        elif data == "entry_set_always_max":
            self.pending[chat_id] = "always_max_price"
            cfg = self._cfg()
            cur = int(float(cfg.get("always_max_price", 0.65)) * 100)
            self._send(chat_id,
                f"🚪 *Set Always Max Price*\nCurrent: `{cur}¢`\n\nEnter price in cents (e.g. 65):")

        else:
            self._send(chat_id, "🎰 *BTC.KILLER*", self._main_menu())

    # ── Main polling loop ──────────────────────────────────────────────
    def run(self):
        print("[Telegram] Bot polling...")
        while self.running:
            try:
                for update in self._get_updates():
                    self.offset = update["update_id"] + 1

                    if "message" in update:
                        msg     = update["message"]
                        user_id = msg.get("from", {}).get("id")
                        chat_id = msg.get("chat", {}).get("id")
                        text    = msg.get("text", "")
                        if not self._auth(user_id):
                            self._send(chat_id, "⛔ Not authorized.")
                            continue
                        if text.startswith("/"):
                            self.handle_command(chat_id, text[1:])
                        else:
                            self.handle_text_input(chat_id, text)

                    elif "callback_query" in update:
                        cq      = update["callback_query"]
                        user_id = cq.get("from", {}).get("id")
                        chat_id = cq.get("message", {}).get("chat", {}).get("id")
                        data    = cq.get("data", "")
                        cq_id   = cq.get("id")
                        if not self._auth(user_id):
                            self._answer(cq_id, "⛔ Not authorized")
                            continue
                        self.handle_callback(chat_id, cq_id, data)

            except Exception as e:
                print(f"[Telegram] Loop error: {e}")
                time.sleep(5)
            time.sleep(0.5)


def run_telegram_bot(token, allowed_users, shared_state, state_lock):
    TelegramBot(token, allowed_users, shared_state, state_lock).run()


if __name__ == "__main__":
    import sys
    if not CFG_PATH.exists():
        print("No bot_config.json found.")
        sys.exit(1)
    cfg   = json.loads(CFG_PATH.read_text())
    token = cfg.get("telegram_token")
    users = cfg.get("telegram_allowed_users", [])
    if not token:
        print("No telegram_token in bot_config.json")
        sys.exit(1)
    run_telegram_bot(token, users, {}, threading.Lock())
