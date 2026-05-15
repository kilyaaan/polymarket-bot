"""Configuration — constants, Settings class, .env loading."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

load_dotenv()

# ── Wallet / API ─────────────────────────────────────────────────────────────
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
USDC_POLYGON = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD (Polymarket USD, migré le 28/04/2026)

HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# ── Trading parameters ───────────────────────────────────────────────────────
BANKROLL = 200.0
MAX_OPEN_POS = 8
MAX_DIR_POS = 3
MIN_POS = 3.5
MAX_POS = 18.0
SCAN_INTERVAL = 1
SYNC_WALLET_EVERY = 5

ENTRY_WINDOW_MIN = 0.3
ENTRY_WINDOW_MAX = 4.0

# v6 -- hold-to-expiry pur, entree sur tokens bas uniquement
# Backtest 30j BTC 1s : entree 0.37-0.50 = +2165$ vs 0.50-0.64 = -855$
TP_DELTA = 0.27          # TP a ~entry+0.27 (~0.75 pour entry 0.48)
SL_DELTA = 0.15          # SL a entry-0.15 (ex: entree 0.50 -> SL 0.35)
HOLD_THRESHOLD = 0.01    # active hold-to-expiry immediatement
HOLD_MIN_REMAINING = 0.0 # pas de contrainte de temps pour activer hold
HOLD_ENABLED = True

MIN_ENTRY_PRICE = 0.37
MAX_ENTRY_PRICE = 0.50   # uniquement tokens bas (edge positif backtest)
MAX_SPREAD = 0.03
TRAILING_STOP = False    # pas de trailing -- on tient jusqu'a expiry
TRAILING_DISTANCE = 0.99

MAX_DAILY_LOSS = 50.0
DEFAULT_TAKER_FEE_RATE = 0.02

# ── Scoring parameters ───────────────────────────────────────────────────────
SPIKE_THRESHOLD = 0.06
SPIKE_DISPLAY_THRESHOLD = 0.02
MOM_15S_REF = 0.05
MOM_30S_REF = 0.10
MOM_60S_REF = 0.20
MIN_SCORE = 0.64
BTC_TREND_THRESHOLD = 0.10  # % BTC momentum 60s max contre la direction d'entree
MIN_MOM_GLOBAL = 0.001
RSI_PERIOD = 7
RSI_OVERBOUGHT = 65.0
RSI_OVERSOLD = 35.0
MIN_DEPTH_USDC = 50.0

# ── Binance feed ─────────────────────────────────────────────────────────────
BTC_CFG = {
    "ws_stream": "btcusdt@aggTrade",
    "rest": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
    "slug_pattern": r"btc-updown-5m-\d+",
}

# ── Paths ────────────────────────────────────────────────────────────────────
TRADES_CSV = Path("crypto_trades_btc_v6.csv")
POSITIONS_CHECKPOINT = Path("positions.json")

# ── Shutdown event (shared across all modules) ───────────────────────────────
SHUTDOWN_EVENT = threading.Event()


# ── Thread-safe runtime settings ─────────────────────────────────────────────
class Settings:
    """Mutable runtime settings, adjustable via keyboard or CLI."""

    def __init__(self):
        self.min_score = MIN_SCORE
        self.auto_score = False
        self.entry_win_min = ENTRY_WINDOW_MIN
        self.entry_win_max = ENTRY_WINDOW_MAX
        self._scan_interval = SCAN_INTERVAL
        self._max_daily_loss = MAX_DAILY_LOSS
        self._lock = threading.Lock()

    # ── Score adjustments ────────────────────────────────────────────────
    def increase(self):
        with self._lock:
            self.min_score = min(round(self.min_score + 0.05, 2), 0.90)
            self.auto_score = False

    def decrease(self):
        with self._lock:
            self.min_score = max(round(self.min_score - 0.05, 2), 0.20)
            self.auto_score = False

    def reset(self):
        with self._lock:
            self.min_score = MIN_SCORE
            self.auto_score = False

    def toggle_auto(self):
        with self._lock:
            self.auto_score = not self.auto_score

    def auto_update(self, best: float):
        with self._lock:
            if not self.auto_score or best <= 0:
                return
            self.min_score = min(0.70, max(0.35, round(best * 0.92, 2)))

    # ── Accessors ────────────────────────────────────────────────────────
    @property
    def scan_interval(self) -> int:
        with self._lock:
            return self._scan_interval

    @scan_interval.setter
    def scan_interval(self, val: int):
        with self._lock:
            self._scan_interval = max(1, val)

    @property
    def max_daily_loss(self) -> float:
        with self._lock:
            return self._max_daily_loss

    @max_daily_loss.setter
    def max_daily_loss(self, val: float):
        with self._lock:
            self._max_daily_loss = val

    @property
    def thresholds(self) -> Tuple[float, float, float]:
        with self._lock:
            return self.min_score, self.entry_win_min, self.entry_win_max

    @property
    def display(self) -> str:
        with self._lock:
            s = f"{self.min_score:.2f}"
            if self.auto_score:
                return f"[bold yellow]{s} AUTO[/]"
            if self.min_score <= 0.35:
                return f"[bold red]{s}[/]"
            if self.min_score <= 0.55:
                return f"[bold yellow]{s}[/]"
            return f"[bold green]{s}[/]"


# ── Dataclasses ──────────────────────────────────────────────────────────────
@dataclass
class CryptoMarket:
    condition_id: str
    question: str
    slug: str
    yes_token: str
    no_token: str
    start_time: float
    end_time: float
    start_price: float = 0.0
    crypto: str = "BTC"

    @property
    def elapsed_min(self) -> float:
        import time
        return (time.time() - self.start_time) / 60

    @property
    def remaining_min(self) -> float:
        import time
        return max(0.0, (self.end_time - time.time()) / 60)

    @property
    def remaining_sec(self) -> float:
        import time
        return max(0.0, self.end_time - time.time())


@dataclass
class Position:
    market: CryptoMarket
    token_id: str
    direction: str
    entry_price: float
    size_usdc: float
    shares_held: float
    order_id: str
    entry_time: float = field(default_factory=lambda: __import__("time").time())
    current_price: float = 0.0
    entry_crypto: float = 0.0
    score: float = 0.0
    peak_price: float = 0.0
    trail_sl: float = 0.0
    mom15_at_entry: float = 0.0
    mom30_at_entry: float = 0.0
    mom60_at_entry: float = 0.0
    rsi_at_entry: float = 50.0
    kelly_used: float = 0.0
    fee_rate_used: float = DEFAULT_TAKER_FEE_RATE
    close_order_id: str = ""
    close_fill: str = ""
    sl_order_id: str = ""
    holding_expiry: bool = False
    awaiting_resolution: bool = False
    verified: bool = True
    # Extended analytics fields
    spread_at_entry: float = 0.0
    ob_depth_at_entry: float = 0.0
    spike_size_at_entry: float = 0.0
    trough_price: float = 0.0
    session_pnl_before: float = 0.0
    market_remaining_min_at_entry: float = 0.0
    window_delta_at_entry: float = 0.0

    @property
    def pnl_gross(self) -> float:
        return (self.current_price - self.entry_price) * self.shares_held

    @property
    def pnl_pct(self) -> float:
        return (self.current_price - self.entry_price) / max(self.entry_price, 0.001)

    @property
    def elapsed_min(self) -> float:
        import time
        return (time.time() - self.entry_time) / 60


@dataclass
class SessionStats:
    start_time: float = field(default_factory=lambda: __import__("time").time())
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    total: int = 0
    scans: int = 0
    snipes: int = 0
    spikes_seen: int = 0
    btc_pnl: float = 0.0
    btc_w: int = 0
    btc_l: int = 0
    held_expiry: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.total * 100 if self.total else 0.0

    @property
    def elapsed(self) -> str:
        import time
        s = int(time.time() - self.start_time)
        h, m = divmod(s, 3600)
        m, s2 = divmod(m, 60)
        return f"{h}h{m:02d}m{s2:02d}s"


# ── Module-level singleton ───────────────────────────────────────────────────
SETTINGS = Settings()
