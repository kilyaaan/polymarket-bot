# Strategy Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the Polymarket BTC 5-min bot win rate by reweighting signals toward the oracle-lag edge, converting RSI to a binary veto, gating OB imbalance, adding a macro trend filter, tightening the entry window, and narrowing the price band.

**Architecture:** Changes are spread across config.py (constants), strategy.py (scoring logic), feed.py (macro trend data), and main.py (RSI veto + macro filter at entry). No new files needed.

**Tech Stack:** Python 3, numpy, existing BTCFeed/compute_score/kelly_size infrastructure.

---

### Task 1: Config constants — tighten entry window and price band

**Files:**
- Modify: `src/pulse/config.py`

- [ ] **Step 1: Update constants**

In `src/pulse/config.py`, change:
```python
# was: ENTRY_WINDOW_MAX = 3.0
ENTRY_WINDOW_MAX = 2.0

# was: MIN_ENTRY_PRICE = 0.37
MIN_ENTRY_PRICE = 0.40

# was: MAX_ENTRY_PRICE = 0.63
MAX_ENTRY_PRICE = 0.60
```

- [ ] **Step 2: Verify no other file hard-codes these values**

```bash
grep -r "0\.37\|0\.63\|3\.0" src/pulse/ --include="*.py" | grep -v config.py | grep -v ".pyc"
```
Expected: no matches for these specific values (or only in comments/strings).

- [ ] **Step 3: Commit**

```bash
git add src/pulse/config.py
git commit -m "perf: tighten entry window 3.0→2.0min, price band 0.37-0.63→0.40-0.60"
```

---

### Task 2: Reweight score — window_delta up, momentum down, remove RSI score

**Files:**
- Modify: `src/pulse/strategy.py`

- [ ] **Step 1: Update compute_score weights and remove rsi_score from formula**

In `src/pulse/strategy.py`, replace the entire `compute_score` function body's weight block and docstring:

```python
def compute_score(
    ob: Optional[dict],
    direction: str,
    m15: float, m30: float, m60: float,
    remaining_min: float = 3.0,
    window_delta: float = 0.0,
) -> Tuple[float, float, float]:
    """
    Entry score v5.1.

    raw = (0.35*mom + 0.20*imb + 0.30*wd + 0.05*vol
           + spike_bonus + coherence) * time_factor

    RSI is no longer scored here — it is a binary veto applied in main.py.
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

    # OB imbalance — step function gated at 58% / 65%
    imb_score = 0.0
    if ob and ob["total_d"] > 0:
        imb = ob["bid_d"] / ob["total_d"]
        if direction == "UP":
            if imb >= 0.65:
                imb_score = 1.0
            elif imb >= 0.58:
                imb_score = 0.5
        else:
            inv = 1.0 - imb
            if inv >= 0.65:
                imb_score = 1.0
            elif inv >= 0.58:
                imb_score = 0.5

    # Volatility
    vol_score = min(FEED.volatility() / 150.0, 1.0)

    # Window delta — primary oracle-lag signal
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

    base = (
        0.35 * mom_score + 0.20 * imb_score
        + 0.30 * wd_score + 0.05 * vol_score
    )  # sums to [0, 1] (weights: 0.35+0.20+0.30+0.05 = 0.90, leaving 0.10 for bonuses)
    bonus_mult = 1.0 + spike_bonus + coh   # up to 1.27x
    raw = min(base * bonus_mult, 1.0) * time_factor
    return round(raw, 3), mom_score, imb_score
```

Note: the RSI import from config is still needed for the veto in main.py — keep the import in strategy.py's header as-is (it's used by feed.py via FEED.rsi()).

- [ ] **Step 2: Remove unused RSI_OVERBOUGHT / RSI_OVERSOLD import from strategy.py**

In `src/pulse/strategy.py`, change the import block at the top:
```python
from pulse.config import (
    MIN_POS, MAX_POS, SPIKE_THRESHOLD,
    MOM_15S_REF, MOM_30S_REF, MOM_60S_REF,
    TRADES_CSV,
)
```
(Remove `RSI_OVERBOUGHT, RSI_OVERSOLD` from the strategy.py import — they are no longer used there.)

- [ ] **Step 3: Commit**

```bash
git add src/pulse/strategy.py
git commit -m "perf: reweight score v5.1 — wd 15%→30%, mom 45%→35%, OB step-gate 65%, RSI removed from score"
```

---

### Task 3: Macro trend — add `macro_trend()` to BTCFeed

**Files:**
- Modify: `src/pulse/feed.py`

- [ ] **Step 1: Add macro_trend method to BTCFeed class**

Add this method after the `rsi()` method in `BTCFeed` (around line 165):

```python
def macro_trend(self, window_sec: float = 300.0) -> str:
    """
    Direction of the last completed 5-minute window.

    Returns 'UP', 'DOWN', or 'NEUTRAL' (insufficient data).
    Uses price 300s ago vs price 60s ago so we capture the
    prior completed window without including the current partial window.
    """
    ts, px = self._snapshot()
    if len(ts) < 4:
        return "NEUTRAL"
    now = time.monotonic()
    # price ~5min ago (start of prior window)
    i_start = np.searchsorted(ts, now - window_sec, side="left")
    # price ~60s ago (end of prior window, before current window)
    i_end = np.searchsorted(ts, now - 60.0, side="left")
    if i_start >= len(ts) or i_end >= len(ts) or i_end <= i_start:
        return "NEUTRAL"
    p_start = px[i_start]
    p_end = px[i_end]
    if p_start == 0:
        return "NEUTRAL"
    delta = (p_end - p_start) / p_start * 100
    if delta > 0.02:
        return "UP"
    elif delta < -0.02:
        return "DOWN"
    return "NEUTRAL"
```

- [ ] **Step 2: Verify method is accessible from main.py**

```bash
python3 -c "from pulse.feed import FEED; print(FEED.macro_trend())"
```
Expected: prints `NEUTRAL` (feed just started, no data yet) — no import error.

- [ ] **Step 3: Commit**

```bash
git add src/pulse/feed.py
git commit -m "feat: add BTCFeed.macro_trend() — 5min prior window direction for trend filter"
```

---

### Task 4: Entry gates — RSI binary veto + macro trend filter in main.py

**Files:**
- Modify: `src/pulse/main.py`

- [ ] **Step 1: Add RSI veto constants import**

At the top of `src/pulse/main.py`, ensure `RSI_OVERBOUGHT, RSI_OVERSOLD` are imported from config. Find the existing import block:
```python
from pulse.config import (
    SETTINGS, SHUTDOWN_EVENT, BANKROLL,
    MAX_OPEN_POS, MAX_DIR_POS, MIN_SCORE, SCAN_INTERVAL, MAX_DAILY_LOSS,
    SPIKE_THRESHOLD, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE, MAX_SPREAD,
    MIN_MOM_GLOBAL, TP_DELTA, SL_DELTA, TRAILING_STOP, TRAILING_DISTANCE,
    HOLD_THRESHOLD, HOLD_MIN_REMAINING, HOLD_ENABLED, RSI_PERIOD,
    TRADES_CSV, POSITIONS_CHECKPOINT,
    Position, SessionStats,
)
```
Add `RSI_OVERBOUGHT, RSI_OVERSOLD,` to this import (they are already in config.py).

- [ ] **Step 2: Add the two veto checks just before the score threshold check**

In `main.py`, find the inner loop block around line 590 where `score` is computed and compared to `cur_min`. Insert two new skip blocks **after** `score` is computed and **before** the `if score < cur_min:` check:

```python
                            score, _, _ = compute_score(
                                ob, direction, m15, m30, m60,
                                remaining_min=remaining, window_delta=wd,
                            )

                            # ── RSI binary veto ───────────────────────────────
                            rsi_now = FEED.rsi()
                            rsi_veto = (
                                (direction == "UP" and rsi_now < RSI_OVERSOLD) or
                                (direction == "DOWN" and rsi_now > RSI_OVERBOUGHT)
                            )
                            if rsi_veto:
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": f"RSI veto {rsi_now:.0f}",
                                    "mom15": m15, "mom60": m60,
                                })
                                continue

                            # ── Macro trend filter ────────────────────────────
                            macro = FEED.macro_trend()
                            # Override allowed if window_delta is strong (≥0.08%)
                            wd_strong = abs(wd) >= 0.08
                            macro_veto = (
                                macro != "NEUTRAL"
                                and macro != direction
                                and not wd_strong
                            )
                            if macro_veto:
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": f"macro {macro} vs {direction}",
                                    "mom15": m15, "mom60": m60,
                                })
                                continue
```

- [ ] **Step 3: Commit**

```bash
git add src/pulse/main.py
git commit -m "feat: add RSI binary veto + macro 5min trend filter at entry"
```

---

### Task 5: Update startup banner to reflect v5.1 changes

**Files:**
- Modify: `src/pulse/main.py`

- [ ] **Step 1: Update the banner in `run()`**

Find the `console.print(Panel(...))` block near line 129 in `main.py`. Update the strategy description lines:

```python
    console.print(Panel(
        f"[bold #f7931a]CRYPTO PULSE SNIPER v5.1-BTC[/]\n"
        f"Mode        : {'[bold red]LIVE[/]' if not dry else '[bold cyan]SIMULATION[/]'}\n"
        f"Score min   : [bold]{SETTINGS.min_score:.2f}[/]\n"
        f"Kelly 1/4   : [bold green]ON[/] (calibrated from CSV)\n"
        f"Hold-expiry : [bold green]{'ON' if hold_enabled else 'OFF'}[/]"
        f" (>{HOLD_THRESHOLD:.2f} with >{HOLD_MIN_REMAINING:.0f}s)\n"
        f"RSI({RSI_PERIOD})      : [bold green]VETO[/] (<{RSI_OVERSOLD:.0f} UP / >{RSI_OVERBOUGHT:.0f} DOWN)\n"
        f"Direction   : [bold green]3-signal vote + macro trend[/]\n"
        f"TP / SL     : +{TP_DELTA:.0%} / -{SL_DELTA:.0%} Trail:{TRAILING_DISTANCE}\n"
        f"Entry window: [bold]{SETTINGS.entry_win_min:.1f}–{SETTINGS.entry_win_max:.1f} min[/]\n"
        f"Circuit br. : stop if loss > [bold red]{SETTINGS.max_daily_loss}$[/]\n\n"
        f"[bold]Controls:[/] [+]/[-] score  [r] reset  [a] AUTO  [q] quit\n",
        border_style="#f7931a",
    ))
```

- [ ] **Step 2: Commit**

```bash
git add src/pulse/main.py
git commit -m "chore: update banner to v5.1 — reflect new RSI veto and macro trend filter"
```

---

### Task 6: Smoke test — dry run for 30 seconds

- [ ] **Step 1: Run in dry/sim mode and verify no import errors or crashes**

```bash
cd C:/Users/monni/bot/crypto_spike/last/polymarket-bot
python -m pulse.main --score 0.60 2>&1 | head -40
```
Expected: banner shows `v5.1-BTC`, `RSI(7) : VETO`, `Entry window: 0.3–2.0 min`, no tracebacks.

- [ ] **Step 2: Quick sanity check on score function**

```bash
python3 -c "
from pulse.strategy import compute_score
# Strong UP signal
s, m, i = compute_score({'bid_d':700,'total_d':1000,'bb':0.48,'ba':0.50,'mid':0.49,'spread':0.02},
    'UP', 0.08, 0.12, 0.18, remaining_min=2.5, window_delta=0.12)
print(f'Strong UP score: {s:.3f} (expect >0.6)')
# Weak signal — should be low
s2, _, _ = compute_score({'bid_d':520,'total_d':1000,'bb':0.48,'ba':0.50,'mid':0.49,'spread':0.02},
    'UP', 0.01, 0.01, 0.01, remaining_min=2.5, window_delta=0.01)
print(f'Weak signal score: {s2:.3f} (expect <0.3)')
# OB gate test — 60% imbalance should give 0.5, 70% should give 1.0
s3, _, i3 = compute_score({'bid_d':600,'total_d':1000,'bb':0.48,'ba':0.50,'mid':0.49,'spread':0.02},
    'UP', 0.05, 0.08, 0.15, remaining_min=2.5, window_delta=0.08)
print(f'60% imb imb_score: {i3:.1f} (expect 0.5)')
s4, _, i4 = compute_score({'bid_d':700,'total_d':1000,'bb':0.48,'ba':0.50,'mid':0.49,'spread':0.02},
    'UP', 0.05, 0.08, 0.15, remaining_min=2.5, window_delta=0.08)
print(f'70% imb imb_score: {i4:.1f} (expect 1.0)')
"
```
Expected output:
```
Strong UP score: >0.6
Weak signal score: <0.3
60% imb imb_score: 0.5
70% imb imb_score: 1.0
```

- [ ] **Step 3: Final commit tag**

```bash
git tag v5.1-strategy
```
