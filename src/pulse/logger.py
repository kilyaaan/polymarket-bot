"""Logging — structured logging, CSV trade writer, Telegram notifier."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from pulse.config import TG_TOKEN, TG_CHAT_ID, TRADES_CSV, DEFAULT_TAKER_FEE_RATE

if TYPE_CHECKING:
    from pulse.config import Position, SessionStats

log = logging.getLogger("pulse")


def setup_logging(level: str = "INFO"):
    """Configure root logger — file only, no StreamHandler.

    Logging to stdout/stderr is intentionally disabled: the Rich Live TUI
    owns the terminal and any direct writes corrupt the display.
    All output goes to pulse.log instead.
    """
    fmt = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove ALL existing handlers (basicConfig / any prior call)
    root.handlers.clear()

    fh = RotatingFileHandler("pulse.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)


# ── Telegram ─────────────────────────────────────────────────────────────────
_tg_session = requests.Session()


def tg(msg: str):
    """Send a Telegram alert. Failures are logged, never swallowed."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = _tg_session.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=5,
        )
        if r.status_code != 200:
            log.warning("Telegram send failed: %d", r.status_code)
    except requests.RequestException as e:
        log.warning("Telegram error: %s", e)


# ── CSV trade log ────────────────────────────────────────────────────────────
_CSV_COLUMNS = [
    "timestamp", "direction", "entry", "exit", "shares_held",
    "size_usdc", "kelly_used", "pnl_gross", "pnl_net", "pnl_pct",
    "fees", "duration_min", "btc_entry", "btc_exit",
    "score", "mom15_entry", "rsi_entry", "min_score",
    "reason", "held_expiry", "order_id", "close_order_id", "close_fill",
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

    p = path or TRADES_CSV
    with open(p, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now().isoformat(),
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
