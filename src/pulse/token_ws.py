"""Token WebSocket — real-time Polymarket price feed for SL/TP monitoring.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market and
subscribes to best_bid_ask / last_trade_price events per token.

When a position's SL or TP level is hit, an event is pushed to
_event_queue: (token_id, reason, price) — consumed by the main scan loop.

Reaction time: ~100ms vs ~1s for REST polling.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Event queue consumed by main.py ─────────────────────────────────────────
# Each item: (token_id: str, reason: str, price: float)
# reason is "SL" or "TP"
_event_queue: queue.Queue = queue.Queue(maxsize=200)

# ── Active subscriptions: token_id → (sl_price, tp_price) ───────────────────
_subs: Dict[str, Tuple[float, float]] = {}
_subs_lock = threading.Lock()

# ── Latest mid prices: token_id → price ─────────────────────────────────────
_prices: Dict[str, float] = {}
_prices_lock = threading.Lock()

# ── WebSocket instance ───────────────────────────────────────────────────────
_ws = None
_ws_lock = threading.Lock()


# ── Public API ────────────────────────────────────────────────────────────────

def get_event_queue() -> queue.Queue:
    return _event_queue


def current_price(token_id: str) -> Optional[float]:
    with _prices_lock:
        return _prices.get(token_id)


def subscribe(token_id: str, sl_price: float, tp_price: float):
    """Register a token for real-time SL/TP monitoring."""
    with _subs_lock:
        _subs[token_id] = (sl_price, tp_price)
    _ws_send({"assets_ids": [token_id], "type": "market", "custom_feature_enabled": True})
    log.info("Token WS subscribed: %s SL=%.3f TP=%.3f", token_id[:16], sl_price, tp_price)


def unsubscribe(token_id: str):
    """Remove a token from SL/TP monitoring."""
    with _subs_lock:
        _subs.pop(token_id, None)
    with _prices_lock:
        _prices.pop(token_id, None)
    _ws_send({"assets_ids": [token_id], "operation": "unsubscribe"})
    log.info("Token WS unsubscribed: %s", token_id[:16])


def update_levels(token_id: str, sl_price: float, tp_price: float):
    """Update SL/TP levels for an existing subscription (e.g. trailing stop)."""
    with _subs_lock:
        if token_id in _subs:
            _subs[token_id] = (sl_price, tp_price)


def start(shutdown_event) -> threading.Thread:
    """Start the WebSocket thread. Returns the thread."""
    t = threading.Thread(
        target=lambda: _run_loop(shutdown_event),
        name="token_ws", daemon=True,
    )
    t.start()
    return t


# ── Internal ──────────────────────────────────────────────────────────────────

def _ws_send(msg: dict):
    with _ws_lock:
        ws = _ws
    if ws is not None:
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            log.debug("Token WS send error: %s", e)


def _handle_message(msg: dict):
    event = msg.get("event_type")

    if event == "best_bid_ask":
        tid = msg.get("asset_id", "")
        try:
            bid = float(msg["best_bid"])
            ask = float(msg["best_ask"])
            mid = round((bid + ask) / 2, 4)
        except (KeyError, ValueError, TypeError):
            return
        _update_price(tid, mid)

    elif event == "last_trade_price":
        tid = msg.get("asset_id", "")
        try:
            price = float(msg["price"])
        except (KeyError, ValueError, TypeError):
            return
        _update_price(tid, price)

    elif event == "book":
        tid = msg.get("asset_id", "")
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        if bids and asks:
            try:
                bb = float(sorted(bids, key=lambda x: float(x["price"]), reverse=True)[0]["price"])
                ba = float(sorted(asks, key=lambda x: float(x["price"]))[0]["price"])
                _update_price(tid, round((bb + ba) / 2, 4))
            except Exception:
                pass

    elif event == "price_change":
        for pc in msg.get("price_changes", []):
            tid = pc.get("asset_id", "")
            if not tid:
                continue
            try:
                bid = float(pc.get("best_bid", 0) or 0)
                ask = float(pc.get("best_ask", 0) or 0)
                if bid > 0 and ask > 0:
                    _update_price(tid, round((bid + ask) / 2, 4))
            except (ValueError, TypeError):
                pass


def _update_price(token_id: str, price: float):
    with _prices_lock:
        _prices[token_id] = price
    _check_triggers(token_id, price)


def _check_triggers(token_id: str, price: float):
    with _subs_lock:
        levels = _subs.get(token_id)
    if levels is None:
        return
    sl_price, tp_price = levels
    reason = None
    if price <= sl_price:
        reason = "SL"
    elif price >= tp_price:
        reason = "TP"

    if reason:
        # Remove subscription to avoid duplicate events
        with _subs_lock:
            _subs.pop(token_id, None)
        try:
            _event_queue.put_nowait((token_id, reason, price))
            log.info("Token WS trigger: %s %s @%.4f", reason, token_id[:16], price)
        except queue.Full:
            log.warning("Token WS event queue full — dropping %s %s", reason, token_id[:16])


# ── WebSocket lifecycle ───────────────────────────────────────────────────────

def _run_loop(shutdown_event):
    global _ws
    import websocket
    delay = 1.0
    while not shutdown_event.is_set():
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            with _ws_lock:
                _ws = ws
            # ping_interval sends PING frames automatically
            ws.run_forever(ping_interval=10, ping_timeout=5)
            delay = min(delay * 2, 30.0)
        except Exception as e:
            log.warning("Token WS run error: %s", e)
        finally:
            with _ws_lock:
                _ws = None

        if not shutdown_event.is_set():
            log.info("Token WS reconnecting in %.0fs", delay)
            shutdown_event.wait(timeout=delay)


def _on_open(ws):
    log.info("Token WS connected to %s", WS_URL)
    # Re-subscribe all active tokens after reconnect
    with _subs_lock:
        tids = list(_subs.keys())
    if tids:
        ws.send(json.dumps({
            "assets_ids": tids,
            "type": "market",
            "custom_feature_enabled": True,
        }))
        log.info("Token WS re-subscribed %d token(s)", len(tids))


def _on_message(ws, message):
    if message in ("PONG", "pong"):
        return
    try:
        data = json.loads(message)
        if isinstance(data, list):
            for item in data:
                _handle_message(item)
        else:
            _handle_message(data)
    except json.JSONDecodeError:
        pass
    except Exception as e:
        log.debug("Token WS message error: %s", e)


def _on_error(ws, error):
    log.warning("Token WS error: %s", error)


def _on_close(ws, close_status_code, close_msg):
    log.info("Token WS closed: %s %s", close_status_code, close_msg)
    with _ws_lock:
        pass  # _ws set to None in _run_loop finally block
