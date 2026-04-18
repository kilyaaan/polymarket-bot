# Crypto Pulse Sniper v5.0 — Design Spec

**Date**: 2026-04-18
**Status**: Draft
**Context**: Refactoring of `crypto_pulse_v4.py` (1795L monolith, already running `--live` with real money on Polymarket BTC 5-min binary options).

---

## 1. Problem Statement

The v4 bot has structural issues that create real financial risk:

- **15+ silent exception swallows** (`except Exception: pass`) — failures are invisible
- **No crash recovery** — process death = orphaned positions on Polymarket
- **Credential leak** — API key prefix printed to stdout
- **Unsanitized URL injection** — token_id concatenated directly into HTTP requests
- **Unbounded thread spawning** — WebSocket reconnect creates threads without limit
- **No structured logging** — monitoring = watching a Rich terminal
- **Fabricated Kelly parameters** — win probability is invented, not calibrated
- **RSI on raw ticks** — noise, not signal (aggTrades arrive 50+/sec)
- **No position correlation** — overlapping 5-min markets treated as independent exposure

The bot must continue operating during refactoring. No behavior changes that could surprise a live deployment.

---

## 2. Goals

1. **Safety first**: eliminate security vulnerabilities and add crash recovery
2. **Observability**: replace silent failures with structured logging
3. **Clean structure**: split into focused modules a beginner can navigate
4. **Strategy improvements**: calibrated sizing, proper RSI, multi-signal direction
5. **Backward compatibility**: same CSV format, same CLI args, same Polymarket API usage

## 3. Non-Goals

- New trading strategies or market types (ETH, SOL, etc.)
- Web UI or REST API
- Database storage (CSV stays)
- Backtesting framework (separate project)
- Over-engineered abstractions (no ABCs, no plugin system)

---

## 4. Architecture

### 4.1 Project Layout

```
~/Bureau/crypto-pulse/
├── pyproject.toml
├── .env.example            # template, no secrets
├── .gitignore
├── README.md
└── src/
    └── pulse/
        ├── __init__.py
        ├── main.py          # ~150L  entrypoint, argparse, thread orchestration, shutdown
        ├── config.py         # ~100L  Settings class, constants, .env loading
        ├── feed.py           # ~200L  BTCFeed, WS Binance, REST fallback, candle accumulator, RSI
        ├── strategy.py       # ~200L  compute_score, kelly_size, direction voting, coherence
        ├── orders.py         # ~250L  CLOB client init, place/close/cancel/redeem, fill polling
        ├── risk.py           # ~100L  circuit breaker, ExpiringBlacklist, position checkpoint (JSON)
        ├── dashboard.py      # ~250L  Rich TUI layout (make_dashboard)
        └── logger.py         # ~80L   Python logging config, CSV trade writer, Telegram notifier
```

Each file has ONE responsibility. A beginner wanting to change the scoring opens `strategy.py`. Wanting to change the display opens `dashboard.py`. No cross-file reasoning required for single-concern changes.

### 4.2 Dependency Flow

```
main.py
  ├── config.py      (imported by all modules)
  ├── logger.py      (imported by all modules)
  ├── feed.py        (depends on: config)
  ├── strategy.py    (depends on: config, feed)
  ├── orders.py      (depends on: config, logger)
  ├── risk.py        (depends on: config, logger)
  └── dashboard.py   (depends on: config, feed, risk)
```

No circular dependencies. `config.py` and `logger.py` are leaf dependencies.

**Shared state**: `BTCFeed` is instantiated as `FEED` in `feed.py` (module-level singleton). Other modules import it via `from pulse.feed import FEED`. This is the simplest pattern for a beginner — explicit import, no dependency injection ceremony. Same pattern for `SETTINGS` in `config.py` and `SCAN` in `main.py`.

---

## 5. Chantier 1 — Security (Priority: CRITICAL)

These fixes must land first. The bot is live.

### 5.1 Input Sanitization

**token_id validation** — before any URL construction:
```python
import re
_TOKEN_ID_RE = re.compile(r"^[a-fA-F0-9]{64,}$")

def validate_token_id(tid: str) -> str:
    if not _TOKEN_ID_RE.match(tid):
        raise ValueError(f"Invalid token_id format: {tid[:16]}...")
    return tid
```

Applied in: `get_ob()`, `place_order()`, `close_position()`, `get_fee_rate()`.

**URL construction** — all API calls must use `params=` (requests library URL-encodes automatically), never f-string interpolation:
```python
# WRONG: f"{HOST}/book?token_id={token_id}"
# RIGHT: HTTP.get(f"{HOST}/book", params={"token_id": validate_token_id(token_id)})
```

### 5.2 Credential Hygiene

- Remove all `print()` of API keys, even partial
- Log credential events at DEBUG level only: `logger.debug("CLOB creds loaded")`
- Never log key material, even truncated

### 5.3 Order Validation Gate

In `orders.py`, before sending any order to Polymarket:
```python
def _validate_order(price: float, size_usdc: float):
    if not (0.01 <= price <= 0.99):
        raise ValueError(f"Price out of bounds: {price}")
    if not (MIN_POS <= size_usdc <= MAX_POS):
        raise ValueError(f"Size out of bounds: {size_usdc}")
```

### 5.4 Crash Recovery

`risk.py` maintains a `positions.json` checkpoint:
- **Write**: after every position open or close
- **Read**: at startup, reconcile with Polymarket API (check if positions still exist)
- **Format**: list of position dicts with all fields needed to reconstruct `Position` objects
- **Atomic write**: write to `.positions.json.tmp`, then `os.replace()` to `positions.json`

**Startup reconciliation failure modes**:
- API down at startup: load positions from checkpoint, log WARNING, mark as `unverified`. Re-verify on first successful API call.
- Position exists in checkpoint but market has expired: log the position as `MISSED_EXPIRY` in CSV, remove from active positions, alert Telegram.
- Position exists on-chain but not in checkpoint: ignore (could be from another bot or manual trade). Log INFO for awareness.

### 5.5 Graceful Shutdown

In `main.py`:
- Register `signal.signal(SIGINT, ...)` and `signal.signal(SIGTERM, ...)`
- On signal: set `_SHUTDOWN_EVENT`, cancel pending orders, flush `positions.json`, shutdown `ThreadPoolExecutor`, restore terminal (tty)
- Timeout: if shutdown takes > 10s, force exit

---

## 6. Chantier 2 — Structure & Reliability

### 6.1 Structured Logging (`logger.py`)

```python
import logging
from logging.handlers import RotatingFileHandler

def setup_logging(level: str = "INFO"):
    fmt = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    logging.basicConfig(level=level, format=fmt)
    # File handler with rotation
    fh = RotatingFileHandler("pulse.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)
```

Every module: `log = logging.getLogger(__name__)`.

Telegram notifier stays but uses the logger — `log.info()` for normal trades, Telegram for actionable alerts only (entries, exits, circuit breaker).

### 6.2 Exception Taxonomy

Replace all `except Exception: pass` with categorized handling:

| Category | Action | Example |
|----------|--------|---------|
| **Network** (ConnectionError, Timeout) | Log WARNING, retry with backoff | WS disconnect, API timeout |
| **Data** (ValueError, KeyError, JSONDecodeError) | Log ERROR, skip iteration | Malformed API response |
| **Critical** (auth failure, order rejection) | Log CRITICAL, alert Telegram, stop if repeated | CLOB auth expired |

No exception is ever silently swallowed.

### 6.3 Bounded Thread Management

**WebSocket reconnect**:
- Max 5 consecutive reconnect attempts
- After 5 failures: log CRITICAL, switch to REST-only mode, alert Telegram
- Reset counter on successful connection
- No new thread per reconnect — reuse the WS thread with a retry loop

**ThreadPoolExecutor**:
- Wrapped in context manager, shutdown on exit
- `max_workers=8` stays (order book fetching is I/O bound)

### 6.4 Thread-Safe Wallet State

```python
_wallet_lock = threading.Lock()
_cached_wallet_usdc = 0.0
_last_wallet_sync = 0.0

def sync_wallet_usdc(force=False) -> float:
    with _wallet_lock:
        # ... existing logic ...
```

### 6.5 Fill Verification

After `place_order()` returns in live mode:
- Poll for actual fill quantity
- Store `actual_shares_filled` on the Position, not the locally computed estimate
- If partial fill: adjust `size_usdc` and `shares_held` to reflect reality
- If no fill after timeout: cancel order, don't create Position

---

## 7. Chantier 3 — Strategy Improvements

### 7.1 RSI on 1-Second Candles

In `feed.py`, add a candle accumulator:
```python
# Accumulate ticks into 1-second OHLC candles
# RSI computed on candle closes, not raw ticks
# Buffer: 120 candles (2 minutes of data)
```

RSI period stays configurable (default 7). The change is the input data quality, not the algorithm.

### 7.2 Calibrated Win Probability

Replace the fabricated `0.45 + score * 0.20` with historical calibration:

```python
def calibrated_win_prob(score: float, csv_path: Path) -> float:
    """
    Bucket scores into [0.5-0.6), [0.6-0.7), [0.7-0.8), [0.8+)
    Compute actual win rate per bucket from CSV history.
    Fallback to 0.50 if < 30 samples in bucket.
    """
```

This means Kelly sizing improves automatically as the bot accumulates data. Early trades use conservative 0.50; later trades use measured edge.

**Cold-start behavior**: when a bucket has < 30 samples, use fallback `win_prob = 0.50` (yields near-`MIN_POS` sizing — conservative by design). Log which path was used: `log.info("kelly: bucket=0.6-0.7 source=calibrated p=0.58 n=47")` or `log.info("kelly: bucket=0.6-0.7 source=fallback p=0.50 n=12")`. This makes the calibrated-vs-fallback transition auditable in logs.

### 7.3 Direction Voting

Replace `directions = ["UP" if m60 >= 0 else "DOWN"]` with:

```python
def vote_direction(m15: float, m30: float, m60: float) -> str:
    votes = sum(1 for m in [m15, m30, m60] if m > 0)
    return "UP" if votes >= 2 else "DOWN"
```

Simple majority. No single timeframe dominates.

### 7.4 Magnitude-Weighted Coherence

Replace binary sign check with proportional bonus:

```python
def coherence_bonus(m15, m30, m60, direction):
    moms = [m15, m30, m60] if direction == "UP" else [-m15, -m30, -m60]
    if all(m > 0 for m in moms):
        return min(0.12, 0.08 * min(abs(m15)/MOM_15S_REF, abs(m30)/MOM_30S_REF, abs(m60)/MOM_60S_REF))
    return 0.0
```

### 7.5 Position Correlation Check

Before opening a new position, check temporal overlap:

```python
def has_overlapping_position(positions: list, new_market, direction: str) -> bool:
    for p in positions:
        if p.direction == direction:
            # Markets overlap if their [start, end] windows intersect
            if p.market.start_time < new_market.end_time and new_market.start_time < p.market.end_time:
                return True
    return False
```

Overlapping same-direction positions count as ONE position for `MAX_DIR_POS`.

---

## 8. Migration Path

1. Create project structure, copy v4 code into modules
2. Apply Chantier 1 fixes (security) — test in `--dry` mode
3. Apply Chantier 2 fixes (structure) — test in `--dry` mode
4. Apply Chantier 3 fixes (strategy) — test in `--dry`, compare CSV output vs v4
5. Run v5 in `--dry` alongside v4 `--live` for 24h — compare decisions
6. Switch to v5 `--live`

No big bang. The v4 bot keeps running live until v5 is validated.

**Acceptance criterion for step 5**: over 24h dry-run, v5 must produce >= 90% of the same entry/exit decisions as v4 on identical markets (direction + condition_id match). Divergences from strategy improvements (direction voting, coherence) are expected and logged but must not exceed 20% of total decisions. If divergence > 20%, investigate before switching to live.

---

## 9. Success Criteria

- Zero `except Exception: pass` in codebase
- `positions.json` checkpoint tested: kill process, restart, positions recovered
- All token_ids validated before URL injection
- No credentials in stdout/logs at INFO level
- RSI computed on candle closes (verifiable by logging candle count vs tick count)
- Kelly win_prob sourced from CSV after 30+ trades per bucket
- Direction uses 3-signal vote (verifiable in trade log)
- Each module < 300 lines
