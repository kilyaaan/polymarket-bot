"""Strategy — scoring, Kelly sizing, direction voting, coherence."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pulse.config import (
    MIN_POS, MAX_POS, SPIKE_THRESHOLD,
    MOM_15S_REF, MOM_30S_REF, MOM_60S_REF,
    RSI_OVERBOUGHT, RSI_OVERSOLD, TRADES_CSV,
)
from pulse.feed import FEED

log = logging.getLogger(__name__)


# ── Direction voting ─────────────────────────────────────────────────────────
def vote_direction(m15: float, m30: float, m60: float) -> str:
    """Simple majority across 3 timeframes. No single timeframe dominates."""
    votes_up = sum(1 for m in [m15, m30, m60] if m > 0)
    return "UP" if votes_up >= 2 else "DOWN"


# ── Coherence — magnitude-weighted ──────────────────────────────────────────
def coherence_bonus(m15: float, m30: float, m60: float, direction: str) -> float:
    """Proportional bonus when all timeframes agree in magnitude, not just sign."""
    moms = [m15, m30, m60] if direction == "UP" else [-m15, -m30, -m60]
    if not all(m > 0 for m in moms):
        return 0.0
    strength = min(
        abs(m15) / MOM_15S_REF,
        abs(m30) / MOM_30S_REF,
        abs(m60) / MOM_60S_REF,
    )
    return min(0.12, 0.08 * strength)


# ── Score v5.0 ───────────────────────────────────────────────────────────────
def compute_score(
    ob: Optional[dict],
    direction: str,
    m15: float, m30: float, m60: float,
    remaining_min: float = 3.0,
    window_delta: float = 0.0,
) -> Tuple[float, float, float]:
    """
    Entry score v5.0.

    raw = (0.45*mom + 0.20*imb + 0.10*rsi + 0.15*wd + 0.05*vol
           + spike_bonus + coherence) * time_factor
    """
    def ds(mom: float, ref: float) -> float:
        if direction == "UP":
            return min(abs(mom) / ref, 1.0) if mom > 0 else 0.0
        return min(abs(mom) / ref, 1.0) if mom < 0 else 0.0

    mom_score = (
        ds(m15, MOM_15S_REF) * 0.50
        + ds(m30, MOM_30S_REF) * 0.30
        + ds(m60, MOM_60S_REF) * 0.20
    )

    spike_bonus = 0.15 if abs(m15) >= SPIKE_THRESHOLD else 0.0

    # OB imbalance — both UP and DOWN computed correctly
    imb_score = 0.0
    if ob and ob["total_d"] > 0:
        imb = ob["bid_d"] / ob["total_d"]
        if direction == "UP":
            imb_score = min((imb - 0.5) * 4, 1.0) if imb > 0.5 else 0.0
        else:
            imb_score = min((0.5 - imb) * 4, 1.0) if imb < 0.5 else 0.0

    # RSI on candle closes
    rsi = FEED.rsi()
    if direction == "UP":
        rsi_score = min((rsi - RSI_OVERBOUGHT) / (100.0 - RSI_OVERBOUGHT), 1.0) if rsi > RSI_OVERBOUGHT else 0.0
    else:
        rsi_score = min((RSI_OVERSOLD - rsi) / RSI_OVERSOLD, 1.0) if rsi < RSI_OVERSOLD else 0.0

    # Volatility
    vol_score = min(FEED.volatility() / 150.0, 1.0)

    # Window delta
    wd_aligned = window_delta if direction == "UP" else -window_delta
    wd_score = max(min(wd_aligned / 0.10, 1.0), 0.0)

    # Coherence — magnitude-weighted
    coh = coherence_bonus(m15, m30, m60, direction)

    # Time penalty
    if remaining_min < 1.0:
        time_factor = 0.5
    elif remaining_min < 1.5:
        time_factor = 0.75
    else:
        time_factor = 1.0

    raw = (
        0.45 * mom_score + 0.20 * imb_score + 0.10 * rsi_score
        + 0.15 * wd_score + 0.05 * vol_score
        + spike_bonus + coh
    ) * time_factor
    return round(min(raw, 1.0), 3), mom_score, imb_score


# ── Kelly fractional sizing — calibrated ─────────────────────────────────────
_win_prob_cache: Dict[str, float] = {}
_win_prob_cache_age: float = 0.0
_CACHE_TTL = 300.0  # recalculate every 5 minutes


def _load_win_rates(csv_path: Path) -> Dict[str, float]:
    """Compute win rate per score bucket from trade history CSV."""
    buckets: Dict[str, list] = {
        "0.50-0.60": [], "0.60-0.70": [], "0.70-0.80": [], "0.80+": [],
    }
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    score = float(row.get("score", 0))
                    pnl = float(row.get("pnl_net", 0))
                except (ValueError, TypeError):
                    continue
                if score < 0.50:
                    continue
                if score < 0.60:
                    buckets["0.50-0.60"].append(pnl >= 0)
                elif score < 0.70:
                    buckets["0.60-0.70"].append(pnl >= 0)
                elif score < 0.80:
                    buckets["0.70-0.80"].append(pnl >= 0)
                else:
                    buckets["0.80+"].append(pnl >= 0)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Error reading trade CSV for calibration: %s", e)

    result = {}
    for bucket_name, outcomes in buckets.items():
        if len(outcomes) >= 30:
            result[bucket_name] = sum(outcomes) / len(outcomes)
            log.info("kelly: bucket=%s source=calibrated p=%.3f n=%d",
                     bucket_name, result[bucket_name], len(outcomes))
        else:
            result[bucket_name] = 0.50
            log.info("kelly: bucket=%s source=fallback p=0.50 n=%d",
                     bucket_name, len(outcomes))
    return result


def _get_win_prob(score: float) -> float:
    """Get calibrated win probability for a score, with caching."""
    global _win_prob_cache, _win_prob_cache_age
    import time
    now = time.time()
    if now - _win_prob_cache_age > _CACHE_TTL:
        _win_prob_cache = _load_win_rates(TRADES_CSV)
        _win_prob_cache_age = now

    if score < 0.60:
        return _win_prob_cache.get("0.50-0.60", 0.50)
    elif score < 0.70:
        return _win_prob_cache.get("0.60-0.70", 0.50)
    elif score < 0.80:
        return _win_prob_cache.get("0.70-0.80", 0.50)
    else:
        return _win_prob_cache.get("0.80+", 0.50)


def kelly_size(score: float, entry_price: float, bankroll: float) -> float:
    """
    1/4 Kelly sizing with calibrated win probability.

    Uses historical win rate per score bucket when >= 30 samples available.
    Falls back to conservative 0.50 during cold start.
    """
    win_prob = max(0.45, min(0.75, _get_win_prob(score)))
    b = (1.0 - entry_price) / max(entry_price, 0.01)
    q = 1.0 - win_prob
    kelly_f = (b * win_prob - q) / b
    if kelly_f <= 0:
        return MIN_POS
    return max(MIN_POS, min(MAX_POS, round(bankroll * kelly_f * 0.25, 2)))


# ── Position correlation ─────────────────────────────────────────────────────
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
