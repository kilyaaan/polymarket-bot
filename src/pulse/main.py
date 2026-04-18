"""Main — entrypoint, thread orchestration, scan loop, graceful shutdown."""

from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
import time
from collections import deque
from typing import List

from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from pulse.config import (
    SETTINGS, SHUTDOWN_EVENT, BANKROLL,
    MAX_OPEN_POS, MAX_DIR_POS, MIN_SCORE, SCAN_INTERVAL, MAX_DAILY_LOSS,
    SPIKE_THRESHOLD, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE, MAX_SPREAD,
    MIN_MOM_GLOBAL, TP_DELTA, SL_DELTA, TRAILING_STOP, TRAILING_DISTANCE,
    HOLD_THRESHOLD, HOLD_MIN_REMAINING, HOLD_ENABLED, RSI_PERIOD,
    TRADES_CSV, POSITIONS_CHECKPOINT,
    Position, SessionStats,
)
from pulse.feed import FEED, SPIKE_INTERRUPT, start_ws_btc, rest_fallback_btc, spike_monitor_loop, prewarm_connections
from pulse.strategy import compute_score, kelly_size, vote_direction, has_overlapping_position
from pulse.orders import (
    place_order, poll_order_status, close_position, cancel_all_pending,
    get_ob, get_ob_multi, get_cached_markets, get_fee_rate,
    sync_wallet_usdc, redeem_loop, prefetch_loop, shutdown_ob_pool,
)
from pulse.risk import ExpiringBlacklist, save_checkpoint, load_checkpoint, reconcile_positions
from pulse.dashboard import make_dashboard
from pulse.logger import setup_logging, init_csv, log_trade, tg, fee_usdc

import logging
log = logging.getLogger(__name__)


# ── Scan state (shared dict for dashboard + spike monitor) ───────────────────
def _make_scan_state() -> dict:
    return {
        "active_markets": [],
        "log": deque(maxlen=150),
        "best_score": 0.0,
        "spikes": deque(maxlen=20),
        "avg_edge": 0.0,
        "settings": SETTINGS,
    }


# ── Keyboard handler ────────────────────────────────────────────────────────
def _handle_key(ch: str) -> None:
    if ch in ("+", "="):
        SETTINGS.increase()
    elif ch == "-":
        SETTINGS.decrease()
    elif ch == "r":
        SETTINGS.reset()
    elif ch == "a":
        SETTINGS.toggle_auto()
    elif ch == "q":
        SHUTDOWN_EVENT.set()


def _keyboard_thread():
    try:
        import select
        import tty
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            # setcbreak instead of setraw:
            #   - single-key reads without Enter (same as raw)
            #   - Ctrl+C still generates SIGINT (unlike raw)
            #   - output processing (ONLCR \n→\r\n) stays ON
            #     → prevents the cursor drift that corrupts Rich on SSH/MobaXterm
            tty.setcbreak(fd)
            while not SHUTDOWN_EVENT.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if ready:
                    ch = sys.stdin.read(1).lower()
                    _handle_key(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        try:
            import msvcrt
            while not SHUTDOWN_EVENT.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getch().decode("utf-8", "ignore").lower()
                    _handle_key(ch)
                time.sleep(0.05)
        except Exception:
            pass


# ── Main loop ────────────────────────────────────────────────────────────────
def run(dry: bool = True, hold_enabled: bool = True, log_level: str = "INFO"):
    # force_terminal=True ensures Rich works correctly over SSH / MobaXterm
    # where terminal capability detection can fail
    console = Console(force_terminal=True)
    setup_logging(log_level)
    init_csv()

    # Private key warning
    import os
    pk = os.getenv("PRIVATE_KEY", "").strip()
    if pk and not dry:
        console.print(Panel(
            "[bold yellow]WARNING: PRIVATE KEY DETECTED[/]\n\n"
            "Make sure this is the [bold red]POLYMARKET PROXY WALLET[/] key,\n"
            "[bold]NOT[/] your main MetaMask key.\n\n"
            "polymarket.com > Profile > Settings > Export Private Key",
            title="WARNING", border_style="yellow",
        ))
        time.sleep(2)

    console.print(Panel(
        f"[bold #f7931a]CRYPTO PULSE SNIPER v5.0-BTC[/]\n"
        f"Mode        : {'[bold red]LIVE[/]' if not dry else '[bold cyan]SIMULATION[/]'}\n"
        f"Score min   : [bold]{SETTINGS.min_score:.2f}[/]\n"
        f"Kelly 1/4   : [bold green]ON[/] (calibrated from CSV)\n"
        f"Hold-expiry : [bold green]{'ON' if hold_enabled else 'OFF'}[/]"
        f" (>{HOLD_THRESHOLD:.2f} with >{HOLD_MIN_REMAINING:.0f}s)\n"
        f"RSI({RSI_PERIOD})      : [bold green]ON[/] (1s candles)\n"
        f"Direction   : [bold green]3-signal vote[/]\n"
        f"TP / SL     : +{TP_DELTA:.0%} / -{SL_DELTA:.0%} Trail:{TRAILING_DISTANCE}\n"
        f"Circuit br. : stop if loss > [bold red]{SETTINGS.max_daily_loss}$[/]\n\n"
        f"[bold]Controls:[/] [+]/[-] score  [r] reset  [a] AUTO  [q] quit\n",
        border_style="#f7931a",
    ))

    scan_state = _make_scan_state()

    # Launch threads
    threads = [
        ("ws_btc", start_ws_btc),
        ("rest_fallback", rest_fallback_btc),
        ("keyboard", _keyboard_thread),
        ("prefetch", lambda: prefetch_loop(SETTINGS)),
        ("spike_monitor", lambda: spike_monitor_loop(scan_state)),
        ("prewarm", prewarm_connections),
        ("redeem", redeem_loop),
    ]
    for name, target in threads:
        threading.Thread(target=target, name=name, daemon=True).start()

    console.print("[dim]Connecting to Binance WS...[/]")
    time.sleep(3)

    # Load positions from checkpoint (crash recovery)
    positions: List[Position] = load_checkpoint()
    if positions:
        positions = reconcile_positions(positions)
        save_checkpoint(positions)

    stats = SessionStats()
    blacklist = ExpiringBlacklist(ttl=360.0)
    bankroll = sync_wallet_usdc(force=True)
    if bankroll <= 0:
        bankroll = BANKROLL
        log.warning("Wallet not found — fallback %.0f$", BANKROLL)
    else:
        log.info("USDC.e on-chain: %.2f$", bankroll)

    countdown = float(SETTINGS.scan_interval)
    last_spike_ts = 0.0
    tg(f"v5.0-BTC {'LIVE' if not dry else 'SIM'} | Kelly+Hold+RSI+Vote")

    try:
        with Live(console=console, refresh_per_second=10, screen=True) as live:
            while not SHUTDOWN_EVENT.is_set():
                max_dl = SETTINGS.max_daily_loss

                # Circuit breaker
                if stats.total_pnl <= -max_dl:
                    tg(f"CIRCUIT BREAKER! Loss: {stats.total_pnl:+.2f}$")
                    log.critical("Circuit breaker triggered: %.2f$", stats.total_pnl)
                    break

                stats.scans += 1
                scan_state["best_score"] = 0.0
                SPIKE_INTERRUPT.clear()
                bankroll = sync_wallet_usdc()
                m15, m30, m60 = FEED.momentum_all()

                # ── Exits ────────────────────────────────────────────────
                closed: List[Position] = []
                if positions:
                    exit_obs = get_ob_multi([p.token_id for p in positions])
                    for pos in positions:
                        ob = exit_obs.get(pos.token_id)
                        if ob:
                            pos.current_price = ob["mid"]

                        rem_sec = pos.market.remaining_sec

                        # Hold-to-expiry activation
                        if (hold_enabled
                                and not pos.holding_expiry
                                and pos.current_price >= HOLD_THRESHOLD
                                and rem_sec >= HOLD_MIN_REMAINING):
                            pos.holding_expiry = True
                            stats.held_expiry += 1
                            scan_state["log"].appendleft({
                                "type": "skip", "dir": pos.direction,
                                "reason": f"HOLD>{HOLD_THRESHOLD:.2f}@{pos.current_price:.3f}",
                                "mom15": m15, "mom60": m60,
                            })
                            tg(f"HOLD expiry {pos.direction} @{pos.current_price:.3f} — {rem_sec:.0f}s")
                            log.info("HOLD activated: %s @%.3f %0.fs remaining",
                                     pos.direction, pos.current_price, rem_sec)

                        # Trailing stop
                        if TRAILING_STOP and pos.current_price > pos.peak_price:
                            pos.peak_price = pos.current_price
                            pos.trail_sl = max(
                                pos.trail_sl,
                                round(pos.peak_price - TRAILING_DISTANCE, 4),
                                round(pos.entry_price - SL_DELTA, 4),
                            )

                        eff_tp = round(pos.entry_price + TP_DELTA, 4)
                        eff_sl = pos.trail_sl if TRAILING_STOP else round(pos.entry_price - SL_DELTA, 4)

                        reason = None
                        if pos.holding_expiry:
                            if pos.current_price <= eff_sl:
                                reason = f"SL(hold) {pos.current_price:.3f}(<{eff_sl:.3f})"
                                pos.holding_expiry = False
                            elif rem_sec <= 9:
                                reason = "EXPIRY"
                        else:
                            if pos.current_price >= eff_tp:
                                reason = f"TP {pos.current_price:.3f}(>{eff_tp:.3f})"
                            elif pos.current_price <= eff_sl:
                                reason = f"SL {pos.current_price:.3f}(<{eff_sl:.3f})"
                            elif pos.market.remaining_min <= 0.15:
                                reason = "EXPIRY"

                        if reason:
                            ob2 = get_ob(pos.token_id) if reason != "EXPIRY" else ob
                            if ob2:
                                pos.current_price = ob2["mid"]

                            if reason != "EXPIRY":
                                exit_price = ob2["bb"] if ob2 else pos.current_price
                                close_id, fill_status = close_position(
                                    pos.token_id, exit_price, pos.shares_held, dry)
                                pos.close_order_id = close_id if close_id else "failed"
                                pos.close_fill = fill_status
                                if not dry:
                                    if close_id is None:
                                        scan_state["log"].appendleft({
                                            "type": "skip", "dir": pos.direction,
                                            "reason": "CLOSE FAILED — kept",
                                            "mom15": m15, "mom60": m60,
                                        })
                                        log.error("Close failed for %s %s", pos.direction, pos.token_id[:16])
                                        continue
                                    if fill_status not in ("filled", "partial"):
                                        scan_state["log"].appendleft({
                                            "type": "skip", "dir": pos.direction,
                                            "reason": f"SELL not fill ({fill_status})",
                                            "mom15": m15, "mom60": m60,
                                        })
                                        tg(f"SELL not fill {pos.direction}")
                                        log.warning("SELL not filled: %s %s", pos.direction, fill_status)
                                        continue
                                    bankroll = sync_wallet_usdc(force=True)
                                pos.current_price = exit_price
                            else:
                                pos.close_order_id = "expiry_resolution"
                                pos.close_fill = "expiry"
                                if not dry:
                                    time.sleep(5)
                                    bankroll = sync_wallet_usdc(force=True)
                                    tg(f"EXPIRY {pos.direction} — auto resolution")

                            pnl = log_trade(pos, pos.current_price, reason, stats,
                                           FEED.current, SETTINGS.min_score)
                            stats.total_pnl += pnl
                            stats.total += 1
                            if pnl >= 0:
                                stats.wins += 1
                            else:
                                stats.losses += 1
                            scan_state["log"].appendleft({
                                "type": "exit", "dir": pos.direction,
                                "reason": reason, "pnl": round(pnl, 2),
                                "pnl_pct": round(pos.pnl_pct * 100, 1),
                                "size": round(pos.size_usdc, 2),
                                "entry": round(pos.entry_price, 4),
                                "mom15": m15, "mom60": m60,
                            })
                            tg(f"{'WIN' if pnl >= 0 else 'LOSS'} {reason} | "
                               f"{pos.direction} | {pnl:+.2f}$ | fill:{pos.close_fill}")
                            closed.append(pos)

                    for c in closed:
                        positions.remove(c)
                    if closed:
                        save_checkpoint(positions)

                if abs(m15) >= SPIKE_THRESHOLD:
                    stats.spikes_seen += 1

                # ── Entry scan ───────────────────────────────────────────
                if len(positions) < MAX_OPEN_POS:
                    all_markets = get_cached_markets()
                    scan_state["active_markets"] = all_markets
                    cur_min, win_min, win_max = SETTINGS.thresholds
                    is_spike_now = abs(m15) >= SPIKE_THRESHOLD

                    primary_dir = vote_direction(m15, m30, m60)
                    directions = [primary_dir]
                    if is_spike_now and abs(m15) >= SPIKE_THRESHOLD * 1.5:
                        directions.append("DOWN" if primary_dir == "UP" else "UP")

                    up_count = sum(1 for p in positions if p.direction == "UP")
                    down_count = sum(1 for p in positions if p.direction == "DOWN")

                    candidates = []
                    for mkt in all_markets:
                        if mkt.condition_id in blacklist:
                            continue
                        if any(p.market.condition_id == mkt.condition_id for p in positions):
                            continue
                        elapsed = mkt.elapsed_min
                        if not (win_min <= elapsed <= win_max) and not is_spike_now:
                            scan_state["log"].appendleft({
                                "type": "skip", "dir": "-",
                                "reason": f"window {elapsed:.1f}m",
                                "mom15": m15, "mom60": m60,
                            })
                            continue
                        if abs(m60) < MIN_MOM_GLOBAL and not is_spike_now:
                            scan_state["log"].appendleft({
                                "type": "skip", "dir": primary_dir,
                                "reason": f"mom weak {m60:+.4f}%",
                                "mom15": m15, "mom60": m60,
                            })
                            continue
                        candidates.append(mkt)

                    if candidates:
                        token_map = {}
                        for mkt in candidates:
                            for direction in directions:
                                if direction == "UP" and up_count >= MAX_DIR_POS:
                                    continue
                                if direction == "DOWN" and down_count >= MAX_DIR_POS:
                                    continue
                                # Correlation check
                                if has_overlapping_position(positions, mkt, direction):
                                    scan_state["log"].appendleft({
                                        "type": "skip", "dir": direction,
                                        "reason": "overlap",
                                        "mom15": m15, "mom60": m60,
                                    })
                                    continue
                                tok = mkt.yes_token if direction == "UP" else mkt.no_token
                                token_map[tok] = (mkt, direction)

                        obs = get_ob_multi(list(token_map.keys()))

                        for tok, ob in obs.items():
                            if len(positions) >= MAX_OPEN_POS:
                                break
                            mkt, direction = token_map[tok]
                            if mkt.condition_id in blacklist:
                                continue
                            if not ob:
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": "OB empty",
                                    "mom15": m15, "mom60": m60,
                                })
                                continue
                            if direction == "UP" and up_count >= MAX_DIR_POS:
                                continue
                            if direction == "DOWN" and down_count >= MAX_DIR_POS:
                                continue

                            remaining = mkt.remaining_min
                            wd = ((FEED.current - mkt.start_price) / mkt.start_price * 100
                                  if mkt.start_price > 0 else 0.0)
                            score, _, _ = compute_score(
                                ob, direction, m15, m30, m60,
                                remaining_min=remaining, window_delta=wd,
                            )

                            # Track best score
                            scan_state["avg_edge"] = 0.1 * score + 0.9 * scan_state["avg_edge"]
                            if score > scan_state["best_score"]:
                                scan_state["best_score"] = score
                            SETTINGS.auto_update(scan_state["best_score"])

                            if score < cur_min:
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": f"score {score:.3f}<{cur_min:.2f}",
                                    "score": score, "mom15": m15, "mom60": m60,
                                })
                                continue

                            entry_p = min(round(ob["ba"] + 0.01, 2), 0.99)
                            if not (MIN_ENTRY_PRICE <= entry_p <= MAX_ENTRY_PRICE):
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": f"price {entry_p:.3f} OOR",
                                    "mom15": m15, "mom60": m60,
                                })
                                continue
                            if ob["spread"] > MAX_SPREAD:
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": f"spread {ob['spread']:.3f}>{MAX_SPREAD}",
                                    "mom15": m15, "mom60": m60,
                                })
                                continue

                            size = kelly_size(score, entry_p, bankroll)
                            if bankroll < size + sum(p.size_usdc for p in positions):
                                continue

                            shares = math.ceil(size / max(entry_p, 0.001) * 10000) / 10000

                            try:
                                order_id = place_order(tok, entry_p, size, dry)
                            except ValueError as e:
                                log.error("Order validation failed: %s", e)
                                continue

                            if order_id is None:
                                continue

                            actual_shares = shares
                            if not dry:
                                buy_fill, filled_shares = poll_order_status(order_id, timeout=20.0)
                                if buy_fill == "cancelled":
                                    scan_state["log"].appendleft({
                                        "type": "skip", "dir": direction,
                                        "reason": "BUY cancelled",
                                        "mom15": m15, "mom60": m60,
                                    })
                                    tg(f"BUY cancelled {direction}")
                                    log.warning("BUY cancelled: %s", direction)
                                    continue
                                if buy_fill == "open":
                                    tg(f"BUY open {direction} (waiting fill)")
                                    log.info("BUY still open: %s", direction)
                                if filled_shares is not None:
                                    actual_shares = filled_shares
                                    log.info("Fill verified: %.4f shares (local est: %.4f)",
                                             filled_shares, shares)
                                bankroll = sync_wallet_usdc(force=True)

                            pos = Position(
                                market=mkt, token_id=tok, direction=direction,
                                entry_price=entry_p, size_usdc=size,
                                shares_held=actual_shares,
                                order_id=order_id,
                                entry_crypto=FEED.current, score=score,
                                peak_price=entry_p,
                                trail_sl=round(entry_p - SL_DELTA, 4),
                                mom15_at_entry=m15,
                                rsi_at_entry=FEED.rsi(),
                                kelly_used=size,
                                fee_rate_used=get_fee_rate(tok),
                            )
                            positions.append(pos)
                            save_checkpoint(positions)
                            stats.snipes += 1
                            if direction == "UP":
                                up_count += 1
                            else:
                                down_count += 1
                            blacklist.add(mkt.condition_id)
                            scan_state["log"].appendleft({
                                "type": "snipe", "dir": direction, "score": score,
                                "mom15": m15, "mom60": m60,
                                "reason": f"entry {entry_p:.3f} K:{size:.1f}$",
                                "size": round(size, 2), "entry": round(entry_p, 4),
                            })
                            tg(f"SNIPE {direction} | score {score:.3f} | "
                               f"Kelly {size:.1f}$ | entry {entry_p:.3f} | "
                               f"RSI {pos.rsi_at_entry:.0f}")
                            log.info("SNIPE %s score=%.3f kelly=%.1f$ entry=%.3f",
                                     direction, score, size, entry_p)

                live.update(make_dashboard(
                    positions, stats, dry, countdown,
                    m15, m30, m60, bankroll, scan_state,
                ))

                # Adaptive scan interval
                since_spike = time.monotonic() - last_spike_ts
                countdown = float(1.0 if since_spike < 5.0 else SETTINGS.scan_interval)
                while countdown > 0 and not SHUTDOWN_EVENT.is_set():
                    live.update(make_dashboard(
                        positions, stats, dry, countdown,
                        m15, m30, m60, bankroll, scan_state,
                    ))
                    if SPIKE_INTERRUPT.wait(timeout=0.1):
                        SPIKE_INTERRUPT.clear()
                        last_spike_ts = time.monotonic()
                        break
                    countdown -= 0.1

    except KeyboardInterrupt:
        SHUTDOWN_EVENT.set()

    # ── Graceful shutdown ────────────────────────────────────────────────
    log.info("Shutting down...")
    if not dry:
        cancel_all_pending()
    shutdown_ob_pool()
    save_checkpoint(positions)

    if positions:
        log.warning("%d position(s) still open at shutdown", len(positions))
        console.print(f"[bold yellow]WARNING: {len(positions)} position(s) open — logged as SHUTDOWN_OPEN[/]")
        for pos in positions:
            log_trade(pos, pos.current_price, "SHUTDOWN_OPEN", stats,
                     FEED.current, SETTINGS.min_score)

    console.print(Panel(
        f"[bold]SESSION COMPLETE[/]\n"
        f" Duration : {stats.elapsed}\n"
        f" P&L      : [bold {'green' if stats.total_pnl >= 0 else 'red'}]{stats.total_pnl:+.2f} USDC[/]\n"
        f" Win rate : [cyan]{stats.win_rate:.1f}%[/] ({stats.wins}W/{stats.losses}L)\n"
        f" HoldExp  : [cyan]{stats.held_expiry}[/] positions held to expiry\n"
        f" Spikes   : {stats.spikes_seen}  Scans: {stats.scans}\n"
        f" CSV      : {TRADES_CSV}\n"
        f" Checkpoint: {POSITIONS_CHECKPOINT}",
        title="SUMMARY", border_style="#f7931a",
    ))


# ── CLI entrypoint ───────────────────────────────────────────────────────────
def cli():
    # Signal handlers for graceful shutdown
    def _shutdown_handler(sig, frame):
        SHUTDOWN_EVENT.set()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    p = argparse.ArgumentParser(description="Crypto Pulse Sniper v5.0-BTC")
    p.add_argument("--live", action="store_true", help="Live mode (default: simulation)")
    p.add_argument("--score", type=float, default=MIN_SCORE, help=f"Min score (default: {MIN_SCORE})")
    p.add_argument("--auto", action="store_true", help="Auto-adaptive score")
    p.add_argument("--window", type=str, default=None, help="Entry window e.g. 0.3,4.5")
    p.add_argument("--scan", type=int, default=SCAN_INTERVAL, help=f"Scan interval secs (default: {SCAN_INTERVAL})")
    p.add_argument("--max-loss", type=float, default=MAX_DAILY_LOSS, help=f"Circuit breaker $ (default: {MAX_DAILY_LOSS})")
    p.add_argument("--no-hold", action="store_true", help="Disable hold-to-expiry")
    p.add_argument("--log-level", type=str, default="INFO", help="Log level (default: INFO)")
    args = p.parse_args()

    hold_enabled = HOLD_ENABLED and not args.no_hold
    SETTINGS.min_score = max(0.20, min(0.90, args.score))
    if args.auto:
        SETTINGS.auto_score = True
    SETTINGS.scan_interval = args.scan
    SETTINGS.max_daily_loss = args.max_loss
    if args.window:
        try:
            wmin, wmax = map(float, args.window.split(","))
            with SETTINGS._lock:
                SETTINGS.entry_win_min = wmin
                SETTINGS.entry_win_max = wmax
        except ValueError:
            pass

    if args.live:
        print("\nLIVE MODE in 5s — Ctrl+C to cancel...")
        time.sleep(5)

    run(dry=not args.live, hold_enabled=hold_enabled, log_level=args.log_level)


if __name__ == "__main__":
    cli()
