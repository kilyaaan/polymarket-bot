#!/usr/bin/env python3
"""
Direction accuracy test — the only honest backtest we can run.

Question: when the v5 scoring says "UP" or "DOWN", does BTC actually
move in that direction over the remaining market window?

This test uses ONLY Binance data (real) and the scoring logic (real).
No option pricing model. No made-up spreads. Just: did BTC go the
right way?

If accuracy < 50%: the strategy has no edge, stop here.
If accuracy > 55%: there may be an edge worth forward-testing.
"""

from __future__ import annotations

import math
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import List

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pulse.config import (
    SPIKE_THRESHOLD, MOM_15S_REF, MOM_30S_REF, MOM_60S_REF,
    RSI_OVERBOUGHT, RSI_OVERSOLD, RSI_PERIOD, MIN_ENTRY_PRICE,
    MAX_ENTRY_PRICE,
)


# ── Data ─────────────────────────────────────────────────────────────────────
def download_klines(hours: int, interval: str = "1m") -> np.ndarray:
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    all_data: list = []
    current = start_ms
    batch = 0
    print(f"Downloading {hours}h of {interval} klines...")
    while current < end_ms:
        r = requests.get(url, params={
            "symbol": "BTCUSDT", "interval": interval,
            "startTime": current, "limit": 1000,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for k in data:
            all_data.append([k[0] / 1000.0, float(k[4])])
        current = int(data[-1][0]) + (60000 if interval == "1m" else 1000)
        batch += 1
        print(f"  batch {batch}: {len(all_data)} klines", end="\r")
        time.sleep(0.05)
    print(f"\nDownloaded {len(all_data)} klines")
    return np.array(all_data, dtype=np.float64)


class SimFeed:
    def __init__(self, klines: np.ndarray):
        self.klines = klines
        self.idx = 0
        self._candle_closes: deque = deque(maxlen=120)
        self._last_candle_sec = 0

    def advance_to(self, target_ts: float):
        while self.idx < len(self.klines) and self.klines[self.idx, 0] <= target_ts:
            sec = int(self.klines[self.idx, 0])
            if sec != self._last_candle_sec:
                self._candle_closes.append(self.klines[self.idx, 1])
                self._last_candle_sec = sec
            self.idx += 1

    def price_at(self, ts: float) -> float:
        i = np.searchsorted(self.klines[:, 0], ts, side="right") - 1
        return self.klines[max(0, i), 1]

    def momentum(self, ts: float, secs: float) -> float:
        p_now = self.price_at(ts)
        p_then = self.price_at(ts - secs)
        return (p_now - p_then) / p_then * 100 if p_then else 0.0

    def rsi(self, period: int = RSI_PERIOD) -> float:
        prices = list(self._candle_closes)
        if len(prices) < period + 1:
            return 50.0
        arr = np.diff(np.array(prices[-period - 1:], dtype=np.float64))
        gains = np.where(arr > 0, arr, 0.0)
        losses = np.where(arr < 0, -arr, 0.0)
        avg_g, avg_l = np.mean(gains), np.mean(losses)
        if avg_l < 1e-10:
            return 100.0
        return round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 2)

    def volatility(self, ts: float, seconds: float = 120) -> float:
        mask = (self.klines[:, 0] >= ts - seconds) & (self.klines[:, 0] <= ts)
        pts = self.klines[mask, 1]
        return float(np.std(pts)) if len(pts) > 3 else 0.0


# ── Score (exact v5 logic, minus OB which is unknown) ────────────────────────
def compute_score(feed, direction, m15, m30, m60, ts, remaining_min, btc_start):
    def ds(mom, ref):
        if direction == "UP":
            return min(abs(mom) / ref, 1.0) if mom > 0 else 0.0
        return min(abs(mom) / ref, 1.0) if mom < 0 else 0.0

    mom_score = ds(m15, MOM_15S_REF) * 0.50 + ds(m30, MOM_30S_REF) * 0.30 + ds(m60, MOM_60S_REF) * 0.20
    spike_bonus = 0.15 if abs(m15) >= SPIKE_THRESHOLD else 0.0
    imb_score = 0.0  # unknown

    rsi = feed.rsi()
    if direction == "UP":
        rsi_score = min((rsi - RSI_OVERBOUGHT) / (100.0 - RSI_OVERBOUGHT), 1.0) if rsi > RSI_OVERBOUGHT else 0.0
    else:
        rsi_score = min((RSI_OVERSOLD - rsi) / RSI_OVERSOLD, 1.0) if rsi < RSI_OVERSOLD else 0.0

    vol_score = min(feed.volatility(ts) / 150.0, 1.0)

    btc_now = feed.price_at(ts)
    wd = (btc_now - btc_start) / btc_start * 100 if btc_start > 0 else 0.0
    wd_aligned = wd if direction == "UP" else -wd
    wd_score = max(min(wd_aligned / 0.10, 1.0), 0.0)

    moms = [m15, m30, m60] if direction == "UP" else [-m15, -m30, -m60]
    if all(m > 0 for m in moms):
        coh = min(0.12, 0.08 * min(abs(m15) / MOM_15S_REF, abs(m30) / MOM_30S_REF, abs(m60) / MOM_60S_REF))
    else:
        coh = 0.0

    tf = 0.5 if remaining_min < 1.0 else (0.75 if remaining_min < 1.5 else 1.0)
    raw = (0.45 * mom_score + 0.20 * imb_score + 0.10 * rsi_score
           + 0.15 * wd_score + 0.05 * vol_score + spike_bonus + coh) * tf
    return round(min(raw, 1.0), 3)


def vote_direction(m15, m30, m60):
    return "UP" if sum(1 for m in [m15, m30, m60] if m > 0) >= 2 else "DOWN"


# ── Prediction record ────────────────────────────────────────────────────────
@dataclass
class Prediction:
    direction: str
    score: float
    entry_ts: float
    market_end_ts: float
    btc_at_entry: float
    btc_at_expiry: float
    correct: bool
    btc_move_pct: float  # actual BTC move from market open to close


# ── Main test ────────────────────────────────────────────────────────────────
def run_direction_test(hours: int = 168, min_score: float = 0.50):
    interval = "1m" if hours > 48 else "1s"
    klines = download_klines(hours, interval)
    if len(klines) < 300:
        print("Not enough data")
        return

    feed = SimFeed(klines)
    start_ts, end_ts = klines[0, 0], klines[-1, 0]

    first_market = math.ceil((start_ts + 120) / 300) * 300
    markets = []
    t = first_market
    while t + 300 <= end_ts:
        markets.append((t, t + 300))
        t += 300

    print(f"\nTesting direction accuracy over {len(markets)} markets ({hours}h)")
    print(f"BTC: ${klines[:, 1].min():,.0f} — ${klines[:, 1].max():,.0f}")
    print(f"Score thresholds tested: 0.00 (all), then {min_score}")
    print()

    predictions: List[Prediction] = []
    scan_step = 10.0 if interval == "1m" else 2.0

    for m_start, m_end in markets:
        feed.idx = max(0, np.searchsorted(klines[:, 0], m_start - 60) - 1)
        feed._candle_closes.clear()
        feed._last_candle_sec = 0
        feed.advance_to(m_start)

        btc_start = feed.price_at(m_start)
        btc_end = feed.price_at(m_end)
        btc_move = (btc_end - btc_start) / btc_start * 100

        # Try to find an entry in the window
        for t in np.arange(m_start + 18, min(m_start + 270, m_end - 30), scan_step):
            feed.advance_to(t)
            m15 = feed.momentum(t, 15)
            m30 = feed.momentum(t, 30)
            m60 = feed.momentum(t, 60)

            if abs(m60) < 0.001 and abs(m15) < SPIKE_THRESHOLD:
                continue

            direction = vote_direction(m15, m30, m60)
            remaining_min = (m_end - t) / 60.0
            score = compute_score(feed, direction, m15, m30, m60, t, remaining_min, btc_start)

            # Record even low-score predictions for analysis
            if score < 0.30:
                continue

            btc_entry = feed.price_at(t)
            # "Correct" = BTC moved in predicted direction from entry to market end
            if direction == "UP":
                correct = btc_end > btc_entry
            else:
                correct = btc_end < btc_entry

            predictions.append(Prediction(
                direction=direction, score=score,
                entry_ts=t, market_end_ts=m_end,
                btc_at_entry=btc_entry, btc_at_expiry=btc_end,
                correct=correct, btc_move_pct=btc_move,
            ))
            break  # one prediction per market

    if not predictions:
        print("No predictions generated")
        return

    # ── Results ──────────────────────────────────────────────────────────
    print(f"{'='*70}")
    print(f"DIRECTION ACCURACY — {len(predictions)} predictions over {hours/24:.1f} days")
    print(f"{'='*70}\n")

    # Overall
    correct = sum(1 for p in predictions if p.correct)
    print(f"ALL PREDICTIONS (score >= 0.30):")
    print(f"  Total: {len(predictions)}, Correct: {correct}, Accuracy: {correct/len(predictions)*100:.1f}%")
    print()

    # By score bucket
    print(f"{'Score':>12} {'Total':>6} {'Correct':>8} {'Accuracy':>9} {'Signal?':>8}")
    print("-" * 50)
    for lo, hi in [(0.30, 0.40), (0.40, 0.50), (0.50, 0.55), (0.55, 0.60),
                   (0.60, 0.65), (0.65, 0.70), (0.70, 0.80), (0.80, 1.01)]:
        bucket = [p for p in predictions if lo <= p.score < hi]
        if not bucket:
            continue
        c = sum(1 for p in bucket if p.correct)
        acc = c / len(bucket) * 100
        signal = "YES" if acc > 55 else ("MAYBE" if acc > 52 else "NO")
        print(f"  [{lo:.2f}-{hi:.2f}) {len(bucket):>6} {c:>8} {acc:>8.1f}% {signal:>8}")
    print()

    # By direction
    print("BY DIRECTION:")
    for d in ["UP", "DOWN"]:
        subset = [p for p in predictions if p.direction == d]
        if not subset:
            continue
        c = sum(1 for p in subset if p.correct)
        print(f"  {d:5s}: {len(subset)} predictions, {c/len(subset)*100:.1f}% accurate")
    print()

    # By BTC regime (trending vs range)
    print("BY BTC REGIME (based on |m60| at entry):")
    for label, lo, hi in [("Range (<0.02%)", 0, 0.02), ("Mild (0.02-0.10%)", 0.02, 0.10),
                           ("Trending (>0.10%)", 0.10, 100)]:
        subset = [p for p in predictions
                  if lo <= abs(feed.momentum(p.entry_ts, 60)) < hi]  # approximate
        # Can't easily recompute momentum here, use btc_move as proxy
        subset = [p for p in predictions if lo <= abs(p.btc_move_pct) < hi]
        if not subset:
            continue
        c = sum(1 for p in subset if p.correct)
        print(f"  {label:25s}: {len(subset)} pred, {c/len(subset)*100:.1f}% accurate")
    print()

    # The key question
    above_threshold = [p for p in predictions if p.score >= min_score]
    if above_threshold:
        c = sum(1 for p in above_threshold if p.correct)
        acc = c / len(above_threshold) * 100
        print(f"{'='*50}")
        print(f"AT SCORE >= {min_score}: {len(above_threshold)} predictions, {acc:.1f}% accurate")
        if acc > 55:
            print(f"  -> EDGE DETECTED. Forward-test to confirm.")
        elif acc > 52:
            print(f"  -> MARGINAL. Needs more data or lower costs.")
        else:
            print(f"  -> NO EDGE. Strategy does not predict direction.")
        print(f"{'='*50}")
    print()

    # Confidence interval (binomial)
    n = len(above_threshold) if above_threshold else len(predictions)
    k = sum(1 for p in (above_threshold or predictions) if p.correct)
    p_hat = k / n
    se = math.sqrt(p_hat * (1 - p_hat) / n)
    print(f"95% CI: {p_hat*100:.1f}% +/- {1.96*se*100:.1f}%")
    print(f"(Need CI lower bound > 50% to claim edge with confidence)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Direction accuracy test")
    p.add_argument("--hours", type=int, default=168, help="Hours (default: 168 = 7d)")
    p.add_argument("--score", type=float, default=0.55, help="Score threshold to evaluate")
    args = p.parse_args()
    run_direction_test(hours=args.hours, min_score=args.score)
