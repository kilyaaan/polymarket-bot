"""Logging — structured logging, CSV trade writer, Telegram & Discord notifier."""

from __future__ import annotations

import csv
import logging
import queue
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from pulse.config import TG_TOKEN, TG_CHAT_ID, DISCORD_WEBHOOK, TRADES_CSV, DEFAULT_TAKER_FEE_RATE

if TYPE_CHECKING:
    from pulse.config import Position, SessionStats

log = logging.getLogger("pulse")


def setup_logging(level: str = "INFO"):
    """Configure root logger with console + rotating file output."""
    fmt = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)
    fh = RotatingFileHandler("pulse.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)


# ── Notification levels ───────────────────────────────────────────────────────
_LEVELS: dict[str, dict] = {
    "STARTUP": {"emoji": "🚀", "color": 0xF7931A},
    "SNIPE":   {"emoji": "🎯", "color": 0xF7931A},
    "WIN":     {"emoji": "✅", "color": 0x00FF88},
    "LOSS":    {"emoji": "❌", "color": 0xFF4444},
    "HOLD":    {"emoji": "🔒", "color": 0x00AAFF},
    "CIRCUIT": {"emoji": "🛑", "color": 0xFF0000},
    "RECAP":   {"emoji": "📊", "color": 0x5865F2},
    "INFO":    {"emoji": "ℹ️",  "color": 0x888888},
}

# Rate limiting: minimum seconds between notifications per level (0 = unlimited)
_COOLDOWNS: dict[str, float] = {
    "SNIPE": 3.0,
    "HOLD":  10.0,
    "INFO":  0.0,
}
_last_sent: dict[str, float] = {}
_rate_lock = threading.Lock()


def _rate_ok(level: str) -> bool:
    cd = _COOLDOWNS.get(level, 0.0)
    if cd == 0.0:
        return True
    with _rate_lock:
        now = time.time()
        if now - _last_sent.get(level, 0.0) < cd:
            return False
        _last_sent[level] = now
        return True


# ── Shared HTTP session ───────────────────────────────────────────────────────
_notify_session = requests.Session()


# ── Formatters ────────────────────────────────────────────────────────────────
def _fmt_telegram(level: str, title: str, fields: dict) -> str:
    meta = _LEVELS.get(level, _LEVELS["INFO"])
    lines = [f"{meta['emoji']} <b>{title}</b>"]
    for k, v in fields.items():
        lines.append(f"<b>{k}</b>: <code>{v}</code>")
    return "\n".join(lines)


def _fmt_discord_embed(level: str, title: str, fields: dict) -> dict:
    meta = _LEVELS.get(level, _LEVELS["INFO"])
    embed_fields = [
        {"name": k, "value": str(v), "inline": True}
        for k, v in fields.items()
    ]
    return {
        "embeds": [{
            "title": f"{meta['emoji']} {title}",
            "color": meta["color"],
            "fields": embed_fields,
        }]
    }


# ── Senders ───────────────────────────────────────────────────────────────────
def _send_telegram(level: str, title: str, fields: dict):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    text = _fmt_telegram(level, title, fields)
    r = _notify_session.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=5,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Telegram {r.status_code}")


def _send_discord(level: str, title: str, fields: dict):
    if not DISCORD_WEBHOOK:
        return
    payload = _fmt_discord_embed(level, title, fields)
    r = _notify_session.post(DISCORD_WEBHOOK, json=payload, timeout=5)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord {r.status_code}")


# ── Async queue + retry worker ────────────────────────────────────────────────
_notify_queue: queue.Queue = queue.Queue(maxsize=100)


def _notify_worker():
    from pulse.config import SHUTDOWN_EVENT
    while not SHUTDOWN_EVENT.is_set() or not _notify_queue.empty():
        try:
            item = _notify_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        level, title, fields = item
        for attempt in range(3):
            try:
                _send_telegram(level, title, fields)
                _send_discord(level, title, fields)
                break
            except Exception as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    log.warning("Notify failed after 3 attempts (%s): %s", level, exc)
        _notify_queue.task_done()


_worker_thread = threading.Thread(target=_notify_worker, name="notify_worker", daemon=True)
_worker_thread.start()


# ── Public API ────────────────────────────────────────────────────────────────
def notify(level: str, title: str, **fields):
    """Send a structured notification to Telegram and/or Discord."""
    if not _rate_ok(level):
        return
    if not TG_TOKEN and not DISCORD_WEBHOOK:
        return
    try:
        _notify_queue.put_nowait((level, title, fields))
    except queue.Full:
        log.warning("Notify queue full, dropping %s", level)


def tg(msg: str, level: str = "INFO"):
    """Legacy plain-text notification (backward compat). Prefer notify()."""
    notify(level, msg)


# ── Recap ─────────────────────────────────────────────────────────────────────
def notify_recap(stats: SessionStats):
    """Send a session summary notification."""
    # Use stats.total / wins / losses — updated in main.py after every log_trade
    total = stats.total
    wr = f"{stats.win_rate:.1f}%" if total else "N/A"
    pnl_sign = "+" if stats.total_pnl >= 0 else ""
    notify(
        "RECAP",
        "Récap session",
        **{
            "Trades": str(total),
            "Winrate": wr,
            "P&L net": f"{pnl_sign}{stats.total_pnl:.2f} USDC",
            "Durée": stats.elapsed,
            "Snipes": str(stats.snipes),
            "Spikes vus": str(stats.spikes_seen),
        },
    )


# ── CSV trade log ─────────────────────────────────────────────────────────────
_CSV_COLUMNS = [
    "timestamp", "direction", "entry", "exit", "shares_held",
    "size_usdc", "kelly_used", "pnl_gross", "pnl_net", "pnl_pct",
    "fees", "duration_min", "btc_entry", "btc_exit",
    "score", "mom15_entry", "rsi_entry", "min_score",
    "reason", "held_expiry", "order_id", "close_order_id", "close_fill",
    # Extended analytics
    "token_peak", "token_trough",
    "mom30_entry", "mom60_entry", "mom15_exit", "rsi_exit",
    "window_delta", "btc_move_pct",
    "spread_entry", "sl_price", "market_remaining_min",
    "hour_of_day", "day_of_week",
    "spike_size", "ob_depth_entry", "session_pnl_before",
]


def init_csv(path: Path | None = None):
    """Create CSV with headers if it doesn't exist."""
    p = path or TRADES_CSV
    if not p.exists():
        with open(p, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_CSV_COLUMNS)
        log.info("Created trade log: %s", p)


def fee_usdc(size: float, rate: float) -> float:
    return round(size * float(rate), 4)


def log_trade(
    pos: Position,
    exit_price: float,
    reason: str,
    stats: SessionStats,
    btc_current: float,
    min_score: float,
    mom15_exit: float = 0.0,
    rsi_exit: float = 50.0,
    path: Path | None = None,
) -> float:
    """Log a closed trade to CSV. Returns net PnL."""
    pnl_gross = (exit_price - pos.entry_price) * pos.shares_held
    fee_buy = fee_usdc(pos.size_usdc, pos.fee_rate_used)
    # Hold-to-expiry: automatic resolution = no SELL fee
    if pos.holding_expiry and reason == "EXPIRY":
        fee_sell = 0.0
    else:
        fee_sell = fee_usdc(exit_price * pos.shares_held, pos.fee_rate_used)
    fees = fee_buy + fee_sell
    pnl_net = pnl_gross - fees
    pct = pnl_net / max(pos.size_usdc, 0.001) * 100

    btc_move_pct = round(
        (btc_current - pos.entry_crypto) / max(pos.entry_crypto, 1.0) * 100, 4
    )
    _now = datetime.now()

    p = path or TRADES_CSV
    with open(p, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            _now.isoformat(),
            pos.direction,
            round(pos.entry_price, 4), round(exit_price, 4),
            round(pos.shares_held, 4),
            round(pos.size_usdc, 2),
            round(pos.kelly_used, 2),
            round(pnl_gross, 4), round(pnl_net, 4), round(pct, 2),
            round(fees, 4),
            round(pos.elapsed_min, 1),
            round(pos.entry_crypto, 4), round(btc_current, 4),
            round(pos.score, 3), round(pos.mom15_at_entry, 4),
            round(pos.rsi_at_entry, 1),
            round(min_score, 2),
            reason, pos.holding_expiry,
            pos.order_id, pos.close_order_id, pos.close_fill,
            # Extended analytics
            round(pos.peak_price, 4), round(pos.trough_price, 4),
            round(pos.mom30_at_entry, 4), round(pos.mom60_at_entry, 4),
            round(mom15_exit, 4), round(rsi_exit, 1),
            round(pos.window_delta_at_entry, 4), btc_move_pct,
            round(pos.spread_at_entry, 4),
            round(pos.trail_sl, 4),
            round(pos.market_remaining_min_at_entry, 2),
            _now.hour, _now.weekday(),
            round(pos.spike_size_at_entry, 6),
            round(pos.ob_depth_at_entry, 2),
            round(pos.session_pnl_before, 4),
        ])

    # Update stats
    stats.btc_pnl += pnl_net
    if pnl_net >= 0:
        stats.btc_w += 1
    else:
        stats.btc_l += 1

    log.info(
        "TRADE %s %s pnl=%.2f$ (%.1f%%) reason=%s held_expiry=%s",
        pos.direction, "WIN" if pnl_net >= 0 else "LOSS",
        pnl_net, pct, reason, pos.holding_expiry,
    )
    return pnl_net
