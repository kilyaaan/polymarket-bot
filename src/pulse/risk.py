"""Risk — circuit breaker, expiring blacklist, position checkpoint (crash recovery)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from pulse.config import (
    POSITIONS_CHECKPOINT, CryptoMarket, Position, DEFAULT_TAKER_FEE_RATE,
)

log = logging.getLogger(__name__)


# ── Expiring blacklist ───────────────────────────────────────────────────────
class ExpiringBlacklist:
    """Thread-safe set with time-to-live per entry."""

    def __init__(self, ttl: float = 360.0):
        self._data: Dict[str, float] = {}
        self._ttl = ttl
        self._lock = threading.Lock()

    def add(self, key: str):
        with self._lock:
            self._data[key] = time.monotonic() + self._ttl

    def __contains__(self, key: str) -> bool:
        with self._lock:
            exp = self._data.get(key, 0.0)
            if time.monotonic() > exp:
                self._data.pop(key, None)
                return False
            return True


# ── Position checkpoint (crash recovery) ─────────────────────────────────────
def _position_to_dict(pos: Position) -> dict:
    """Serialize a Position to a JSON-safe dict."""
    return {
        "token_id": pos.token_id,
        "direction": pos.direction,
        "entry_price": pos.entry_price,
        "size_usdc": pos.size_usdc,
        "shares_held": pos.shares_held,
        "order_id": pos.order_id,
        "entry_time": pos.entry_time,
        "current_price": pos.current_price,
        "entry_crypto": pos.entry_crypto,
        "score": pos.score,
        "peak_price": pos.peak_price,
        "trail_sl": pos.trail_sl,
        "mom15_at_entry": pos.mom15_at_entry,
        "rsi_at_entry": pos.rsi_at_entry,
        "kelly_used": pos.kelly_used,
        "fee_rate_used": pos.fee_rate_used,
        "holding_expiry": pos.holding_expiry,
        # Market fields
        "market_condition_id": pos.market.condition_id,
        "market_question": pos.market.question,
        "market_slug": pos.market.slug,
        "market_yes_token": pos.market.yes_token,
        "market_no_token": pos.market.no_token,
        "market_start_time": pos.market.start_time,
        "market_end_time": pos.market.end_time,
        "market_start_price": pos.market.start_price,
    }


def _dict_to_position(d: dict) -> Position:
    """Reconstruct a Position from a checkpoint dict."""
    market = CryptoMarket(
        condition_id=d["market_condition_id"],
        question=d.get("market_question", ""),
        slug=d.get("market_slug", ""),
        yes_token=d["market_yes_token"],
        no_token=d["market_no_token"],
        start_time=d["market_start_time"],
        end_time=d["market_end_time"],
        start_price=d.get("market_start_price", 0.0),
    )
    return Position(
        market=market,
        token_id=d["token_id"],
        direction=d["direction"],
        entry_price=d["entry_price"],
        size_usdc=d["size_usdc"],
        shares_held=d["shares_held"],
        order_id=d["order_id"],
        entry_time=d.get("entry_time", time.time()),
        current_price=d.get("current_price", 0.0),
        entry_crypto=d.get("entry_crypto", 0.0),
        score=d.get("score", 0.0),
        peak_price=d.get("peak_price", d["entry_price"]),
        trail_sl=d.get("trail_sl", 0.0),
        mom15_at_entry=d.get("mom15_at_entry", 0.0),
        rsi_at_entry=d.get("rsi_at_entry", 50.0),
        kelly_used=d.get("kelly_used", 0.0),
        fee_rate_used=d.get("fee_rate_used", DEFAULT_TAKER_FEE_RATE),
        holding_expiry=d.get("holding_expiry", False),
        verified=False,  # needs reconciliation
    )


def save_checkpoint(positions: List[Position], path: Path | None = None):
    """Atomic write of positions to JSON checkpoint."""
    p = path or POSITIONS_CHECKPOINT
    tmp = p.parent / f".{p.name}.tmp"
    try:
        data = [_position_to_dict(pos) for pos in positions]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(str(tmp), str(p))
        log.debug("Checkpoint saved: %d positions", len(positions))
    except Exception as e:
        log.error("Checkpoint save failed: %s", e)


def load_checkpoint(path: Path | None = None) -> List[Position]:
    """Load positions from checkpoint. Returns empty list if missing or corrupt."""
    p = path or POSITIONS_CHECKPOINT
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        positions = [_dict_to_position(d) for d in data]
        log.info("Checkpoint loaded: %d positions", len(positions))
        return positions
    except Exception as e:
        log.error("Checkpoint load failed: %s", e)
        return []


def reconcile_positions(positions: List[Position]) -> List[Position]:
    """
    Reconcile checkpoint positions against current market state.

    - Expired markets: log as MISSED_EXPIRY, remove
    - API unavailable: keep as unverified, will re-verify on next successful call
    """
    now = time.time()
    live: List[Position] = []
    for pos in positions:
        if pos.market.end_time < now:
            log.warning(
                "MISSED_EXPIRY: %s %s market=%s (expired %.0fs ago)",
                pos.direction, pos.token_id[:16],
                pos.market.condition_id[:16],
                now - pos.market.end_time,
            )
            from pulse.logger import tg, log_trade
            from pulse.config import SessionStats
            stats = SessionStats()
            tg(f"MISSED_EXPIRY: {pos.direction} {pos.market.question}")
            continue
        pos.verified = False
        live.append(pos)
        log.info(
            "Recovered position: %s %s entry=%.3f (unverified)",
            pos.direction, pos.token_id[:16], pos.entry_price,
        )

    if live:
        from pulse.logger import tg
        tg(f"Crash recovery: {len(live)} position(s) recovered, {len(positions) - len(live)} expired")

    return live
