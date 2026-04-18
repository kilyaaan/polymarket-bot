#!/usr/bin/env python3
"""
Backtest v2 — Crypto Pulse v5.0 with realistic binary option pricing.

Fixes vs v1:
  1. Black-Scholes binary option pricing (CDF, not linear)
  2. Dynamic entry price from BS model (not fixed 0.50)
  3. Spread + slippage on entry/exit
  4. 1m klines for multi-day runs (7+ days practical)
  5. Synthetic OB imbalance correlated to momentum
  6. Parameter sweep mode

Usage:
    python backtest.py                      # 7 days, default score
    python backtest.py --hours 168          # 7 days
    python backtest.py --sweep              # score threshold sweep
    python backtest.py --hours 72 --score 0.50
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np
from scipy.stats import norm  # type: ignore

import requests

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pulse.config import (
    TP_DELTA, SL_DELTA, TRAILING_DISTANCE, HOLD_THRESHOLD, HOLD_MIN_REMAINING,
    MIN_ENTRY_PRICE, MAX_ENTRY_PRICE, SPIKE_THRESHOLD,
    MOM_15S_REF, MOM_30S_REF, MOM_60S_REF,
    RSI_OVERBOUGHT, RSI_OVERSOLD, RSI_PERIOD,
    MIN_POS, MAX_POS, DEFAULT_TAKER_FEE_RATE,
)

# ── Simulation parameters ───────────────────────────────────────────────────
SPREAD = 0.02          # bid-ask spread on Polymarket binary
SLIPPAGE = 0.005       # additional slippage on taker orders
FEE_RATE = DEFAULT_TAKER_FEE_RATE


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1: Black-Scholes binary option pricing
# ═══════════════════════════════════════════════════════════════════════════════
def binary_option_price(
    spot: float,
    strike: float,
    tau: float,
    sigma: float,
    direction: str,
) -> float:
    """
    Price of a binary (digital) option using Black-Scholes.

    P(UP)   = N(d2)    — probability BTC finishes above strike
    P(DOWN) = N(-d2)   — probability BTC finishes below strike

    d2 = ln(S/K) / (sigma * sqrt(tau))

    Where:
      S     = current BTC price
      K     = BTC price at market open (the "strike")
      tau   = time remaining in years (we use seconds / 86400 / 365)
      sigma = annualized realized volatility
    """
    if tau <= 0 or sigma <= 0 or strike <= 0 or spot <= 0:
        # At expiry or degenerate: resolve deterministically
        if direction == "UP":
            return 1.0 if spot > strike else 0.0
        return 1.0 if spot < strike else 0.0

    d2 = math.log(spot / strike) / (sigma * math.sqrt(tau))
    if direction == "UP":
        return max(0.01, min(0.99, norm.cdf(d2)))
    return max(0.01, min(0.99, norm.cdf(-d2)))


def realized_vol_annualized(klines: np.ndarray, ts: float, lookback_s: float = 3600) -> float:
    """
    Compute annualized realized volatility from kline returns.
    Uses last `lookback_s` seconds of data.
    """
    mask = (klines[:, 0] >= ts - lookback_s) & (klines[:, 0] <= ts)
    prices = klines[mask, 1]
    if len(prices) < 10:
        return 0.50  # default ~50% annualized vol for BTC
    returns = np.diff(np.log(prices))
    # Scale to annualized: std of per-interval returns * sqrt(intervals_per_year)
    interval_s = lookback_s / len(prices)
    intervals_per_year = 365.25 * 86400 / interval_s
    return float(np.std(returns) * np.sqrt(intervals_per_year))


# ═══════════════════════════════════════════════════════════════════════════════
# Data download — supports 1s and 1m
# ═══════════════════════════════════════════════════════════════════════════════
def download_klines(hours: int = 168, interval: str = "1m") -> np.ndarray:
    """Download BTC/USDT klines from Binance."""
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - hours * 3600 * 1000

    all_data: list = []
    current = start_ms
    batch = 0

    print(f"Downloading {hours}h of {interval} BTC/USDT klines...")
    while current < end_ms:
        params = {
            "symbol": "BTCUSDT", "interval": interval,
            "startTime": current, "limit": 1000,
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for k in data:
            all_data.append([k[0] / 1000.0, float(k[4])])  # [ts_seconds, close]
        current = int(data[-1][0]) + (60000 if interval == "1m" else 1000)
        batch += 1
        pct = min(100, (current - start_ms) / (end_ms - start_ms) * 100)
        print(f"  batch {batch}: {len(all_data)} klines ({pct:.0f}%)", end="\r")
        time.sleep(0.05)

    print(f"\nDownloaded {len(all_data)} klines ({len(all_data)/(3600 if interval=='1s' else 60):.1f}h)")
    return np.array(all_data, dtype=np.float64)


# ═══════════════════════════════════════════════════════════════════════════════
# Simulated feed
# ═══════════════════════════════════════════════════════════════════════════════
class SimFeed:
    def __init__(self, klines: np.ndarray):
        self.klines = klines
        self.idx = 0
        self._candle_closes: deque = deque(maxlen=120)
        self._last_candle_sec = 0

    def advance_to(self, target_ts: float):
        while self.idx < len(self.klines) and self.klines[self.idx, 0] <= target_ts:
            ts = self.klines[self.idx, 0]
            price = self.klines[self.idx, 1]
            sec = int(ts)
            if sec != self._last_candle_sec:
                self._candle_closes.append(price)
                self._last_candle_sec = sec
            self.idx += 1

    @property
    def current(self) -> float:
        if self.idx == 0:
            return self.klines[0, 1]
        return self.klines[min(self.idx - 1, len(self.klines) - 1), 1]

    def price_at(self, ts: float) -> float:
        i = np.searchsorted(self.klines[:, 0], ts, side="right") - 1
        return self.klines[max(0, i), 1]

    def momentum(self, ts: float, secs: float) -> float:
        p_now = self.price_at(ts)
        p_then = self.price_at(ts - secs)
        if p_then == 0:
            return 0.0
        return (p_now - p_then) / p_then * 100

    def rsi(self, period: int = RSI_PERIOD) -> float:
        prices = list(self._candle_closes)
        if len(prices) < period + 1:
            return 50.0
        arr = np.diff(np.array(prices[-period - 1:], dtype=np.float64))
        gains = np.where(arr > 0, arr, 0.0)
        losses = np.where(arr < 0, -arr, 0.0)
        avg_g = np.mean(gains)
        avg_l = np.mean(losses)
        if avg_l < 1e-10:
            return 100.0
        return round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 2)

    def volatility(self, ts: float, seconds: float = 120) -> float:
        mask = (self.klines[:, 0] >= ts - seconds) & (self.klines[:, 0] <= ts)
        pts = self.klines[mask, 1]
        return float(np.std(pts)) if len(pts) > 3 else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 5: Synthetic OB imbalance
# ═══════════════════════════════════════════════════════════════════════════════
_rng = np.random.default_rng(42)


def synthetic_ob_imbalance(m15: float, direction: str) -> float:
    """
    Generate synthetic OB imbalance correlated ~0.3 with momentum direction.
    Returns imb_score in [0, 1].
    """
    # Base: random centered around 0.5
    raw_imb = 0.5 + 0.3 * np.sign(m15) * abs(m15) / MOM_15S_REF + 0.15 * _rng.standard_normal()
    raw_imb = max(0.0, min(1.0, raw_imb))

    if direction == "UP":
        return min((raw_imb - 0.5) * 4, 1.0) if raw_imb > 0.5 else 0.0
    else:
        return min((0.5 - raw_imb) * 4, 1.0) if raw_imb < 0.5 else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Score with all fixes
# ═══════════════════════════════════════════════════════════════════════════════
def bt_compute_score(
    feed: SimFeed,
    direction: str,
    m15: float, m30: float, m60: float,
    ts: float,
    remaining_min: float,
    btc_start: float,
) -> float:
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

    # FIX 5: synthetic OB imbalance
    imb_score = synthetic_ob_imbalance(m15, direction)

    # RSI
    rsi = feed.rsi()
    if direction == "UP":
        rsi_score = min((rsi - RSI_OVERBOUGHT) / (100.0 - RSI_OVERBOUGHT), 1.0) if rsi > RSI_OVERBOUGHT else 0.0
    else:
        rsi_score = min((RSI_OVERSOLD - rsi) / RSI_OVERSOLD, 1.0) if rsi < RSI_OVERSOLD else 0.0

    vol_score = min(feed.volatility(ts) / 150.0, 1.0)

    # Window delta (now computable — we know btc_start)
    btc_now = feed.price_at(ts)
    window_delta = (btc_now - btc_start) / btc_start * 100 if btc_start > 0 else 0.0
    wd_aligned = window_delta if direction == "UP" else -window_delta
    wd_score = max(min(wd_aligned / 0.10, 1.0), 0.0)

    # Coherence
    moms = [m15, m30, m60] if direction == "UP" else [-m15, -m30, -m60]
    if all(m > 0 for m in moms):
        strength = min(abs(m15) / MOM_15S_REF, abs(m30) / MOM_30S_REF, abs(m60) / MOM_60S_REF)
        coh = min(0.12, 0.08 * strength)
    else:
        coh = 0.0

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
    return round(min(raw, 1.0), 3)


def vote_direction(m15: float, m30: float, m60: float) -> str:
    return "UP" if sum(1 for m in [m15, m30, m60] if m > 0) >= 2 else "DOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class BtPosition:
    direction: str
    entry_price: float
    entry_ts: float
    market_end_ts: float
    size_usdc: float
    shares: float
    score: float
    btc_strike: float     # BTC price at market open
    peak_price: float = 0.0
    trail_sl: float = 0.0
    holding_expiry: bool = False


@dataclass
class BtResult:
    direction: str
    entry_price: float
    exit_price: float
    pnl_gross: float
    pnl_net: float
    reason: str
    score: float
    duration_s: float
    held_expiry: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# Market simulation
# ═══════════════════════════════════════════════════════════════════════════════
def simulate_market(
    feed: SimFeed,
    klines: np.ndarray,
    market_start: float,
    market_end: float,
    min_score: float,
) -> Optional[BtResult]:
    entry_window_start = market_start + 18
    entry_window_end = market_start + 270
    market_duration_s = market_end - market_start

    btc_strike = feed.price_at(market_start)
    pos: Optional[BtPosition] = None

    # Scan interval: 2s for 1s data, 10s for 1m data
    scan_step = 2.0 if len(klines) > 20000 else 10.0

    # ── Entry scan ───────────────────────────────────────────────────────
    for t in np.arange(entry_window_start, min(entry_window_end, market_end - 30), scan_step):
        feed.advance_to(t)
        m15 = feed.momentum(t, 15)
        m30 = feed.momentum(t, 30)
        m60 = feed.momentum(t, 60)

        if abs(m60) < 0.001 and abs(m15) < SPIKE_THRESHOLD:
            continue

        direction = vote_direction(m15, m30, m60)
        remaining_min = (market_end - t) / 60.0
        score = bt_compute_score(feed, direction, m15, m30, m60, t, remaining_min, btc_strike)

        if score < min_score:
            continue

        # FIX 1+2: Entry price from BS model
        sigma = realized_vol_annualized(klines, t)
        tau = (market_end - t) / (86400 * 365.25)
        btc_now = feed.price_at(t)
        mid_price = binary_option_price(btc_now, btc_strike, tau, sigma, direction)

        # FIX 3: Apply spread + slippage (buyer pays above mid)
        entry_price = mid_price + SPREAD / 2 + SLIPPAGE

        if not (MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE):
            continue

        # Kelly sizing
        win_prob = 0.50
        b = (1.0 - entry_price) / max(entry_price, 0.01)
        q = 1.0 - win_prob
        kelly_f = (b * win_prob - q) / b
        size = max(MIN_POS, min(MAX_POS, round(200.0 * max(kelly_f, 0) * 0.25, 2))) if kelly_f > 0 else MIN_POS
        shares = size / entry_price

        pos = BtPosition(
            direction=direction, entry_price=round(entry_price, 4),
            entry_ts=t, market_end_ts=market_end,
            size_usdc=size, shares=shares, score=score,
            btc_strike=btc_strike,
            peak_price=entry_price,
            trail_sl=round(entry_price - SL_DELTA, 4),
        )
        break

    if pos is None:
        return None

    # ── Exit simulation ──────────────────────────────────────────────────
    exit_step = 1.0 if len(klines) > 20000 else 5.0

    for t in np.arange(pos.entry_ts + exit_step, market_end + 1, exit_step):
        feed.advance_to(t)
        btc_now = feed.current
        rem_sec = market_end - t
        tau = max(rem_sec / (86400 * 365.25), 0)
        sigma = realized_vol_annualized(klines, t)

        # FIX 1: BS binary option price
        if rem_sec <= 0:
            # Resolved
            if pos.direction == "UP":
                sim_price = 1.0 if btc_now > pos.btc_strike else 0.0
            else:
                sim_price = 1.0 if btc_now < pos.btc_strike else 0.0
        else:
            sim_price = binary_option_price(btc_now, pos.btc_strike, tau, sigma, pos.direction)

        # Hold-to-expiry
        if (not pos.holding_expiry
                and sim_price >= HOLD_THRESHOLD
                and rem_sec >= HOLD_MIN_REMAINING):
            pos.holding_expiry = True

        # Trailing stop
        if sim_price > pos.peak_price:
            pos.peak_price = sim_price
            pos.trail_sl = max(
                pos.trail_sl,
                round(pos.peak_price - TRAILING_DISTANCE, 4),
                round(pos.entry_price - SL_DELTA, 4),
            )

        eff_tp = round(pos.entry_price + TP_DELTA, 4)
        eff_sl = pos.trail_sl

        reason = None
        exit_price = sim_price

        if pos.holding_expiry:
            if sim_price <= eff_sl:
                reason = "SL(hold)"
                pos.holding_expiry = False
            elif rem_sec <= 0:
                reason = "EXPIRY"
        else:
            if sim_price >= eff_tp:
                reason = "TP"
            elif sim_price <= eff_sl:
                reason = "SL"
            elif rem_sec <= 0:
                reason = "EXPIRY"

        if reason:
            # FIX 3: Spread on exit (seller receives below mid)
            if reason != "EXPIRY":
                exit_price = max(0.01, sim_price - SPREAD / 2)
            # else: expiry resolves at 0 or 1, no spread

            pnl_gross = (exit_price - pos.entry_price) * pos.shares
            fee_buy = pos.size_usdc * FEE_RATE
            fee_sell = 0.0 if (pos.holding_expiry and reason == "EXPIRY") else exit_price * pos.shares * FEE_RATE
            pnl_net = pnl_gross - fee_buy - fee_sell

            return BtResult(
                direction=pos.direction,
                entry_price=pos.entry_price,
                exit_price=round(exit_price, 4),
                pnl_gross=round(pnl_gross, 4),
                pnl_net=round(pnl_net, 4),
                reason=reason,
                score=pos.score,
                duration_s=round(t - pos.entry_ts, 1),
                held_expiry=pos.holding_expiry,
            )

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════════
def report(results: List[BtResult], hours: int, min_score: float,
           n_markets: int, skipped: int, btc_range: tuple):
    print(f"\n{'='*70}")
    print(f"BACKTEST v2 — Crypto Pulse v5.0 (BS pricing + spread + synthetic OB)")
    print(f"{'='*70}")
    print(f"Period       : {hours}h ({hours/24:.1f}d) — {n_markets} markets")
    print(f"Min score    : {min_score}")
    print(f"BTC range    : ${btc_range[0]:,.0f} — ${btc_range[1]:,.0f}")
    print(f"Spread       : {SPREAD} + slippage {SLIPPAGE}")
    print(f"Fee rate     : {FEE_RATE:.1%}")
    print()

    if not results:
        print("NO TRADES")
        return results

    wins = [r for r in results if r.pnl_net >= 0]
    losses = [r for r in results if r.pnl_net < 0]
    total_pnl = sum(r.pnl_net for r in results)
    gross_win = sum(r.pnl_net for r in wins) if wins else 0
    gross_loss = sum(r.pnl_net for r in losses) if losses else 0
    avg_win = np.mean([r.pnl_net for r in wins]) if wins else 0
    avg_loss = np.mean([r.pnl_net for r in losses]) if losses else 0
    pnls = np.array([r.pnl_net for r in results])
    sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(288) if np.std(pnls) > 0 else 0

    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    max_dd = float(np.max(peak - cumulative)) if len(cumulative) > 0 else 0

    print(f"TRADES       : {len(results)} ({skipped} markets skipped, {len(results)/n_markets*100:.0f}% hit rate)")
    print(f"WIN RATE     : {len(wins)/len(results)*100:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"TOTAL P&L    : ${total_pnl:+.2f}")
    print(f"  Gross wins : ${gross_win:+.2f} (avg ${avg_win:+.2f})")
    print(f"  Gross loss : ${gross_loss:+.2f} (avg ${avg_loss:+.2f})")
    pf = abs(gross_win / gross_loss) if gross_loss != 0 else float('inf')
    print(f"PROFIT FACTOR: {pf:.2f}")
    print(f"SHARPE (ann) : {sharpe:.2f}")
    print(f"MAX DRAWDOWN : ${max_dd:.2f}")
    print(f"HELD EXPIRY  : {sum(1 for r in results if r.held_expiry)}")
    print()

    # By reason
    print("BY EXIT REASON:")
    by_reason: dict = {}
    for r in results:
        by_reason.setdefault(r.reason, {"n": 0, "pnl": 0.0, "w": 0})
        by_reason[r.reason]["n"] += 1
        by_reason[r.reason]["pnl"] += r.pnl_net
        if r.pnl_net >= 0:
            by_reason[r.reason]["w"] += 1
    for reason, d in sorted(by_reason.items()):
        wr = d["w"] / d["n"] * 100
        print(f"  {reason:12s}: {d['n']:3d} trades, ${d['pnl']:+8.2f}, WR {wr:.0f}%")
    print()

    # By direction
    print("BY DIRECTION:")
    for label in ["UP", "DOWN"]:
        trades = [r for r in results if r.direction == label]
        if not trades:
            continue
        w = sum(1 for r in trades if r.pnl_net >= 0)
        p = sum(r.pnl_net for r in trades)
        print(f"  {label:5s}: {len(trades):3d} trades, ${p:+8.2f}, WR {w/len(trades)*100:.0f}%")
    print()

    # By score bucket
    print("SCORE DISTRIBUTION:")
    for lo, hi in [(0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01)]:
        bucket = [r for r in results if lo <= r.score < hi]
        if not bucket:
            continue
        w = sum(1 for r in bucket if r.pnl_net >= 0)
        p = sum(r.pnl_net for r in bucket)
        print(f"  [{lo:.2f}-{hi:.2f}): {len(bucket):3d} trades, ${p:+8.2f}, WR {w/len(bucket)*100:.0f}%")
    print()

    # Entry price distribution
    print("ENTRY PRICE DISTRIBUTION:")
    entry_prices = [r.entry_price for r in results]
    print(f"  min={min(entry_prices):.3f}  median={np.median(entry_prices):.3f}  "
          f"max={max(entry_prices):.3f}  mean={np.mean(entry_prices):.3f}")
    print()

    # Equity curve
    print("EQUITY CURVE:")
    width = 50
    mn, mx = cumulative.min(), cumulative.max()
    rng = mx - mn if mx != mn else 1
    step = max(1, len(cumulative) // width)
    for v in cumulative[::step]:
        pos = int((v - mn) / rng * width)
        bar = "." * pos + "|" + "." * (width - pos)
        print(f"  ${v:+8.2f} [{bar}]")
    print(f"\n{'='*70}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def run_backtest(hours: int = 168, min_score: float = 0.60, quiet: bool = False):
    # Use 1m klines for anything > 48h, 1s otherwise
    interval = "1m" if hours > 48 else "1s"
    klines = download_klines(hours=hours, interval=interval)
    if len(klines) < 300:
        print("Not enough data")
        return []

    feed = SimFeed(klines)
    start_ts = klines[0, 0]
    end_ts = klines[-1, 0]

    warmup_end = start_ts + 120
    first_market = math.ceil(warmup_end / 300) * 300
    markets = []
    t = first_market
    while t + 300 <= end_ts:
        markets.append((t, t + 300))
        t += 300

    if not quiet:
        print(f"\nSimulating {len(markets)} markets over {hours}h (score >= {min_score})")
        print(f"BTC range: ${klines[:, 1].min():,.0f} — ${klines[:, 1].max():,.0f}")
        print(f"Period: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(start_ts))} → "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(end_ts))} UTC")
        print(f"Interval: {interval}, pricing: Black-Scholes binary")

    results: List[BtResult] = []
    skipped = 0

    for i, (m_start, m_end) in enumerate(markets):
        feed.idx = max(0, np.searchsorted(klines[:, 0], m_start - 60) - 1)
        feed._candle_closes.clear()
        feed._last_candle_sec = 0
        feed.advance_to(m_start)

        result = simulate_market(feed, klines, m_start, m_end, min_score)
        if result:
            results.append(result)
        else:
            skipped += 1

        if not quiet and (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(markets)} markets, {len(results)} trades", end="\r")

    btc_range = (float(klines[:, 1].min()), float(klines[:, 1].max()))
    if not quiet:
        report(results, hours, min_score, len(markets), skipped, btc_range)
    return results


def run_sweep(hours: int = 168):
    """Parameter sweep across score thresholds."""
    interval = "1m" if hours > 48 else "1s"
    klines = download_klines(hours=hours, interval=interval)
    if len(klines) < 300:
        print("Not enough data")
        return

    start_ts, end_ts = klines[0, 0], klines[-1, 0]
    warmup_end = start_ts + 120
    first_market = math.ceil(warmup_end / 300) * 300
    markets = []
    t = first_market
    while t + 300 <= end_ts:
        markets.append((t, t + 300))
        t += 300

    print(f"\n{'='*70}")
    print(f"PARAMETER SWEEP — {hours}h ({hours/24:.1f}d), {len(markets)} markets")
    print(f"BTC: ${klines[:, 1].min():,.0f} — ${klines[:, 1].max():,.0f}")
    print(f"{'='*70}\n")

    thresholds = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    print(f"{'Score':>6} {'Trades':>7} {'WinR%':>6} {'P&L':>9} {'PF':>6} {'Sharpe':>7} {'MaxDD':>7} {'Held':>5}")
    print("-" * 65)

    for min_score in thresholds:
        # Reset RNG for reproducibility across thresholds
        global _rng
        _rng = np.random.default_rng(42)

        feed = SimFeed(klines)
        results: List[BtResult] = []
        for m_start, m_end in markets:
            feed.idx = max(0, np.searchsorted(klines[:, 0], m_start - 60) - 1)
            feed._candle_closes.clear()
            feed._last_candle_sec = 0
            feed.advance_to(m_start)
            r = simulate_market(feed, klines, m_start, m_end, min_score)
            if r:
                results.append(r)

        if not results:
            print(f"{min_score:>6.2f} {'0':>7} {'—':>6} {'—':>9} {'—':>6} {'—':>7} {'—':>7} {'—':>5}")
            continue

        wins = sum(1 for r in results if r.pnl_net >= 0)
        total_pnl = sum(r.pnl_net for r in results)
        gross_w = sum(r.pnl_net for r in results if r.pnl_net >= 0)
        gross_l = sum(r.pnl_net for r in results if r.pnl_net < 0)
        pf = abs(gross_w / gross_l) if gross_l != 0 else float('inf')
        pnls = np.array([r.pnl_net for r in results])
        sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(288) if np.std(pnls) > 0 else 0
        cum = np.cumsum(pnls)
        max_dd = float(np.max(np.maximum.accumulate(cum) - cum))
        held = sum(1 for r in results if r.held_expiry)
        wr = wins / len(results) * 100

        pf_str = f"{pf:.2f}" if pf < 100 else "inf"
        print(f"{min_score:>6.2f} {len(results):>7} {wr:>5.1f}% ${total_pnl:>+7.2f} {pf_str:>6} {sharpe:>+6.2f} ${max_dd:>6.2f} {held:>5}")

    print(f"\n{'='*70}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backtest v2 — Crypto Pulse v5.0")
    p.add_argument("--hours", type=int, default=168, help="Hours of history (default: 168 = 7d)")
    p.add_argument("--score", type=float, default=0.60, help="Min score (default: 0.60)")
    p.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    args = p.parse_args()

    if args.sweep:
        run_sweep(hours=args.hours)
    else:
        run_backtest(hours=args.hours, min_score=args.score)
