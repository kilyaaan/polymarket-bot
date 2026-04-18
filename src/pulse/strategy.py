"""Strategy v6 — Late Trend heuristics, calibrated on 10h of real Polymarket data.

7 heuristics, zero composite score, ~40 lines of decision logic.

Data basis (observed 2026-04-18, 129 markets, 32K snapshots):
  - Mid-price at T<=60s predicts resolution with 86.3% accuracy
  - At |window_delta| > 0.05%: 97.3% accuracy (37 markets)
  - At |window_delta| > 0.10%: 100% accuracy (10 markets)
  - Spread median: 0.01, amplification: 18x median
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)

# ── Thresholds (calibrated from collected data) ──────────────────────────────
DELTA_THRESHOLD = 0.05    # minimum |window_delta| % to trade
LATE_ENTRY_SEC = 60       # only enter in last 60s of market
MAX_ENTRY_PRICE = 0.70    # skip if option already too expensive
COOLDOWN_LOSSES = 3       # pause after N consecutive losses
COOLDOWN_MARKETS = 3      # skip N markets during cooldown

# Sizing tiers (CYNIC verdict mapping)
SIZE_HOWL = 15.0           # |delta| > 0.15%
SIZE_WAG = 10.0            # |delta| > 0.10%
SIZE_GROWL = 5.0           # |delta| > 0.05%

DELTA_HOWL = 0.15
DELTA_WAG = 0.10


@dataclass
class TradeSignal:
    """Output of the gate logic. Either trade or skip."""
    trade: bool
    direction: str = ""
    size_usdc: float = 0.0
    verdict: str = ""       # HOWL/WAG/GROWL/BARK
    window_delta: float = 0.0
    reason: str = ""


def evaluate_gates(
    btc_now: float,
    btc_market_open: float,
    remaining_sec: float,
    entry_price: float,
    consecutive_losses: int,
    cooldown_remaining: int,
) -> TradeSignal:
    """
    3 binary gates + direction + sizing. No composite score.

    H1: Regime gate     — |window_delta| > 0.05%
    H2: Late entry      — remaining_sec <= 60
    H3: Direction = fact — btc_now vs btc_market_open
    H4: Max entry price  — entry_price <= 0.70
    H5: Sizing by tier   — HOWL/WAG/GROWL based on |delta|
    H6: Hold-to-expiry   — (enforced in main loop, not here)
    H7: Cooldown         — 3 consecutive losses → pause
    """
    skip = lambda reason: TradeSignal(trade=False, reason=reason)

    # H7: Cooldown
    if cooldown_remaining > 0:
        return skip(f"COOLDOWN ({cooldown_remaining} left)")

    if consecutive_losses >= COOLDOWN_LOSSES:
        return skip(f"COOLDOWN triggered ({consecutive_losses} losses)")

    # H2: Late entry
    if remaining_sec > LATE_ENTRY_SEC:
        return skip(f"too early ({remaining_sec:.0f}s > {LATE_ENTRY_SEC}s)")

    if remaining_sec <= 5:
        return skip("too late (<5s)")

    # H1: Regime gate
    if btc_market_open <= 0:
        return skip("no market open price")

    window_delta = (btc_now - btc_market_open) / btc_market_open * 100
    abs_delta = abs(window_delta)

    if abs_delta < DELTA_THRESHOLD:
        return skip(f"delta {abs_delta:.4f}% < {DELTA_THRESHOLD}%")

    # H3: Direction = observed fact
    direction = "UP" if btc_now > btc_market_open else "DOWN"

    # H4: Max entry price
    if entry_price > MAX_ENTRY_PRICE:
        return skip(f"price {entry_price:.3f} > {MAX_ENTRY_PRICE}")

    if entry_price <= 0.01:
        return skip(f"price {entry_price:.3f} too low")

    # H5: Sizing by tier
    if abs_delta >= DELTA_HOWL:
        size = SIZE_HOWL
        verdict = "HOWL"
    elif abs_delta >= DELTA_WAG:
        size = SIZE_WAG
        verdict = "WAG"
    else:
        size = SIZE_GROWL
        verdict = "GROWL"

    log.info(
        "GATE PASS: %s delta=%.4f%% entry=%.3f size=$%.0f verdict=%s rem=%0.fs",
        direction, window_delta, entry_price, size, verdict, remaining_sec,
    )

    return TradeSignal(
        trade=True,
        direction=direction,
        size_usdc=size,
        verdict=verdict,
        window_delta=window_delta,
        reason=f"{verdict} delta={window_delta:+.4f}%",
    )


def has_overlapping_position(
    positions: List, new_market, direction: str,
) -> bool:
    """Check if a same-direction position already covers this time window."""
    for p in positions:
        if p.direction == direction:
            if (p.market.start_time < new_market.end_time
                    and new_market.start_time < p.market.end_time):
                return True
    return False
