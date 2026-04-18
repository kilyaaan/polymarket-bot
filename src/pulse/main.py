"""Main v6 — Late Trend heuristics. Hold-to-expiry, no TP/SL/trailing."""

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
    MAX_OPEN_POS, MAX_DIR_POS, SCAN_INTERVAL, MAX_DAILY_LOSS,
    SPIKE_THRESHOLD, DEFAULT_TAKER_FEE_RATE,
    TRADES_CSV, POSITIONS_CHECKPOINT,
    Position, SessionStats,
)
from pulse.feed import FEED, SPIKE_INTERRUPT, start_ws_btc, rest_fallback_btc, spike_monitor_loop, prewarm_connections
from pulse.strategy import evaluate_gates, has_overlapping_position, COOLDOWN_MARKETS
from pulse.orders import (
    place_order, poll_order_status, cancel_all_pending,
    get_ob, get_ob_multi, get_cached_markets, get_fee_rate,
    sync_wallet_usdc, redeem_loop, prefetch_loop, shutdown_ob_pool,
)
from pulse.risk import ExpiringBlacklist, save_checkpoint, load_checkpoint, reconcile_positions
from pulse.dashboard import make_dashboard
from pulse.logger import setup_logging, init_csv, log_trade, tg

import logging
log = logging.getLogger(__name__)


def _make_scan_state() -> dict:
    return {
        "active_markets": [],
        "log": deque(maxlen=150),
        "best_score": 0.0,
        "spikes": deque(maxlen=20),
        "avg_edge": 0.0,
        "settings": SETTINGS,
    }


def _handle_key(ch: str) -> None:
    if ch in ("+", "="):
        SETTINGS.increase()
    elif ch == "-":
        SETTINGS.decrease()
    elif ch == "r":
        SETTINGS.reset()
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
            # setcbreak (not setraw): single-key reads without Enter,
            # but preserves output processing (ONLCR \n→\r\n).
            # setraw disables ONLCR which causes cursor drift in Rich on SSH.
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


def run(dry: bool = True, log_level: str = "INFO"):
    console = Console(force_terminal=True)
    setup_logging(log_level)
    init_csv()

    import os
    pk = os.getenv("PRIVATE_KEY", "").strip()
    if pk and not dry:
        console.print(Panel(
            "[bold yellow]WARNING: PRIVATE KEY DETECTED[/]\n\n"
            "Make sure this is the [bold red]POLYMARKET PROXY WALLET[/] key,\n"
            "[bold]NOT[/] your main MetaMask key.",
            title="WARNING", border_style="yellow",
        ))
        time.sleep(2)

    try:
        from pyfiglet import Figlet
        _fig = Figlet(font="doom", width=console.width or 100)
        ascii_title = _fig.renderText("JOSSELIN III") + _fig.renderText("CRYPTO PULSE v5")
    except ImportError:
        ascii_title = "JOSSELIN III\nCRYPTO PULSE v5\n"

    console.print(Panel(
        f"[bold #f7931a]{ascii_title}[/]"
        f"Mode     : {'[bold red]LIVE[/]' if not dry else '[bold cyan]SIMULATION[/]'}\n"
        f"Strategy : [bold green]3 gates + hold-to-expiry[/]\n"
        f"  Gate 1 : |window_delta| > 0.05%\n"
        f"  Gate 2 : remaining <= 60s (late entry)\n"
        f"  Gate 3 : entry_price <= 0.70\n"
        f"  Exit   : hold-to-expiry (no TP/SL/trailing)\n"
        f"  Sizing : HOWL $15 / WAG $10 / GROWL $5\n"
        f"Circuit  : stop if loss > [bold red]{SETTINGS.max_daily_loss}$[/]\n\n"
        f"[bold]Controls:[/] [q] quit\n",
        border_style="#f7931a",
    ))

    scan_state = _make_scan_state()

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

    consecutive_losses = 0
    cooldown_remaining = 0
    countdown = float(SETTINGS.scan_interval)
    tg(f"v6.0 Late Trend {'LIVE' if not dry else 'SIM'} | 3 gates + hold-to-expiry")

    try:
        with Live(console=console, refresh_per_second=10, screen=True) as live:
            while not SHUTDOWN_EVENT.is_set():
                max_dl = SETTINGS.max_daily_loss

                # Circuit breaker
                if stats.total_pnl <= -max_dl:
                    tg(f"CIRCUIT BREAKER! Loss: {stats.total_pnl:+.2f}$")
                    log.critical("Circuit breaker: %.2f$", stats.total_pnl)
                    break

                stats.scans += 1
                scan_state["best_score"] = 0.0
                bankroll = sync_wallet_usdc()
                m15, m30, m60 = FEED.momentum_all()

                # ── Exits: hold-to-expiry only ───────────────────────────
                closed: List[Position] = []
                if positions:
                    exit_obs = get_ob_multi([p.token_id for p in positions])
                    for pos in positions:
                        ob = exit_obs.get(pos.token_id)
                        if ob:
                            pos.current_price = ob["mid"]

                        rem_sec = pos.market.remaining_sec

                        # Only exit at expiry — no TP, no SL, no trailing
                        if rem_sec <= 5:
                            pos.holding_expiry = True
                            pos.close_order_id = "expiry_resolution"
                            pos.close_fill = "expiry"

                            if not dry:
                                time.sleep(5)
                                bankroll = sync_wallet_usdc(force=True)

                            # At expiry, option resolves to ~1.0 or ~0.0
                            # Use last observed mid as proxy for dry mode
                            exit_price = pos.current_price
                            pnl = log_trade(pos, exit_price, "EXPIRY", stats,
                                           FEED.current, 0.0)
                            stats.total_pnl += pnl
                            stats.total += 1
                            stats.held_expiry += 1
                            if pnl >= 0:
                                stats.wins += 1
                                consecutive_losses = 0
                            else:
                                stats.losses += 1
                                consecutive_losses += 1
                                if consecutive_losses >= 3:
                                    cooldown_remaining = COOLDOWN_MARKETS

                            scan_state["log"].appendleft({
                                "type": "exit", "dir": pos.direction,
                                "reason": "EXPIRY", "pnl": round(pnl, 2),
                                "pnl_pct": round(pos.pnl_pct * 100, 1),
                                "size": round(pos.size_usdc, 2),
                                "entry": round(pos.entry_price, 4),
                                "mom15": m15, "mom60": m60,
                            })
                            tg(f"{'WIN' if pnl >= 0 else 'LOSS'} EXPIRY | "
                               f"{pos.direction} | {pnl:+.2f}$ | "
                               f"entry={pos.entry_price:.3f} exit={exit_price:.3f}")
                            closed.append(pos)

                    for c in closed:
                        positions.remove(c)
                    if closed:
                        save_checkpoint(positions)

                # ── Entry: gate logic ────────────────────────────────────
                if len(positions) < MAX_OPEN_POS:
                    all_markets = get_cached_markets()
                    scan_state["active_markets"] = all_markets

                    # Decrement cooldown every scan (not gated on closed positions)
                    if cooldown_remaining > 0:
                        cooldown_remaining -= 1

                    # Sort by |window_delta| descending — best signal first
                    scored_markets = []
                    for mkt in all_markets:
                        if mkt.condition_id in blacklist:
                            continue
                        if any(p.market.condition_id == mkt.condition_id for p in positions):
                            continue
                        if mkt.start_price <= 0:
                            continue
                        delta = abs((FEED.current - mkt.start_price) / mkt.start_price * 100)
                        scored_markets.append((delta, mkt))
                    scored_markets.sort(reverse=True)

                    for _, mkt in scored_markets:
                        if len(positions) >= MAX_OPEN_POS:
                            break

                        btc_now = FEED.current
                        rem_sec = mkt.remaining_sec

                        # Skip timing check early — avoids a wasted OB fetch
                        # for every market that isn't in the late-entry window
                        if rem_sec > 60 or rem_sec <= 5:
                            scan_state["log"].appendleft({
                                "type": "skip", "dir": "-",
                                "reason": f"timing {rem_sec:.0f}s",
                                "mom15": m15, "mom60": m60,
                            })
                            continue

                        # Fetch OB only for markets in the late window
                        direction_guess = "UP" if btc_now > mkt.start_price else "DOWN"
                        tok = mkt.yes_token if direction_guess == "UP" else mkt.no_token
                        ob = get_ob(tok)

                        entry_price = ob["ba"] if ob else 0.0

                        # Run full gates
                        signal = evaluate_gates(
                            btc_now=btc_now,
                            btc_market_open=mkt.start_price,
                            remaining_sec=rem_sec,
                            entry_price=entry_price,
                            consecutive_losses=consecutive_losses,
                            cooldown_remaining=cooldown_remaining,
                        )

                        if not signal.trade:
                            scan_state["log"].appendleft({
                                "type": "skip", "dir": direction_guess,
                                "reason": signal.reason,
                                "mom15": m15, "mom60": m60,
                            })
                            continue

                        # Overlap check
                        if has_overlapping_position(positions, mkt, signal.direction):
                            scan_state["log"].appendleft({
                                "type": "skip", "dir": signal.direction,
                                "reason": "overlap",
                                "mom15": m15, "mom60": m60,
                            })
                            continue

                        # Direction cap
                        up_count = sum(1 for p in positions if p.direction == "UP")
                        down_count = sum(1 for p in positions if p.direction == "DOWN")
                        if signal.direction == "UP" and up_count >= MAX_DIR_POS:
                            continue
                        if signal.direction == "DOWN" and down_count >= MAX_DIR_POS:
                            continue

                        # Bankroll check
                        if bankroll < signal.size_usdc + sum(p.size_usdc for p in positions):
                            continue

                        shares = math.ceil(signal.size_usdc / max(entry_price, 0.001) * 10000) / 10000

                        try:
                            order_id = place_order(tok, entry_price, signal.size_usdc, dry)
                        except ValueError as e:
                            log.error("Order validation: %s", e)
                            continue

                        if order_id is None:
                            continue

                        actual_shares = shares
                        if not dry:
                            buy_fill, filled_shares = poll_order_status(order_id, timeout=20.0)
                            if buy_fill == "cancelled":
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": signal.direction,
                                    "reason": "BUY cancelled",
                                    "mom15": m15, "mom60": m60,
                                })
                                tg(f"BUY cancelled {signal.direction}")
                                continue
                            if filled_shares is not None:
                                actual_shares = filled_shares
                            bankroll = sync_wallet_usdc(force=True)

                        pos = Position(
                            market=mkt, token_id=tok, direction=signal.direction,
                            entry_price=entry_price, size_usdc=signal.size_usdc,
                            shares_held=actual_shares,
                            order_id=order_id,
                            entry_crypto=FEED.current,
                            score=abs(signal.window_delta),
                            peak_price=entry_price,
                            trail_sl=0.0,  # no trailing in v6
                            mom15_at_entry=m15,
                            rsi_at_entry=FEED.rsi(),
                            kelly_used=signal.size_usdc,
                            fee_rate_used=get_fee_rate(tok),
                            holding_expiry=True,  # always hold
                        )
                        positions.append(pos)
                        save_checkpoint(positions)
                        stats.snipes += 1
                        blacklist.add(mkt.condition_id)
                        scan_state["log"].appendleft({
                            "type": "snipe", "dir": signal.direction,
                            "score": abs(signal.window_delta),
                            "mom15": m15, "mom60": m60,
                            "reason": signal.reason,
                            "size": round(signal.size_usdc, 2),
                            "entry": round(entry_price, 4),
                        })
                        tg(f"{signal.verdict} {signal.direction} | "
                           f"delta={signal.window_delta:+.4f}% | "
                           f"${signal.size_usdc:.0f} | entry={entry_price:.3f}")
                        log.info("SNIPE %s %s delta=%.4f%% $%.0f entry=%.3f",
                                 signal.verdict, signal.direction,
                                 signal.window_delta, signal.size_usdc, entry_price)

                live.update(make_dashboard(
                    positions, stats, dry, countdown,
                    m15, m30, m60, bankroll, scan_state,
                ))

                # Fast scan (every 1s) — we need to catch the late entry window
                countdown = 1.0
                while countdown > 0 and not SHUTDOWN_EVENT.is_set():
                    live.update(make_dashboard(
                        positions, stats, dry, countdown,
                        m15, m30, m60, bankroll, scan_state,
                    ))
                    time.sleep(0.1)
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
        log.warning("%d position(s) still open", len(positions))
        console.print(f"[bold yellow]{len(positions)} position(s) open — logged SHUTDOWN_OPEN[/]")
        for pos in positions:
            log_trade(pos, pos.current_price, "SHUTDOWN_OPEN", stats,
                     FEED.current, 0.0)

    console.print(Panel(
        f"[bold]SESSION COMPLETE[/]\n"
        f" Duration : {stats.elapsed}\n"
        f" P&L      : [bold {'green' if stats.total_pnl >= 0 else 'red'}]{stats.total_pnl:+.2f} USDC[/]\n"
        f" Win rate : [cyan]{stats.win_rate:.1f}%[/] ({stats.wins}W/{stats.losses}L)\n"
        f" Held exp : [cyan]{stats.held_expiry}[/]\n"
        f" Snipes   : {stats.snipes}  Scans: {stats.scans}\n"
        f" CSV      : {TRADES_CSV}",
        title="SUMMARY", border_style="#f7931a",
    ))


def cli():
    def _shutdown_handler(sig, frame):
        SHUTDOWN_EVENT.set()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    p = argparse.ArgumentParser(description="Crypto Pulse v6.0 — Late Trend")
    p.add_argument("--live", action="store_true", help="Live mode (default: sim)")
    p.add_argument("--scan", type=int, default=SCAN_INTERVAL, help=f"Scan interval (default: {SCAN_INTERVAL})")
    p.add_argument("--max-loss", type=float, default=MAX_DAILY_LOSS, help=f"Circuit breaker (default: {MAX_DAILY_LOSS})")
    p.add_argument("--log-level", type=str, default="INFO")
    args = p.parse_args()

    SETTINGS.scan_interval = args.scan
    SETTINGS.max_daily_loss = args.max_loss

    if args.live:
        print("\nLIVE MODE in 5s — Ctrl+C to cancel...")
        time.sleep(5)

    run(dry=not args.live, log_level=args.log_level)


if __name__ == "__main__":
    cli()
