"""Orders — CLOB client, token validation, place/close/cancel/redeem."""

from __future__ import annotations

import logging
import math
import os
import re
import time
import threading
from typing import Dict, List, Optional, Tuple

from pulse.config import (
    HOST, GAMMA_API, SHUTDOWN_EVENT,
    DEFAULT_TAKER_FEE_RATE, MIN_POS, MAX_POS,
    MIN_DEPTH_USDC, BTC_CFG, WALLET_ADDRESS, POLYGON_RPC, USDC_POLYGON,
    SYNC_WALLET_EVERY, BANKROLL, CryptoMarket,
)
from pulse.feed import FEED

log = logging.getLogger(__name__)

# ── HTTP session ─────────────────────────────────────────────────────────────
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


# ── Token ID validation ─────────────────────────────────────────────────────
_TOKEN_ID_RE = re.compile(r"^[a-fA-F0-9]{64,}$")


def validate_token_id(tid: str) -> str:
    """Validate token_id format. Raises ValueError if invalid."""
    if not _TOKEN_ID_RE.match(tid):
        raise ValueError(f"Invalid token_id: {tid[:20]}...")
    return tid


def _validate_order(price: float, size_usdc: float):
    """Gate: reject orders with nonsensical parameters."""
    if not (0.01 <= price <= 0.99):
        raise ValueError(f"Price out of bounds: {price}")
    if not (MIN_POS <= size_usdc <= MAX_POS):
        raise ValueError(f"Size out of bounds: {size_usdc}")


# ── Tick size cache ──────────────────────────────────────────────────────────
TICK_SIZE_CACHE: Dict[str, str] = {}


def get_tick_size(token_id: str) -> str:
    """Fetch and cache the minimum tick size for a token. Returns string e.g. '0.01'."""
    if token_id in TICK_SIZE_CACHE:
        return TICK_SIZE_CACHE[token_id]
    try:
        validate_token_id(token_id)
        r = _get_http().get(
            f"{HOST}/tick-size",
            params={"token_id": token_id},
            timeout=2,
        )
        if r.status_code == 200:
            ts = str(r.json().get("minimum_tick_size", "0.01"))
            TICK_SIZE_CACHE[token_id] = ts
            log.debug("Tick size for %s: %s", token_id[:16], ts)
            return ts
    except ValueError:
        log.error("Invalid token_id for tick size: %s", token_id[:20])
    except Exception as e:
        log.warning("Tick size fetch error: %s", e)
    TICK_SIZE_CACHE[token_id] = "0.01"
    return "0.01"


# ── Fee rate cache ───────────────────────────────────────────────────────────
FEE_RATE_CACHE: Dict[str, float] = {}


def get_fee_rate(token_id: str) -> float:
    if not token_id:
        return DEFAULT_TAKER_FEE_RATE
    if token_id in FEE_RATE_CACHE:
        return FEE_RATE_CACHE[token_id]
    try:
        validate_token_id(token_id)
        r = _get_http().get(
            f"{HOST}/fee-rate",
            params={"token_id": token_id},
            timeout=2,
        )
        if r.status_code == 200:
            d = r.json()
            # API returns {"base_fee": 1000} where 1000 bps / 50000 = 0.02 (2%)
            raw = (d.get("base_fee") or d.get("fee_rate_bps")
                   or d.get("fee_rate") or d.get("feeRate"))
            rate = None
            if raw is not None:
                rate = float(raw) / 50000.0
                # Sanity check: plausible fee range is 0.1 % – 10 %
                if not (0.001 <= rate <= 0.10):
                    log.warning("Fee rate out of bounds %.4f (raw=%s) — using default", rate, raw)
                    rate = None
            if rate is not None:
                FEE_RATE_CACHE[token_id] = rate
                return rate
    except ValueError:
        log.error("Invalid token_id for fee rate: %s", token_id[:20])
    except Exception as e:
        log.warning("Fee rate fetch error: %s", e)
    FEE_RATE_CACHE[token_id] = DEFAULT_TAKER_FEE_RATE
    return DEFAULT_TAKER_FEE_RATE


# ── Order book ───────────────────────────────────────────────────────────────
def get_ob(token_id: str, _retry: bool = True) -> Optional[dict]:
    """Fetch order book with 1 retry on empty book."""
    try:
        validate_token_id(token_id)
        r = _get_http().get(
            f"{HOST}/book",
            params={"token_id": token_id},
            timeout=2,
        )
        if r.status_code != 200:
            log.debug("OB fetch %d for %s", r.status_code, token_id[:16])
            return None
        book = r.json()
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)[:5]
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))[:5]
        if not bids or not asks:
            if _retry:
                time.sleep(0.2)
                return get_ob(token_id, _retry=False)
            return None
        bb = float(bids[0]["price"])
        ba = float(asks[0]["price"])
        bd = sum(float(b["price"]) * float(b["size"]) for b in bids)
        ad = sum(float(a["price"]) * float(a["size"]) for a in asks)
        total = round(bd + ad, 2)
        if total < MIN_DEPTH_USDC:
            return None
        return {
            "mid": round((bb + ba) / 2, 4), "bb": bb, "ba": ba,
            "bid_d": round(bd, 2), "ask_d": round(ad, 2),
            "spread": round(ba - bb, 4), "total_d": total,
        }
    except ValueError as e:
        log.error("OB validation error: %s", e)
        return None
    except Exception as e:
        log.warning("OB fetch error: %s", e)
        return None


from concurrent.futures import ThreadPoolExecutor, as_completed

_OB_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ob_fetch")


def get_ob_multi(token_ids: List[str]) -> Dict[str, Optional[dict]]:
    futures = {_OB_POOL.submit(get_ob, tid): tid for tid in token_ids}
    results: Dict[str, Optional[dict]] = {}
    try:
        for fut in as_completed(futures, timeout=2.5):
            tid = futures[fut]
            try:
                results[tid] = fut.result()
            except Exception as e:
                log.debug("OB multi error for %s: %s", tid[:16], e)
                results[tid] = None
    except TimeoutError:
        # Fill None for any tokens that didn't complete in time
        for tid in futures.values():
            if tid not in results:
                log.debug("OB multi timeout for %s", tid[:16])
                results[tid] = None
    return results


def shutdown_ob_pool():
    _OB_POOL.shutdown(wait=False)


# ── CLOB client ──────────────────────────────────────────────────────────────
_CLOB_CLIENT = None
_clob_lock = threading.Lock()


def get_clob_client():
    global _CLOB_CLIENT
    if _CLOB_CLIENT is not None:
        return _CLOB_CLIENT
    with _clob_lock:
        if _CLOB_CLIENT is not None:  # double-checked under lock
            return _CLOB_CLIENT
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds
            from py_clob_client_v2.constants import POLYGON

            pk = os.getenv("PRIVATE_KEY", "").strip()
            if not pk:
                log.error("PRIVATE_KEY missing in .env")
                return None

            sig_type = int(os.getenv("CLOB_SIGNATURE_TYPE", "1"))
            funder = (os.getenv("FUNDER", "") or os.getenv("WALLET_ADDRESS", "")).strip()

            if sig_type == 1 and funder:
                cl = ClobClient(HOST, key=pk, chain_id=POLYGON,
                                signature_type=1, funder=funder)
            else:
                cl = ClobClient(HOST, key=pk, chain_id=POLYGON,
                                signature_type=0)

            api_key = os.getenv("CLOB_API_KEY", "").strip()
            api_sec = os.getenv("CLOB_SECRET", "").strip()
            api_pass = os.getenv("CLOB_PASSPHRASE", "").strip()

            if api_key and api_sec and api_pass:
                cl.set_api_creds(ApiCreds(
                    api_key=api_key, api_secret=api_sec, api_passphrase=api_pass))
                log.info("CLOB creds loaded from env")
            else:
                log.info("CLOB deriving creds automatically")
                creds = cl.create_or_derive_api_key()
                cl.set_api_creds(creds)
                log.info("CLOB creds derived")
            # Assign global only after creds are fully set — prevents a
            # half-initialised client being returned on key-derivation failure.
            _CLOB_CLIENT = cl
            return _CLOB_CLIENT
        except Exception as e:
            log.error("CLOB init error: %s", e)
            _CLOB_CLIENT = None  # ensure no partial client leaks out
            return None


# ── Pending order tracking ───────────────────────────────────────────────────
_pending_lock = threading.Lock()
_pending_orders: List[str] = []


def _track_order(order_id: str):
    with _pending_lock:
        _pending_orders.append(order_id)


def _untrack_order(order_id: str):
    with _pending_lock:
        try:
            _pending_orders.remove(order_id)
        except ValueError:
            pass


def cancel_order_safe(order_id: str) -> bool:
    """Cancel a single order. Returns True if successful."""
    if not order_id:
        return False
    cl = get_clob_client()
    if cl is None:
        return False
    try:
        from py_clob_client_v2.clob_types import OrderPayload
        cl.cancel_order(OrderPayload(orderID=order_id))
        _untrack_order(order_id)
        log.info("Cancelled order %s", order_id[:16])
        return True
    except Exception as e:
        log.warning("Cancel order %s failed: %s", order_id[:16], e)
        return False


def cancel_all_pending():
    """Cancel all pending orders (called at shutdown)."""
    cl = get_clob_client()
    if cl is None:
        return
    with _pending_lock:
        orders = list(_pending_orders)
    for oid in orders:
        try:
            from py_clob_client_v2.clob_types import OrderPayload
            cl.cancel_order(OrderPayload(orderID=oid))
            log.info("Cancelled pending order %s", oid[:16])
        except Exception as e:
            log.warning("Cancel order %s failed: %s", oid[:16], e)


# ── Order execution ─────────────────────────────────────────────────────────
def _retry_order(fn, retries: int = 3):
    for attempt in range(retries):
        result = fn()
        if result is not None:
            return result
        if attempt < retries - 1:
            time.sleep(0.5 * (2 ** attempt))
    return None


def place_order(token_id: str, price: float, size_usdc: float,
                dry: bool = True) -> Optional[str]:
    """Place a BUY order. Returns order ID or None.

    Only retries on network exceptions — never retries after a POST was sent,
    to avoid placing multiple orders on the same token.
    """
    validate_token_id(token_id)
    _validate_order(price, size_usdc)

    if dry:
        return f"dry_{int(time.time() * 1000)}"

    from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY

    for attempt in range(3):
        cl = get_clob_client()
        if cl is None:
            return None
        try:
            shares = math.ceil(size_usdc / max(price, 0.001) * 10000) / 10000
            resp = cl.create_and_post_order(
                OrderArgsV2(token_id=token_id, price=price, size=shares, side=BUY),
                options=PartialCreateOrderOptions(tick_size=get_tick_size(token_id)))
            # POST was sent — do NOT retry regardless of response shape
            if not resp or not isinstance(resp, dict):
                log.error("BUY unexpected response (order may have been placed): %s", resp)
                return None
            oid = resp.get("orderID")
            if not oid:
                log.error("BUY rejected — no orderID: %s", resp)
                return None
            _track_order(oid)
            log.info("Order placed: %s price=%.3f size=%.2f$", oid[:16], price, size_usdc)
            return oid
        except Exception as e:
            log.error("Place order error (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
            # Only retry on exception (POST may not have been sent)
    return None


def poll_order_status(order_id: str, timeout: float = 10.0) -> Tuple[str, Optional[float]]:
    """Poll order fill status. Returns (status, filled_shares)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = _get_http().get(f"{HOST}/order/{order_id}", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "").lower()
                if status in ("matched", "filled"):
                    _untrack_order(order_id)
                    filled = data.get("size_matched") or data.get("sizeMatched")
                    return "filled", float(filled) if filled else None
                if status == "partial":
                    filled = data.get("size_matched") or data.get("sizeMatched")
                    return "partial", float(filled) if filled else None
                if status in ("cancelled", "canceled"):
                    _untrack_order(order_id)
                    return "cancelled", None
                # "live" / "delayed" / "unmatched" = resting, keep polling
        except Exception as e:
            log.debug("Poll order %s error: %s", order_id[:16], e)
        time.sleep(0.5)
    return "open", None


def place_limit_sell(token_id: str, price: float, shares: float,
                    dry: bool = True) -> Optional[str]:
    """Place a resting SELL limit order in the CLOB without waiting for fill.
    Used to pre-place the stop-loss order immediately after entry."""
    validate_token_id(token_id)
    if not (0.01 <= price <= 0.99) or shares <= 0:
        return None
    if dry:
        return f"dry_sl_{int(time.time() * 1000)}"
    try:
        from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import SELL
        cl = get_clob_client()
        if cl is None:
            return None
        resp = cl.create_and_post_order(
            OrderArgsV2(token_id=token_id, price=price,
                        size=round(shares, 4), side=SELL),
            options=PartialCreateOrderOptions(tick_size=get_tick_size(token_id)))
        if not resp or not isinstance(resp, dict):
            return None
        oid = resp.get("orderID")
        if not oid:
            log.error("SL order rejected — no orderID: %s", resp)
            return None
        _track_order(oid)
        log.info("SL order pre-placed: %s price=%.3f shares=%.4f",
                 oid[:16], price, shares)
        return oid
    except Exception as e:
        err = str(e).lower()
        if "balance" in err and ("balance: 0" in err or "not enough balance" in err):
            log.error("Place SL order rejected — no balance (BUY not filled): %s", e)
            return "no_balance"
        log.error("Place SL order error: %s", e)
        return None


def close_position(token_id: str, price: float, shares_held: float,
                   dry: bool = True) -> Tuple[Optional[str], str, Optional[float]]:
    """Place a SELL order to close a position.
    Returns (order_id, fill_status, filled_shares).

    One-shot — never retries. Retrying after a POST would risk a duplicate
    SELL (naked short) if the TCP packet was already sent when the exception
    fired. The stale-order check in main.py handles recovery on the next scan.
    """
    validate_token_id(token_id)
    if not (0.01 <= price <= 0.99):
        raise ValueError(f"SELL price out of bounds: {price}")
    if shares_held <= 0:
        raise ValueError(f"SELL shares invalid: {shares_held}")

    if dry:
        return f"dry_close_{int(time.time() * 1000)}", "filled", shares_held

    cl = get_clob_client()
    if cl is None:
        return None, "failed", None

    from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import SELL

    try:
        resp = cl.create_and_post_order(
            OrderArgsV2(token_id=token_id, price=price,
                        size=round(shares_held, 4), side=SELL),
            options=PartialCreateOrderOptions(tick_size=get_tick_size(token_id)))
        # POST was sent — do NOT retry regardless of response shape
        if not resp or not isinstance(resp, dict):
            log.error("SELL unexpected response (order may have been placed): %s", resp)
            return None, "unknown", None
        oid = resp.get("orderID")
        if not oid:
            log.error("Close order rejected — no orderID: %s", resp)
            return None, "rejected", None
        _track_order(oid)
        log.info("Close order placed: %s price=%.3f shares=%.4f", oid[:16], price, shares_held)
        fill_status, filled_shares = poll_order_status(oid, timeout=25.0)
        _untrack_order(oid)
        return oid, fill_status, filled_shares
    except Exception as e:
        err = str(e).lower()
        if "balance" in err and ("balance: 0" in err or "not enough balance" in err):
            # No tokens held — BUY order was never filled (phantom position)
            log.error("Close rejected — no balance (phantom position): %s", e)
            return None, "no_balance", None
        if "400" in err or "bad request" in err:
            # Other CLOB rejection (market locked, etc.)
            log.error("Close rejected by CLOB (400): %s", e)
            return None, "rejected", None
        log.error("Close position error: %s", e)
        return None, "failed", None


# ── Redemption ───────────────────────────────────────────────────────────────
# Polymarket automatically settles resolved binary option positions to USDC
# on-chain. The py-clob-client has no redeem_positions method. Manual
# redemption would require calling the CTF contract directly via web3.
# sync_wallet_usdc() picks up the settled balance automatically.

def redeem_loop():
    """No-op: Polymarket auto-settles resolved positions to USDC on-chain."""
    log.debug("redeem_loop: Polymarket handles settlement automatically")
    SHUTDOWN_EVENT.wait()


# ── Wallet balance ───────────────────────────────────────────────────────────
_wallet_lock = threading.Lock()
_cached_wallet_usdc = 0.0
_last_wallet_sync = 0.0


def get_wallet_address() -> str:
    if WALLET_ADDRESS:
        return WALLET_ADDRESS
    try:
        from eth_account import Account
        pk = os.getenv("PRIVATE_KEY", "").strip()
        return Account.from_key(pk).address if pk else ""
    except Exception:
        return ""


def get_polygon_usdc_balance() -> float:
    addr = get_wallet_address()
    if not addr:
        return 0.0
    try:
        data = "0x70a08231" + addr[2:].lower().zfill(64)
        payload = {
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": USDC_POLYGON, "data": data}, "latest"], "id": 1,
        }
        r = _get_http().post(POLYGON_RPC, json=payload, timeout=4)
        return int(r.json().get("result", "0x0"), 16) / 1e6
    except Exception as e:
        log.warning("Polygon USDC balance error: %s", e)
        return 0.0


def sync_wallet_usdc(force: bool = False) -> float:
    global _cached_wallet_usdc, _last_wallet_sync
    now = time.time()
    with _wallet_lock:
        if force or (now - _last_wallet_sync >= SYNC_WALLET_EVERY):
            bal = get_polygon_usdc_balance()
            if bal >= 0:
                _cached_wallet_usdc = float(bal)
                _last_wallet_sync = now
        return _cached_wallet_usdc


# ── Market fetching ──────────────────────────────────────────────────────────
_mkt_cache: List = []
_mkt_cache_lock = threading.Lock()
_mkt_start_price: Dict[str, float] = {}  # condition_id → BTC price at first parse


def parse_ts(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        from datetime import datetime
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        elif "+" not in s[-7:] and len(s) > 10:
            s += "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _parse_market(m: dict, now_ts: float) -> Optional[CryptoMarket]:
    cid = m.get("conditionId", "")
    if not cid:
        return None
    end_ts = (parse_ts(m.get("endDate"))
              or parse_ts(m.get("end_date_iso"))
              or parse_ts(m.get("endDateIso")))
    if not end_ts:
        m2 = re.search(r"-(\d{9,11})$", m.get("slug", ""))
        if m2:
            end_ts = int(m2.group(1))
    if not end_ts:
        return None
    if end_ts < now_ts - 15 or end_ts > now_ts + 330:
        return None
    try:
        r = _get_http().get(f"{HOST}/markets/{cid}", timeout=4)
        if r.status_code != 200:
            return None
        tokens = r.json().get("tokens", [])
        up = next((t["token_id"] for t in tokens if any(
            x in t.get("outcome", "").upper() for x in ["UP", "YES", "HAUSSE"])), None)
        dn = next((t["token_id"] for t in tokens if any(
            x in t.get("outcome", "").upper() for x in ["DOWN", "NO", "BAISSE"])), None)
        if not up or not dn:
            return None
    except Exception as e:
        log.debug("Market parse error for %s: %s", cid[:16], e)
        return None
    start_time = end_ts - 300
    # start_price: only set on first parse — never overwritten on later prefetch cycles
    # so window_delta remains consistent across scans for the same market.
    if cid not in _mkt_start_price and FEED.current > 0:
        _mkt_start_price[cid] = FEED.current
    start_price = _mkt_start_price.get(cid, 0.0)
    return CryptoMarket(
        condition_id=cid, question=m.get("question", "")[:55],
        slug=m.get("slug", ""), yes_token=up, no_token=dn,
        start_time=start_time, end_time=end_ts,
        start_price=start_price,
    )


def fetch_markets_btc() -> List[CryptoMarket]:
    slug_re = re.compile(BTC_CFG["slug_pattern"], re.IGNORECASE)
    now_ts = time.time()
    out: List[CryptoMarket] = []
    for off in range(0, 3):
        slot = int((now_ts // 300 + off) * 300)
        slug = f"btc-updown-5m-{slot}"
        try:
            r = _get_http().get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=4)
            if r.status_code != 200:
                continue
            data = r.json()
            if not data:
                continue
            for m in (data if isinstance(data, list) else [data]):
                mkt = _parse_market(m, now_ts)
                if mkt:
                    out.append(mkt)
        except Exception as e:
            log.debug("Market fetch error for %s: %s", slug, e)
    if out:
        return out
    # Fallback by volume
    try:
        r = _get_http().get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false",
                    "order": "volume", "ascending": "false", "limit": "100"},
            timeout=6,
        )
        r.raise_for_status()
        for m in r.json():
            sl = m.get("slug", "").lower()
            q = m.get("question", "").lower()
            if not (slug_re.search(sl) or "btc" in q or "btc" in sl):
                continue
            if not any(x in q or x in sl for x in ["up", "down", "hausse", "baisse"]):
                continue
            mkt = _parse_market(m, now_ts)
            if mkt:
                out.append(mkt)
    except Exception as e:
        log.warning("Market fallback error: %s", e)
    return out


def prefetch_loop(settings):
    """Background thread: refresh market cache."""
    global _mkt_cache
    while not SHUTDOWN_EVENT.is_set():
        try:
            mkts = fetch_markets_btc()
            with _mkt_cache_lock:
                _mkt_cache = mkts
        except Exception as e:
            log.warning("Prefetch error: %s", e)
        time.sleep(max(1.5, settings.scan_interval / 1.5))


def get_cached_markets() -> List[CryptoMarket]:
    with _mkt_cache_lock:
        return list(_mkt_cache)
