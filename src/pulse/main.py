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
    MIN_MOM_GLOBAL, TP_DELTA, SL_DELTA, TRAILING_STOP, TRAILING_DISTANCE, BTC_TREND_THRESHOLD,
    HOLD_THRESHOLD, HOLD_MIN_REMAINING, HOLD_ENABLED, RSI_PERIOD,
    TRADES_CSV, POSITIONS_CHECKPOINT,
    Position, SessionStats,
)
from pulse.feed import FEED, SPIKE_INTERRUPT, start_ws_btc, rest_fallback_btc, spike_monitor_loop, prewarm_connections
from pulse.strategy import compute_score, kelly_size, vote_direction, has_overlapping_position
from pulse.orders import (
    place_order, poll_order_status, close_position, place_limit_sell,
    cancel_all_pending, cancel_order_safe,
    get_ob, get_ob_multi, get_cached_markets, get_fee_rate, get_tick_size,
    sync_wallet_usdc, redeem_loop, prefetch_loop, shutdown_ob_pool,
)
from pulse.risk import ExpiringBlacklist, save_checkpoint, load_checkpoint, reconcile_positions
from pulse.dashboard import make_dashboard
from pulse.logger import setup_logging, init_csv, log_trade, tg, notify, notify_recap, fee_usdc
import pulse.token_ws as token_ws

import logging
log = logging.getLogger(__name__)


# ── Recap thread ────────────────────────────────────────────────────────────
def _recap_loop(stats: SessionStats, interval_h: float = 2.0):
    interval = interval_h * 3600
    while not SHUTDOWN_EVENT.is_set():
        SHUTDOWN_EVENT.wait(timeout=interval)
        if not SHUTDOWN_EVENT.is_set():
            notify_recap(stats)


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
                        break
                time.sleep(0.05)
        except Exception:
            pass


# ── Main loop ────────────────────────────────────────────────────────────────
def run(dry: bool = True, hold_enabled: bool = True):
    console = Console(force_terminal=True)
    setup_logging()
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
        f"[bold #f7931a]CRYPTO PULSE SNIPER v6.0-BTC[/]\n"
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
    token_ws.start(SHUTDOWN_EVENT)

    console.print("[dim]Connecting to Binance WS...[/]")
    time.sleep(3)

    # Load positions from checkpoint (crash recovery)
    positions: List[Position] = load_checkpoint()
    _missed_pnl = 0.0
    if positions:
        positions, _missed_pnl = reconcile_positions(positions)
        save_checkpoint(positions)
        # Re-subscribe recovered positions to Token WS for fast SL/TP detection
        for _rp in positions:
            _sl = _rp.trail_sl if _rp.trail_sl > 0 else round(_rp.entry_price - SL_DELTA, 4)
            token_ws.subscribe(_rp.token_id, _sl, round(_rp.entry_price + TP_DELTA, 4))
            log.info("WS re-subscribed recovered pos: %s %s", _rp.direction, _rp.token_id[:16])

    stats = SessionStats()
    if _missed_pnl:
        stats.total_pnl += _missed_pnl
        stats.btc_pnl += _missed_pnl
    threading.Thread(target=lambda: _recap_loop(stats), name="recap", daemon=True).start()
    blacklist = ExpiringBlacklist(ttl=360.0)
    bankroll = sync_wallet_usdc(force=True)
    if bankroll <= 0:
        bankroll = BANKROLL
        log.warning("Wallet not found — fallback %.0f$", BANKROLL)
    else:
        log.info("pUSD on-chain: %.2f$", bankroll)

    countdown = float(SETTINGS.scan_interval)
    last_spike_ts = 0.0
    notify("STARTUP", f"v6.0-BTC {'LIVE' if not dry else 'SIM'} démarré",
           **{"Mode": "LIVE" if not dry else "SIM",
              "Score min": f"{SETTINGS.min_score:.2f}",
              "Circuit br.": f"-{SETTINGS.max_daily_loss}$"})

    try:
        with Live(console=console, refresh_per_second=10, screen=True) as live:
            while not SHUTDOWN_EVENT.is_set():
                max_dl = SETTINGS.max_daily_loss

                # Circuit breaker
                if stats.total_pnl <= -max_dl:
                    notify("CIRCUIT", "Circuit breaker déclenché",
                           **{"Perte": f"{stats.total_pnl:+.2f}$",
                              "Trades": str(stats.total),
                              "Winrate": f"{stats.win_rate:.1f}%"})
                    log.critical("Circuit breaker triggered: %.2f$", stats.total_pnl)
                    break

                stats.scans += 1
                scan_state["best_score"] = 0.0
                SPIKE_INTERRUPT.clear()
                bankroll = sync_wallet_usdc()
                m15, m30, m60 = FEED.momentum_all()

                # ── WS-triggered SL/TP events (priority — ~100ms reaction) ──
                ws_eq = token_ws.get_event_queue()
                while not ws_eq.empty():
                    try:
                        ws_tid, ws_reason, ws_price = ws_eq.get_nowait()
                    except Exception:
                        break
                    pos_hit = next((p for p in positions if p.token_id == ws_tid), None)
                    if pos_hit is None:
                        continue  # already closed
                    if pos_hit.holding_expiry and ws_reason == "SL":
                        # Don't exit hold positions on SL via WS —
                        # the hold SL check in the loop handles this
                        token_ws.subscribe(ws_tid,
                                           pos_hit.trail_sl,
                                           round(pos_hit.entry_price + TP_DELTA, 4))
                        continue
                    # Cancel pre-placed SL order to avoid double-sell
                    if pos_hit.sl_order_id:
                        if not dry:
                            cancel_order_safe(pos_hit.sl_order_id)
                        pos_hit.sl_order_id = ""
                    ob_ws = get_ob(pos_hit.token_id)
                    if ob_ws:
                        exit_price = ob_ws["bb"] if ws_reason == "SL" else ob_ws["ba"]
                    else:
                        exit_price = ws_price
                    reason_ws = (f"SL {exit_price:.3f}(<{round(pos_hit.entry_price - SL_DELTA, 4):.3f})"
                                 if ws_reason == "SL"
                                 else f"TP {exit_price:.3f}(>{round(pos_hit.entry_price + TP_DELTA, 4):.3f})")
                    if not dry:
                        close_id, fill_status = close_position(
                            pos_hit.token_id, exit_price, pos_hit.shares_held, dry)
                        pos_hit.close_order_id = close_id if close_id else "failed"
                        pos_hit.close_fill = fill_status
                        if close_id is None or fill_status not in ("filled", "partial"):
                            log.warning("WS close failed: %s %s — keeping position",
                                        ws_reason, pos_hit.direction)
                            # Re-subscribe so we keep monitoring
                            token_ws.subscribe(ws_tid,
                                               round(pos_hit.entry_price - SL_DELTA, 4),
                                               round(pos_hit.entry_price + TP_DELTA, 4))
                            continue
                        bankroll = sync_wallet_usdc(force=True)
                    else:
                        pos_hit.close_order_id = f"dry_ws_{int(time.time()*1000)}"
                        pos_hit.close_fill = "filled"
                    pos_hit.current_price = exit_price
                    pnl = log_trade(pos_hit, exit_price, reason_ws, stats,
                                    FEED.current, SETTINGS.min_score,
                                    mom15_exit=m15, rsi_exit=FEED.rsi())
                    stats.total_pnl += pnl
                    stats.total += 1
                    if pnl >= 0:
                        stats.wins += 1
                    else:
                        stats.losses += 1
                    scan_state["log"].appendleft({
                        "type": "exit", "dir": pos_hit.direction,
                        "reason": reason_ws, "pnl": round(pnl, 2),
                        "pnl_pct": round(pos_hit.pnl_pct * 100, 1),
                        "size": round(pos_hit.size_usdc, 2),
                        "entry": round(pos_hit.entry_price, 4),
                        "mom15": m15, "mom60": m60,
                    })
                    _level = "WIN" if pnl >= 0 else "LOSS"
                    notify(_level, f"{_level} — BTC {pos_hit.direction}",
                           **{"P&L net": f"{pnl:+.2f}$",
                              "Raison": f"{ws_reason} (WS)",
                              "Durée": f"{pos_hit.elapsed_min:.1f} min",
                              "Score entrée": f"{pos_hit.score:.3f}",
                              "Fill": pos_hit.close_fill,
                              "Session P&L": f"{stats.total_pnl:+.2f}$"})
                    positions.remove(pos_hit)
                    save_checkpoint(positions)

                # ── Exits ────────────────────────────────────────────────
                closed: List[Position] = []
                if positions:
                    exit_obs = get_ob_multi([p.token_id for p in positions])
                    for pos in positions:
                        ob = exit_obs.get(pos.token_id)
                        if ob:
                            pos.current_price = ob["bb"] or ob["mid"]

                        # ── Awaiting expiry resolution (non-blocking) ─────────
                        if pos.awaiting_resolution:
                            _p = pos.current_price
                            if _p >= 0.90:
                                pos.current_price = 1.0
                                pos.awaiting_resolution = False
                            elif _p <= 0.10:
                                pos.current_price = 0.0
                                pos.awaiting_resolution = False
                            else:
                                continue  # still uncertain, check next scan
                            # Resolved — cancel any stale SL, log and close
                            if pos.sl_order_id:
                                if not dry:
                                    cancel_order_safe(pos.sl_order_id)
                                pos.sl_order_id = ""
                            if not dry:
                                bankroll = sync_wallet_usdc(force=True)
                                tg(f"EXPIRY {pos.direction} — resolved")
                            pnl = log_trade(pos, pos.current_price, "EXPIRY", stats,
                                            FEED.current, SETTINGS.min_score,
                                            mom15_exit=m15, rsi_exit=FEED.rsi())
                            stats.total_pnl += pnl
                            stats.total += 1
                            if pnl >= 0:
                                stats.wins += 1
                            else:
                                stats.losses += 1
                            scan_state["log"].appendleft({
                                "type": "exit", "dir": pos.direction,
                                "reason": "EXPIRY", "pnl": round(pnl, 2),
                                "pnl_pct": round(pos.pnl_pct * 100, 1),
                                "size": round(pos.size_usdc, 2),
                                "entry": round(pos.entry_price, 4),
                                "mom15": m15, "mom60": m60,
                            })
                            _level = "WIN" if pnl >= 0 else "LOSS"
                            notify(_level, f"{_level} — BTC {pos.direction}",
                                   **{"P&L net": f"{pnl:+.2f}$",
                                      "Raison": "EXPIRY",
                                      "Durée": f"{pos.elapsed_min:.1f} min",
                                      "Score entrée": f"{pos.score:.3f}",
                                      "Fill": "expiry",
                                      "Session P&L": f"{stats.total_pnl:+.2f}$"})
                            token_ws.unsubscribe(pos.token_id)
                            closed.append(pos)
                            continue

                        rem_sec = pos.market.remaining_sec

                        # ── Check pre-placed SL order fill ───────────────────
                        # The SL order sits in the CLOB book and executes at the
                        # exact SL price — no scan-loop slippage.
                        if pos.sl_order_id and not pos.holding_expiry:
                            sl_status, _ = poll_order_status(pos.sl_order_id, timeout=1.0)
                            if sl_status in ("filled", "partial"):
                                # Use the actual SL execution price, not current OB bid
                                exit_price = pos.trail_sl if pos.trail_sl > 0 else round(pos.entry_price - SL_DELTA, 4)
                                reason_sl = f"SL {exit_price:.3f}(<{round(pos.entry_price - SL_DELTA, 4):.3f})"
                                pos.close_order_id = pos.sl_order_id
                                pos.close_fill = sl_status
                                pos.sl_order_id = ""
                                pos.current_price = exit_price
                                token_ws.unsubscribe(pos.token_id)
                                if not dry:
                                    bankroll = sync_wallet_usdc(force=True)
                                pnl = log_trade(pos, exit_price, reason_sl, stats,
                                                FEED.current, SETTINGS.min_score,
                                                mom15_exit=m15, rsi_exit=FEED.rsi())
                                stats.total_pnl += pnl
                                stats.total += 1
                                if pnl >= 0:
                                    stats.wins += 1
                                else:
                                    stats.losses += 1
                                scan_state["log"].appendleft({
                                    "type": "exit", "dir": pos.direction,
                                    "reason": reason_sl, "pnl": round(pnl, 2),
                                    "pnl_pct": round(pos.pnl_pct * 100, 1),
                                    "size": round(pos.size_usdc, 2),
                                    "entry": round(pos.entry_price, 4),
                                    "mom15": m15, "mom60": m60,
                                })
                                _level = "WIN" if pnl >= 0 else "LOSS"
                                notify(_level, f"{_level} — BTC {pos.direction}",
                                       **{"P&L net": f"{pnl:+.2f}$",
                                          "Raison": "SL (book)",
                                          "Durée": f"{pos.elapsed_min:.1f} min",
                                          "Score entrée": f"{pos.score:.3f}",
                                          "Fill": sl_status,
                                          "Session P&L": f"{stats.total_pnl:+.2f}$"})
                                closed.append(pos)
                                continue

                        # Hold-to-expiry activation
                        if (hold_enabled
                                and not pos.holding_expiry
                                and pos.current_price >= HOLD_THRESHOLD
                                and rem_sec >= HOLD_MIN_REMAINING):
                            # Cancel pre-placed SL before holding to expiry
                            if pos.sl_order_id:
                                if not dry:
                                    cancel_order_safe(pos.sl_order_id)
                                pos.sl_order_id = ""
                            pos.holding_expiry = True
                            stats.held_expiry += 1
                            scan_state["log"].appendleft({
                                "type": "skip", "dir": pos.direction,
                                "reason": f"HOLD>{HOLD_THRESHOLD:.2f}@{pos.current_price:.3f}",
                                "mom15": m15, "mom60": m60,
                            })
                            notify("HOLD", f"Hold-to-expiry activé {pos.direction}",
                                   **{"Prix": f"{pos.current_price:.3f}",
                                      "Restant": f"{rem_sec:.0f}s",
                                      "Score": f"{pos.score:.3f}"})
                            log.info("HOLD activated: %s @%.3f %.0fs remaining",
                                     pos.direction, pos.current_price, rem_sec)

                        # Always track peak and trough for analytics
                        _price_moved_up = pos.current_price > pos.peak_price
                        if _price_moved_up:
                            pos.peak_price = pos.current_price
                        if pos.trough_price == 0.0 or pos.current_price < pos.trough_price:
                            pos.trough_price = pos.current_price

                        # Trailing stop (only for hold protection — not for normal exits
                        # which are handled by the pre-placed SL order)
                        if TRAILING_STOP and _price_moved_up:
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
                                # Fallback SL — fires when WS missed the event or
                                # pre-placed order didn't fill (e.g. no buyer / sim mode)
                                reason = f"SL {pos.current_price:.3f}(<{eff_sl:.3f})"
                            elif pos.market.remaining_min <= 0.15:
                                reason = "EXPIRY"

                        if reason:
                            # Cancel pre-placed SL order before any manual close
                            if pos.sl_order_id:
                                if not dry:
                                    cancel_order_safe(pos.sl_order_id)
                                pos.sl_order_id = ""

                            ob2 = get_ob(pos.token_id) if reason != "EXPIRY" else ob
                            if ob2:
                                pos.current_price = ob2["mid"]

                            if reason != "EXPIRY":
                                # ── Safety: resolve any stale pending close order ──────
                                _skip_placement = False
                                _stale_id = pos.close_order_id
                                if _stale_id and _stale_id not in ("", "failed", "expiry_resolution"):
                                    prev_status, _ = poll_order_status(_stale_id, timeout=3.0)
                                    if prev_status in ("filled", "partial"):
                                        pos.close_fill = prev_status
                                        exit_price = ob2["bb"] if ob2 else pos.current_price
                                        pos.current_price = exit_price
                                        _skip_placement = True
                                        log.info("Stale close order %s already %s — using it",
                                                 _stale_id[:16], prev_status)
                                    else:
                                        if not dry:
                                            cancel_order_safe(_stale_id)
                                        pos.close_order_id = ""
                                        log.warning("Cancelled stale close order %s, retrying",
                                                    _stale_id[:16])

                                if not _skip_placement:
                                    if ob2 is None:
                                        scan_state["log"].appendleft({
                                            "type": "skip", "dir": pos.direction,
                                            "reason": "CLOSE SKIP — no OB",
                                            "mom15": m15, "mom60": m60,
                                        })
                                        log.warning("Close skipped (no OB): %s %s",
                                                    pos.direction, pos.token_id[:16])
                                        continue

                                    exit_price = max(ob2["bb"], 0.01)
                                    try:
                                        close_id, fill_status = close_position(
                                            pos.token_id, exit_price, pos.shares_held, dry)
                                    except ValueError as _ve:
                                        log.error("Close skipped — invalid price: %s", _ve)
                                        continue
                                    pos.close_order_id = close_id if close_id else "failed"
                                    pos.close_fill = fill_status
                                    if not dry:
                                        if close_id is None:
                                            scan_state["log"].appendleft({
                                                "type": "skip", "dir": pos.direction,
                                                "reason": "CLOSE FAILED — kept",
                                                "mom15": m15, "mom60": m60,
                                            })
                                            log.error("Close failed for %s %s",
                                                      pos.direction, pos.token_id[:16])
                                            continue
                                        if fill_status not in ("filled", "partial"):
                                            scan_state["log"].appendleft({
                                                "type": "skip", "dir": pos.direction,
                                                "reason": f"SELL not fill ({fill_status})",
                                                "mom15": m15, "mom60": m60,
                                            })
                                            tg(f"SELL not fill {pos.direction}")
                                            log.warning("SELL not filled: %s %s",
                                                        pos.direction, fill_status)
                                            continue
                                        bankroll = sync_wallet_usdc(force=True)
                                pos.current_price = exit_price
                            else:
                                pos.close_order_id = "expiry_resolution"
                                pos.close_fill = "expiry"
                                # Snap to clean resolution price (0.0 or 1.0)
                                if pos.current_price >= 0.90:
                                    pos.current_price = 1.0
                                elif pos.current_price <= 0.10:
                                    pos.current_price = 0.0
                                else:
                                    # Uncertain — defer to next scan (non-blocking)
                                    log.info("EXPIRY uncertain price=%.3f — awaiting resolution",
                                             pos.current_price)
                                    pos.awaiting_resolution = True
                                    continue  # skip logging, keep position alive
                                if not dry:
                                    time.sleep(5)
                                    bankroll = sync_wallet_usdc(force=True)
                                    tg(f"EXPIRY {pos.direction} — auto resolution")

                            pnl = log_trade(pos, pos.current_price, reason, stats,
                                           FEED.current, SETTINGS.min_score,
                                           mom15_exit=m15, rsi_exit=FEED.rsi())
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
                            _level = "WIN" if pnl >= 0 else "LOSS"
                            notify(_level, f"{_level} — BTC {pos.direction}",
                                   **{"P&L net": f"{pnl:+.2f}$",
                                      "Raison": reason.split()[0],
                                      "Durée": f"{pos.elapsed_min:.1f} min",
                                      "Score entrée": f"{pos.score:.3f}",
                                      "Fill": pos.close_fill,
                                      "Session P&L": f"{stats.total_pnl:+.2f}$"})
                            token_ws.unsubscribe(pos.token_id)
                            closed.append(pos)

                    for c in closed:
                        positions.remove(c)
                    if closed:
                        save_checkpoint(positions)

                if abs(m15) >= SPIKE_THRESHOLD:
                    stats.spikes_seen += 1

                # ── Entry scan ───────────────────────────────────────────
                # Guard: skip new entries when price feed is degraded
                _feed_ok = FEED.ws_status in ("live", "rest_only")
                if not _feed_ok and not dry:
                    scan_state["log"].appendleft({
                        "type": "skip", "dir": "-",
                        "reason": f"feed {FEED.ws_status} — entrées bloquées",
                        "mom15": m15, "mom60": m60,
                    })
                if len(positions) < MAX_OPEN_POS and (_feed_ok or dry):
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
                        # Track available funds locally to avoid stale bankroll
                        # when multiple entries happen in the same scan cycle
                        _available = bankroll - sum(p.size_usdc for p in positions)

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

                            # BTC trend filter — block entries against sustained trend
                            if direction == "DOWN" and m60 > BTC_TREND_THRESHOLD:
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": f"btc_trend UP m60={m60:.3f}>{BTC_TREND_THRESHOLD}",
                                    "mom15": m15, "mom60": m60,
                                })
                                continue
                            if direction == "UP" and m60 < -BTC_TREND_THRESHOLD:
                                scan_state["log"].appendleft({
                                    "type": "skip", "dir": direction,
                                    "reason": f"btc_trend DOWN m60={m60:.3f}<-{BTC_TREND_THRESHOLD}",
                                    "mom15": m15, "mom60": m60,
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
                            if _available < size:
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
                                    # Order not confirmed — cancel and blacklist to avoid re-entry
                                    # (cancel may fail if order was already matched on-chain)
                                    cancel_order_safe(order_id)
                                    blacklist.add(mkt.condition_id)
                                    scan_state["log"].appendleft({
                                        "type": "skip", "dir": direction,
                                        "reason": "BUY timeout — annulé + blacklist",
                                        "mom15": m15, "mom60": m60,
                                    })
                                    notify("INFO", f"BUY timeout annulé {direction}")
                                    log.warning("BUY timed out — cancelled and blacklisted: %s", direction)
                                    continue
                                if filled_shares is not None:
                                    actual_shares = filled_shares
                                    log.info("Fill verified: %.4f shares (local est: %.4f)",
                                             filled_shares, shares)
                                bankroll = sync_wallet_usdc(force=True)

                            sl_price = round(entry_p - SL_DELTA, 4)
                            sl_oid = place_limit_sell(tok, sl_price, actual_shares, dry)
                            if sl_oid:
                                log.info("SL pre-placed: %s @%.3f", sl_oid[:16], sl_price)
                            else:
                                log.warning("SL pre-placement failed for %s — scan-based fallback",
                                            direction)

                            pos = Position(
                                market=mkt, token_id=tok, direction=direction,
                                entry_price=entry_p, size_usdc=size,
                                shares_held=actual_shares,
                                order_id=order_id,
                                entry_crypto=FEED.current, score=score,
                                peak_price=entry_p,
                                trough_price=entry_p,
                                trail_sl=sl_price,
                                sl_order_id=sl_oid or "",
                                mom15_at_entry=m15,
                                mom30_at_entry=m30,
                                mom60_at_entry=m60,
                                rsi_at_entry=FEED.rsi(),
                                kelly_used=size,
                                fee_rate_used=get_fee_rate(tok),
                                spread_at_entry=round(ob["spread"], 4),
                                ob_depth_at_entry=round(ob["total_d"], 2),
                                spike_size_at_entry=round(abs(m15), 6),
                                session_pnl_before=round(stats.total_pnl, 4),
                                market_remaining_min_at_entry=round(remaining, 2),
                                window_delta_at_entry=round(wd, 4),
                            )
                            positions.append(pos)
                            _available -= size  # deduct immediately for next candidate
                            save_checkpoint(positions)
                            # Subscribe to real-time WS for fast SL/TP detection
                            token_ws.subscribe(tok, sl_price, round(entry_p + TP_DELTA, 4))
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
                            notify("SNIPE", f"Snipe BTC {direction}",
                                   **{"Score": f"{score:.3f}",
                                      "Kelly": f"{size:.1f}$",
                                      "Entrée": f"{entry_p:.3f}",
                                      "RSI": f"{pos.rsi_at_entry:.0f}",
                                      "Mom15s": f"{m15:+.4f}%"})
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
            # Cancel pre-placed SL order and unsubscribe WS before logging shutdown
            token_ws.unsubscribe(pos.token_id)
            if pos.sl_order_id and not dry:
                cancel_order_safe(pos.sl_order_id)
                pos.sl_order_id = ""
            pnl = log_trade(pos, pos.current_price, "SHUTDOWN_OPEN", stats,
                            FEED.current, SETTINGS.min_score,
                            mom15_exit=FEED.momentum_all()[0], rsi_exit=FEED.rsi())
            stats.total_pnl += pnl
            stats.total += 1
            if pnl >= 0:
                stats.wins += 1
            else:
                stats.losses += 1

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


# ── Validate mode ────────────────────────────────────────────────────────────
def validate():
    """
    Test the full CLOB path without placing any real order.
    Checks: credentials, market fetch, OB, tick size, fee rate,
    order construction (signed but NOT posted), wallet balance.
    """
    import traceback
    console = Console(force_terminal=True)
    ok = True

    def _check(label: str, fn):
        nonlocal ok
        try:
            result = fn()
            console.print(f"  [green]✓[/] {label}: [dim]{result}[/]")
            return result
        except Exception as e:
            console.print(f"  [red]✗[/] {label}: [bold red]{e}[/]")
            ok = False
            return None

    console.print(Panel("[bold]VALIDATE MODE — aucun ordre réel ne sera envoyé[/]",
                        border_style="yellow"))

    # 1. CLOB client init + credentials
    console.print("\n[bold]1. Connexion CLOB[/]")
    cl = _check("Init ClobClient", lambda: (
        __import__("pulse.orders", fromlist=["get_clob_client"]).get_clob_client()
        or (_ for _ in ()).throw(RuntimeError("get_clob_client() returned None"))
    ))

    # 2. Wallet balance
    console.print("\n[bold]2. Wallet[/]")
    _check("Balance USDC on-chain",
           lambda: f"{sync_wallet_usdc(force=True):.2f} USDC")

    # 3. Fetch active BTC market
    console.print("\n[bold]3. Marché BTC 5min[/]")
    from pulse.orders import fetch_markets_btc, get_ob, get_fee_rate
    from pulse.feed import start_ws_btc
    import threading as _threading
    _threading.Thread(target=start_ws_btc, daemon=True).start()
    time.sleep(2)

    mkts = _check("Fetch marchés BTC", lambda: (
        fetch_markets_btc()
        or (_ for _ in ()).throw(RuntimeError("Aucun marché BTC trouvé"))
    ))
    mkt = mkts[0] if mkts else None

    if mkt:
        _check("Marché slug", lambda: mkt.slug)
        _check("Temps restant", lambda: f"{mkt.remaining_min:.1f} min")

        tok = mkt.yes_token
        _check("Yes token ID", lambda: tok[:20] + "...")

        # 4. Order book
        console.print("\n[bold]4. Carnet d'ordres[/]")
        ob = _check("GET /book", lambda: (
            get_ob(tok)
            or (_ for _ in ()).throw(RuntimeError("OB vide ou inaccessible"))
        ))
        if ob:
            _check("Spread", lambda: f"{ob['spread']:.4f} ({'OK' if ob['spread'] < 0.03 else 'LARGE'})")
            _check("Best bid / ask", lambda: f"{ob['bb']:.3f} / {ob['ba']:.3f}")
            _check("Depth total", lambda: f"{ob['total_d']:.0f} USDC")

        # 5. Tick size
        console.print("\n[bold]5. Tick size & fee rate[/]")
        _check("GET /tick-size", lambda: (
            __import__("requests").get(
                "https://clob.polymarket.com/tick-size",
                params={"token_id": tok}, timeout=4
            ).json()["minimum_tick_size"]
        ))
        _check("GET /fee-rate", lambda: f"{get_fee_rate(tok):.4f} ({get_fee_rate(tok)*100:.2f}%)")

        # 6. Authenticated API call (verifies HMAC creds are valid)
        console.print("\n[bold]6. Credentials CLOB (appel authentifié réel)[/]")
        if cl:
            def _test_auth():
                # get_orders requires Level 2 auth — same creds used by place/close
                result = cl.get_open_orders()
                return f"get_orders() OK — {len(result) if isinstance(result, list) else '?'} ordre(s) en cours"
            _check("GET /orders (Level 2 auth)", _test_auth)
        else:
            console.print("  [yellow]⚠[/] Skipped (pas de client)")

        # 7. Build + sign order WITHOUT posting
        console.print("\n[bold]7. Construction d'ordre (signé, NON envoyé)[/]")
        if cl and ob:
            def _build_order():
                from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions
                from py_clob_client_v2.order_builder.constants import BUY, SELL
                ts = get_tick_size(tok)
                buy_price = min(round(ob["ba"] + float(ts), 2), 0.98)
                buy_args = OrderArgsV2(token_id=tok, price=buy_price, size=1.0, side=BUY)
                cl.create_order(buy_args, options=PartialCreateOrderOptions(tick_size=ts))
                sell_price = max(round(ob["bb"] - float(ts), 2), float(ts))
                sell_args = OrderArgsV2(token_id=tok, price=sell_price, size=1.0, side=SELL)
                cl.create_order(sell_args, options=PartialCreateOrderOptions(tick_size=ts))
                return f"BUY@{buy_price} ok  SELL@{sell_price} ok  ts={ts} (non postes)"
            _check("create_order BUY + SELL", _build_order)
        else:
            console.print("  [yellow]⚠[/] Skipped (pas de client ou OB)")

    # 7. Summary
    console.print()
    if ok:
        console.print(Panel(
            "[bold green]Tous les checks sont passés.[/]\n"
            "Le bot peut être lancé en [bold]--live[/].",
            border_style="green",
        ))
    else:
        console.print(Panel(
            "[bold red]Des erreurs ont été détectées.[/]\n"
            "Corrige-les avant de lancer en [bold]--live[/].",
            border_style="red",
        ))
    return ok


# ── CLI entrypoint ───────────────────────────────────────────────────────────
def cli():
    # Signal handlers for graceful shutdown
    def _shutdown_handler(sig, frame):
        SHUTDOWN_EVENT.set()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    p = argparse.ArgumentParser(description="Crypto Pulse Sniper v6.0-BTC")
    p.add_argument("--validate", action="store_true", help="Test CLOB connectivity sans placer d'ordre")
    p.add_argument("--live", action="store_true", help="Live mode (default: simulation)")
    p.add_argument("--score", type=float, default=MIN_SCORE, help=f"Min score (default: {MIN_SCORE})")
    p.add_argument("--auto", action="store_true", help="Auto-adaptive score")
    p.add_argument("--window", type=str, default=None, help="Entry window e.g. 0.3,4.5")
    p.add_argument("--scan", type=int, default=SCAN_INTERVAL, help=f"Scan interval secs (default: {SCAN_INTERVAL})")
    p.add_argument("--max-loss", type=float, default=MAX_DAILY_LOSS, help=f"Circuit breaker $ (default: {MAX_DAILY_LOSS})")
    p.add_argument("--no-hold", action="store_true", help="Disable hold-to-expiry")
    p.add_argument("--log-level", type=str, default="INFO", help="Log level (default: INFO)")
    args = p.parse_args()

    if args.validate:
        setup_logging(args.log_level if hasattr(args, "log_level") else "WARNING")
        sys.exit(0 if validate() else 1)

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

    run(dry=not args.live, hold_enabled=hold_enabled)


if __name__ == "__main__":
    cli()
