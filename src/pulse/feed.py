"""BTC price feed — WebSocket, REST fallback, candle-based RSI, spike detection."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import List, Tuple

import numpy as np
import websocket

from pulse.config import (
    BTC_CFG, SHUTDOWN_EVENT, SPIKE_THRESHOLD, SPIKE_DISPLAY_THRESHOLD,
    RSI_PERIOD, RSI_OVERBOUGHT, RSI_OVERSOLD,
)

log = logging.getLogger(__name__)

# ── HTTP session (shared with orders.py via lazy import to avoid circular) ───
_http_session = None


def _get_http():
    global _http_session
    if _http_session is None:
        import requests
        from requests.adapters import HTTPAdapter, Retry
        retry = Retry(total=3, backoff_factor=0.3,
                      status_forcelist=[500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=20, max_retries=retry)
        _http_session = requests.Session()
        _http_session.mount("https://", adapter)
        _http_session.headers.update({"Connection": "keep-alive"})
    return _http_session


# ── 1-second candle for RSI ──────────────────────────────────────────────────
class CandleAccumulator:
    """Accumulates ticks into 1-second OHLC candles for clean RSI."""

    def __init__(self, max_candles: int = 120):
        self._candles: deque = deque(maxlen=max_candles)
        self._current_second: int = 0
        self._open = self._high = self._low = self._close = 0.0
        self._has_tick = False
        self._lock = threading.Lock()

    def add_tick(self, price: float):
        now_sec = int(time.monotonic())
        with self._lock:
            if now_sec != self._current_second:
                if self._has_tick:
                    self._candles.append(self._close)
                self._current_second = now_sec
                self._open = self._high = self._low = self._close = price
                self._has_tick = True
            else:
                self._high = max(self._high, price)
                self._low = min(self._low, price)
                self._close = price
                self._has_tick = True

    def closes(self, n: int) -> List[float]:
        with self._lock:
            candles = list(self._candles)
            if self._has_tick:
                candles.append(self._close)
        return candles[-n:] if len(candles) >= n else candles


# ── BTCFeed ──────────────────────────────────────────────────────────────────
class BTCFeed:
    MAXLEN = 2400

    def __init__(self):
        self._ts = np.zeros(self.MAXLEN, dtype=np.float64)
        self._px = np.zeros(self.MAXLEN, dtype=np.float64)
        self._head = 0
        self._size = 0
        self.current = 0.0
        self.ws_status = "connecting"
        self.latency = 0.0
        self._tick_win: deque = deque(maxlen=200)
        self._lock = threading.RLock()
        self._candles = CandleAccumulator()

    def add(self, price: float):
        now = time.monotonic()
        with self._lock:
            idx = self._head % self.MAXLEN
            if self._size >= 1:
                prev_idx = (self._head - 1) % self.MAXLEN
                self.latency = (now - self._ts[prev_idx]) * 1000
            self._ts[idx] = now
            self._px[idx] = price
            self._head += 1
            if self._size < self.MAXLEN:
                self._size += 1
            self.current = price
            self._tick_win.append(now)
        self._candles.add_tick(price)

    def _snapshot(self) -> Tuple[np.ndarray, np.ndarray]:
        with self._lock:
            n = self._size
            if n == 0:
                return np.array([]), np.array([])
            if n < self.MAXLEN:
                return self._ts[:n].copy(), self._px[:n].copy()
            split = self._head % self.MAXLEN
            ts = np.concatenate([self._ts[split:], self._ts[:split]])
            px = np.concatenate([self._px[split:], self._px[:split]])
            return ts, px

    def momentum_all(self) -> Tuple[float, float, float]:
        ts, px = self._snapshot()
        if len(ts) < 2:
            return 0.0, 0.0, 0.0
        now = time.monotonic()

        def _mom(secs: float) -> float:
            i = np.searchsorted(ts, now - secs, side="left")
            if i >= len(ts) - 1:
                return 0.0
            p0 = px[i]
            return (px[-1] - p0) / p0 * 100 if p0 != 0 else 0.0

        return _mom(15), _mom(30), _mom(60)

    def spike_check(self) -> Tuple[bool, float]:
        m15, _, _ = self.momentum_all()
        return abs(m15) >= SPIKE_THRESHOLD, m15

    def volatility(self, seconds: float = 120) -> float:
        ts, px = self._snapshot()
        if len(ts) < 4:
            return 0.0
        pts = px[(time.monotonic() - ts) <= seconds]
        return float(np.std(pts)) if len(pts) > 3 else 0.0

    def last_n(self, n: int = 14) -> List[float]:
        with self._lock:
            if self._size == 0:
                return []
            if self._size < self.MAXLEN:
                return list(self._px[max(0, self._size - n):self._size])
            return [self._px[(self._head - n + i) % self.MAXLEN] for i in range(n)]

    def rsi(self, period: int = RSI_PERIOD) -> float:
        """RSI computed on 1-second candle closes, not raw ticks."""
        prices = self._candles.closes(period + 2)
        if len(prices) < period + 1:
            return 50.0
        arr = np.diff(np.array(prices, dtype=np.float64))
        gains = np.where(arr > 0, arr, 0.0)
        losses = np.where(arr < 0, -arr, 0.0)
        avg_g = np.mean(gains[-period:])
        avg_l = np.mean(losses[-period:])
        if avg_l < 1e-10:
            return 100.0
        return round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 2)

    def ticks_per_sec(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(1 for t in self._tick_win if now - t <= 1.0)


# ── Module singleton ─────────────────────────────────────────────────────────
FEED = BTCFeed()
SPIKE_INTERRUPT = threading.Event()


# ── WebSocket Binance ────────────────────────────────────────────────────────
MAX_WS_RECONNECTS = 5


def start_ws_btc():
    """Run WS with bounded reconnect. Falls back to REST after MAX_WS_RECONNECTS."""
    url = f"wss://stream.binance.com:9443/ws/{BTC_CFG['ws_stream']}"
    reconnect_count = 0
    backoff = 1.0

    while not SHUTDOWN_EVENT.is_set() and reconnect_count < MAX_WS_RECONNECTS:
        last_spike_check = 0.0

        def on_message(ws, msg):
            nonlocal last_spike_check
            try:
                d = json.loads(msg)
                FEED.add(float(d["p"]))
                now = time.monotonic()
                if now - last_spike_check >= 0.25:
                    last_spike_check = now
                    if FEED.spike_check()[0]:
                        SPIKE_INTERRUPT.set()
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.debug("WS message parse error: %s", e)

        def on_open(ws):
            nonlocal reconnect_count, backoff
            FEED.ws_status = "live"
            reconnect_count = 0
            backoff = 1.0
            log.info("Binance WS connected")

        def on_error(ws, err):
            FEED.ws_status = "error"
            log.warning("Binance WS error: %s", err)

        def on_close(ws, *a):
            FEED.ws_status = "reconnecting"
            log.warning("Binance WS closed")

        ws = websocket.WebSocketApp(
            url, on_message=on_message, on_open=on_open,
            on_error=on_error, on_close=on_close,
        )
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log.error("WS run_forever exception: %s", e)

        if SHUTDOWN_EVENT.is_set():
            break

        reconnect_count += 1
        delay = min(backoff + (time.time() % 1.0), 30.0)
        backoff = min(backoff * 2, 30.0)
        log.warning("WS reconnect %d/%d in %.1fs", reconnect_count, MAX_WS_RECONNECTS, delay)
        time.sleep(delay)

    if reconnect_count >= MAX_WS_RECONNECTS:
        FEED.ws_status = "rest_only"
        log.critical("WS reconnect exhausted — switching to REST-only mode")
        from pulse.logger import tg
        tg("WS Binance DOWN — REST-only mode")
        rest_fallback_btc_loop()


def rest_fallback_btc():
    """Initial REST fallback while WS is connecting."""
    while FEED.ws_status == "connecting" and not SHUTDOWN_EVENT.is_set():
        try:
            r = _get_http().get(BTC_CFG["rest"], timeout=2)
            if r.status_code == 200:
                FEED.add(float(r.json()["price"]))
        except Exception as e:
            log.debug("REST fallback error: %s", e)
        time.sleep(0.8)


def rest_fallback_btc_loop():
    """Permanent REST fallback when WS is exhausted."""
    while not SHUTDOWN_EVENT.is_set():
        try:
            r = _get_http().get(BTC_CFG["rest"], timeout=2)
            if r.status_code == 200:
                FEED.add(float(r.json()["price"]))
                FEED.ws_status = "rest_only"
        except Exception as e:
            log.warning("REST loop error: %s", e)
        time.sleep(0.5)


# ── Spike monitor ────────────────────────────────────────────────────────────
def spike_monitor_loop(scan_state):
    """Monitor BTC momentum for spike interrupts. Runs in its own thread."""
    last_logged = {"ts": 0.0, "dir": ""}

    while not SHUTDOWN_EVENT.is_set():
        try:
            m15, m30, m60 = FEED.momentum_all()
            abs_m15 = abs(m15)
            if abs_m15 >= SPIKE_DISPLAY_THRESHOLD and FEED.ws_status in ("live", "rest_only"):
                now = time.monotonic()
                direction_sp = "UP" if m15 > 0 else "DOWN"
                dedup = 5.0 if abs_m15 >= SPIKE_THRESHOLD else 2.0
                if not (last_logged["dir"] == direction_sp and now - last_logged["ts"] < dedup):
                    last_logged["ts"] = now
                    last_logged["dir"] = direction_sp

                    from pulse.strategy import compute_score
                    sc_sp, _, _ = compute_score(
                        None, direction_sp, m15, m30, m60,
                        remaining_min=3.0, window_delta=0.0,
                    )
                    min_s = scan_state["settings"].min_score
                    if abs_m15 >= SPIKE_THRESHOLD:
                        action = "INTERRUPT" if sc_sp >= min_s else "watch"
                    else:
                        action = "signal" if sc_sp >= min_s else "noise"

                    scan_state["spikes"].appendleft({
                        "dir": direction_sp,
                        "mom15": round(m15, 4),
                        "mom60": round(m60, 4),
                        "score": sc_sp,
                        "action": action,
                        "strong": abs_m15 >= SPIKE_THRESHOLD,
                    })
                    if abs_m15 >= SPIKE_THRESHOLD:
                        SPIKE_INTERRUPT.set()
                        log.info("SPIKE %s m15=%.4f%% score=%.3f", direction_sp, m15, sc_sp)
        except Exception as e:
            log.warning("Spike monitor error: %s", e)
        time.sleep(0.5)


def prewarm_connections():
    """Pre-warm HTTP connection pools."""
    http = _get_http()
    for h in [BTC_CFG["rest"].rsplit("/", 1)[0], "https://clob.polymarket.com"]:
        try:
            http.get(h, timeout=3)
        except Exception:
            pass
