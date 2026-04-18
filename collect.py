#!/usr/bin/env python3
"""
Data collector — log real Polymarket prices for BTC 5-min markets.

Runs alongside the bot. Every 2 seconds, for each active BTC 5-min market:
- Fetches the real order book (public, no auth)
- Logs: timestamp, market_slug, condition_id, direction, bid, ask, mid,
        bid_depth, ask_depth, spread, btc_price, time_remaining_s

Output: polymarket_data.csv — the REAL data needed for honest backtesting.

Usage:
    python collect.py                   # collect until Ctrl+C
    python collect.py --hours 24        # collect for 24h
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter, Retry

HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

OUTPUT = Path("polymarket_data.csv")
SCAN_INTERVAL = 2  # seconds

# HTTP session with retry
retry = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
HTTP = requests.Session()
HTTP.mount("https://", adapter)

CSV_COLUMNS = [
    "timestamp", "market_slug", "condition_id",
    "token_id_up", "token_id_down",
    "bid_up", "ask_up", "mid_up", "bid_depth_up", "ask_depth_up", "spread_up",
    "bid_down", "ask_down", "mid_down", "bid_depth_down", "ask_depth_down", "spread_down",
    "btc_price", "time_remaining_s",
    "market_start_ts", "market_end_ts",
]


def get_btc_price() -> float:
    try:
        r = HTTP.get(BINANCE_REST, timeout=2)
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    return 0.0


def parse_ts(s: str) -> float | None:
    if not s:
        return None
    try:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        elif "+" not in s[-7:] and len(s) > 10:
            s += "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def fetch_active_btc_markets() -> list:
    """Find active BTC 5-min markets with their token IDs."""
    now_ts = time.time()
    markets = []

    for off in range(0, 4):
        slot = int((now_ts // 300 + off) * 300)
        slug = f"btc-updown-5m-{slot}"
        try:
            r = HTTP.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=4)
            if r.status_code != 200:
                continue
            data = r.json()
            if not data:
                continue
            for m in (data if isinstance(data, list) else [data]):
                cid = m.get("conditionId", "")
                if not cid:
                    continue
                end_ts = (parse_ts(m.get("endDate"))
                          or parse_ts(m.get("end_date_iso"))
                          or parse_ts(m.get("endDateIso")))
                if not end_ts:
                    m2 = re.search(r"-(\d{9,11})$", m.get("slug", ""))
                    if m2:
                        end_ts = int(m2.group(1))
                if not end_ts or end_ts < now_ts - 15 or end_ts > now_ts + 330:
                    continue

                # Get token IDs
                try:
                    cr = HTTP.get(f"{HOST}/markets/{cid}", timeout=4)
                    if cr.status_code != 200:
                        continue
                    tokens = cr.json().get("tokens", [])
                    up = next((t["token_id"] for t in tokens if any(
                        x in t.get("outcome", "").upper() for x in ["UP", "YES"])), None)
                    dn = next((t["token_id"] for t in tokens if any(
                        x in t.get("outcome", "").upper() for x in ["DOWN", "NO"])), None)
                    if not up or not dn:
                        continue
                    markets.append({
                        "slug": m.get("slug", slug),
                        "condition_id": cid,
                        "up_token": up,
                        "down_token": dn,
                        "start_ts": end_ts - 300,
                        "end_ts": end_ts,
                    })
                except Exception:
                    continue
        except Exception:
            continue
    return markets


def get_ob(token_id: str) -> dict | None:
    """Fetch order book — public endpoint, no auth needed."""
    try:
        r = HTTP.get(f"{HOST}/book", params={"token_id": token_id}, timeout=2)
        if r.status_code != 200:
            return None
        book = r.json()
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)[:5]
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))[:5]
        if not bids or not asks:
            return None
        bb = float(bids[0]["price"])
        ba = float(asks[0]["price"])
        bd = sum(float(b["price"]) * float(b["size"]) for b in bids)
        ad = sum(float(a["price"]) * float(a["size"]) for a in asks)
        return {
            "bid": bb, "ask": ba, "mid": round((bb + ba) / 2, 4),
            "bid_depth": round(bd, 2), "ask_depth": round(ad, 2),
            "spread": round(ba - bb, 4),
        }
    except Exception:
        return None


def init_csv():
    if not OUTPUT.exists():
        with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_COLUMNS)
        print(f"Created {OUTPUT}")


def collect(max_hours: float | None = None):
    init_csv()
    start = time.time()
    deadline = start + max_hours * 3600 if max_hours else float("inf")
    snapshots = 0
    rows_written = 0
    last_markets: list = []
    last_market_fetch = 0.0

    print(f"Collecting Polymarket BTC 5-min data...")
    print(f"Output: {OUTPUT}")
    print(f"Duration: {'indefinite' if max_hours is None else f'{max_hours}h'}")
    print(f"Press Ctrl+C to stop\n")

    try:
        while time.time() < deadline:
            now = time.time()
            snapshots += 1

            # Refresh market list every 30s
            if now - last_market_fetch > 30:
                last_markets = fetch_active_btc_markets()
                last_market_fetch = now
                if last_markets:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                          f"{len(last_markets)} active market(s)", end="")

            btc = get_btc_price()

            for mkt in last_markets:
                remaining = mkt["end_ts"] - now
                if remaining < -15:
                    continue

                ob_up = get_ob(mkt["up_token"])
                ob_dn = get_ob(mkt["down_token"])

                if ob_up or ob_dn:
                    row = [
                        datetime.now().isoformat(),
                        mkt["slug"], mkt["condition_id"],
                        mkt["up_token"], mkt["down_token"],
                        # UP side
                        ob_up["bid"] if ob_up else "", ob_up["ask"] if ob_up else "",
                        ob_up["mid"] if ob_up else "",
                        ob_up["bid_depth"] if ob_up else "", ob_up["ask_depth"] if ob_up else "",
                        ob_up["spread"] if ob_up else "",
                        # DOWN side
                        ob_dn["bid"] if ob_dn else "", ob_dn["ask"] if ob_dn else "",
                        ob_dn["mid"] if ob_dn else "",
                        ob_dn["bid_depth"] if ob_dn else "", ob_dn["ask_depth"] if ob_dn else "",
                        ob_dn["spread"] if ob_dn else "",
                        # Context
                        btc, round(remaining, 1),
                        mkt["start_ts"], mkt["end_ts"],
                    ]
                    with open(OUTPUT, "a", newline="", encoding="utf-8") as f:
                        csv.writer(f).writerow(row)
                    rows_written += 1

            elapsed = time.time() - start
            if snapshots % 30 == 0:  # status every ~60s
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
                      f"{rows_written} rows, {elapsed/60:.0f}min elapsed, "
                      f"BTC=${btc:,.0f}")

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        pass

    elapsed = time.time() - start
    print(f"\n{'='*50}")
    print(f"Collection complete")
    print(f"  Duration: {elapsed/60:.1f} min")
    print(f"  Snapshots: {snapshots}")
    print(f"  Rows written: {rows_written}")
    print(f"  Output: {OUTPUT}")
    print(f"{'='*50}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Polymarket data collector")
    p.add_argument("--hours", type=float, default=None, help="Hours to collect (default: indefinite)")
    args = p.parse_args()
    collect(max_hours=args.hours)
