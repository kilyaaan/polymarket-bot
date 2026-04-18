# Polymarket Bot — Constitution

> *"Trade with data, not with hope. The market doesn't care about your model."*

A living trading organism for Polymarket binary options. Not a static bot — an entity that collects, learns, adapts, and knows what it doesn't know.

The developer (Kilyan) is learning. Code must be readable, well-named, and honest. If it needs a PhD to understand, simplify.

---

## I. Epistemic Foundation

Inherited from CYNIC's perennial epistemology. 22 traditions, one practice: bounded judgment under uncertainty.

### The Bound

**Max confidence: 61.8% (phi-inverse).** On any claim about market behavior, strategy performance, or signal quality. The market contains an irreducible unknowable remainder.

Four convergent sources: mathematical self-similarity (arXiv 2602.15266), metaphysical ceiling (Kybalion: "THE ALL is UNKNOWABLE"), traditional bounds (Pyrrhonism, Taoism ch.71, Kabbalah), pragmatic discrimination in scoring.

### Epistemic Labels

Every claim about the market must carry its status:
- **Observed** — probed with real data (collected prices, executed trades)
- **Deduced** — derived from observed (win rate calculated from trade CSV)
- **Inferred** — pattern recognized (regime detection from momentum)
- **Conjecture** — hypothesis not yet tested (strategy improvement ideas)

Never present conjecture as observation. The cost of confusion is money.

### The Traditions That Change Decisions

Eight traditions that directly alter trading behavior — not atmosphere, cognitive procedures:

| Tradition | When it fires | What it prevents |
|-----------|--------------|-----------------|
| **Pyrrhonism** | Signals diverge → suspend judgment → don't trade | Forcing trades on contradictory data |
| **Stoicism** | Pre-visualize the loss BEFORE entering (premeditatio malorum) | Emotional sizing, revenge trading |
| **Cynicism** | Data says strategy is dead → kill it. The Dog doesn't flatter (parrhesia) | Clinging to broken strategies |
| **Kalama Sutta** | "The backtest says" is not evidence — test in experience | Trusting models over reality |
| **Taoism** | Not trading IS a decision (wu wei). Don't force entries | Overtrading in low-signal regimes |
| **Confucius** | Call things by their true name (zhengming). "Observed fact", not "prediction" | Confusing inference with observation |
| **Epictetus** | You own your process, not the outcome. Only what is causally yours is yours | Judging strategy by individual P&L instead of process quality |
| **Diogenes** | Deface the currency: expose false signals, overfitting, hollow scores | Shipping untested "improvements" |

Further traditions (Carneades, Pythagoras, Kabbalah, Hermeticism, Seneca, Kybalion, Russell) reinforce these. Full mapping: CYNIC `docs/identity/CYNIC-PERENNIAL-EPISTEMOLOGY.md`.

### The Three Times

| Time | In the market | In the bot |
|------|--------------|------------|
| **Chronos** | Sequential: the 5-min candle, the scan loop, the pipeline | Mechanical execution |
| **Kairos** | The opportune moment — when conditions align for entry | Gate logic, late-entry timing |
| **Aion** | Eternal patterns — what survives across sessions | Crystals, learned parameters, evolved strategies |

---

## II. Principles

### The Scientific Protocol

Every strategy change follows this loop. No exceptions.

```
OBSERVE    → What is actually happening? (real data, not models)
HYPOTHESIZE → "If I do X, then Y changes by Z" (falsifiable)
EXPERIMENT  → Do X. Measure before AND after.
ANALYZE    → Did Y change? By how much?
CONCLUDE   → Adopt / reject / modify. Loop if inconclusive.
```

**What would falsify this?** If you can't answer, you don't have a hypothesis.

### Organic Before Mechanical

Run any protocol manually 2-3 times before crystallizing it as code. Observe reality before abstracting it. Heuristics before ML. ML before deep learning. Each layer earns its place by proving the previous one insufficient.

### The FOGC Test (Trading Edition)

Before trusting any strategy: *"If I invert this strategy (buy DOWN when it says UP), does it lose money?"* If the inverted strategy breaks even → the original has no edge. The strategy must be load-bearing, not decoration.

### Data Hierarchy

1. **Real collected Polymarket data** — order books, mid prices, spreads (ground truth)
2. **Trade CSV with outcomes** — what happened when we traded
3. **BTC price data from Binance** — real but insufficient alone
4. **Synthetic models (BS, linear)** — proven wrong (2026-04-18), never trust

### Transmutation, Not Negation

A losing trade is not failure — it is material. Every loss teaches something about market microstructure, timing, or regime. The system that learns from losses compounds. The system that ignores them repeats.

---

## III. Sovereignty

### External Dependencies

| Dependency | What breaks without it | Fallback |
|------------|----------------------|----------|
| Binance WebSocket | No BTC price feed | REST polling (already implemented) |
| Polymarket CLOB API | No order books, no markets | Stop trading, keep collecting what's available |
| Polygon RPC | No wallet balance | Fallback to configured BANKROLL |
| Telegram | No notifications | Log-only (already works) |

No single external failure should cause the bot to trade blindly. If a data source fails, the bot degrades to observation mode — it collects data but does not enter positions.

### Decision Authority

The human holds final authority. The bot proposes, the human disposes.
- DRY_RUN=true is the default. Switching to live is a human decision.
- The bot never escalates its own permissions.
- When data and heuristics contradict, the bot stops and logs the contradiction — it does not resolve it autonomously.

### Enforcement Status

Rules that have mechanical gates (hooks, validation, code):
- Token ID validation (`validate_token_id()`)
- Order validation gate (`_validate_order()`)
- DRY_RUN default in code
- Crash recovery (positions.json checkpoint)

Rules that are guidance only (no gate yet — documented debt):
- Branch + PR discipline (no pre-push hook)
- No credentials in logs (no lint)
- Scientific protocol for strategy changes (no checklist enforcer)
- Epistemic labels on claims (LLM compliance only)

Naming the debt is better than pretending the gate exists.

---

## IV. Security (Inviolable)

- **DRY_RUN=true** by default. `--live` requires explicit flag.
- **PRIVATE_KEY never committed.** Never. In any file. Ever.
- **Secrets in `~/.pulse-env`**, not in the repo. `.env` = project defaults only.
- **No credentials in logs.** Not even truncated.
- **Token IDs validated** before URL construction.
- **Order validation gate** before any order submission.

---

## V. Git Discipline

- **Branch + PR for every change.** Never push directly to main.
- **Branch naming:** `<type>/<scope>-YYYY-MM-DD`
- **Atomic commits**, clear messages, one concern per commit.
- **PR before ending session.**

---

## VI. Development Approach

### The Learning Loop

```
Collect real data (collect.py)
  → Analyze patterns (jupyter, scripts)
    → Formulate heuristic (falsifiable)
      → Implement + dry-run test
        → Forward test (24-48h)
          → Measure results
            → Adopt / reject / modify
              → Collect more data...
```

The bot gets smarter by accumulating real trades and real market observations. Not by adding complexity.

### Strategy Is Alive

The current strategy is always documented in `strategy.py` with its data basis. But strategies evolve. What matters is the process (observe → hypothesize → test → measure), not the current parameters.

### Code Principles

- Each file has ONE responsibility. < 300 lines.
- A beginner opens the file they need and nothing else.
- No abstractions without evidence of reuse.
- Delete before deprecate. Git has history.

---

## VII. Vision

The bot is an organism, not a tool. Today it runs heuristics. Tomorrow it may run learned models. The epistemology stays the same — bounded confidence, real data, falsifiable hypotheses, organic evolution.

The path: **heuristics → calibrated heuristics → online learning → autonomous adaptation.** Each stage earns the next by proving insufficient at the current scale.

---

## VIII. Environment

```
~/.pulse-env          # operator secrets (not committed)
.env                  # project defaults (POLYGON_RPC, etc.)
src/pulse/            # bot source
collect.py            # Polymarket data collector
direction_test.py     # strategy accuracy test
backtest.py           # backtest on collected data
```

`PYTHONPATH=src` required for running modules.
