"""Dashboard — Rich TUI layout."""

from __future__ import annotations

from typing import List

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from pulse.config import (
    SETTINGS, SPIKE_THRESHOLD, MAX_OPEN_POS, MAX_SPREAD,
    TP_DELTA, SL_DELTA, TRAILING_DISTANCE, HOLD_THRESHOLD, HOLD_MIN_REMAINING,
    MIN_ENTRY_PRICE, MAX_ENTRY_PRICE, ENTRY_WINDOW_MIN, ENTRY_WINDOW_MAX,
    RSI_OVERBOUGHT, RSI_OVERSOLD, Position, SessionStats,
)
from pulse.feed import FEED
from pulse.logger import fee_usdc


def make_dashboard(
    positions: List[Position],
    stats: SessionStats,
    dry: bool,
    countdown: float,
    m15: float, m30: float, m60: float,
    bankroll: float,
    scan_state: dict,
) -> Layout:
    cur = FEED.current
    ws_st = FEED.ws_status
    rsi_v = FEED.rsi()
    max_dl = SETTINGS.max_daily_loss
    si = SETTINGS.scan_interval

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="spikes", size=10),
        Layout(name="controls", size=3),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="feed", ratio=3),
        Layout(name="center", ratio=5),
        Layout(name="right", ratio=3),
    )
    layout["right"].split_column(
        Layout(name="pos", ratio=4),
        Layout(name="sess", ratio=3),
        Layout(name="perf", ratio=3),
    )

    ws_col = "green" if ws_st == "live" else ("yellow" if ws_st in ("connecting", "rest_only") else "red")
    spk_now = abs(m15) >= SPIKE_THRESHOLD
    mode_text = ("LIVE", "bold red") if not dry else ("SIM", "bold cyan")
    cb_warn = stats.total_pnl <= -(max_dl * 0.7)
    rsi_c = "bold green" if rsi_v > RSI_OVERBOUGHT else ("bold red" if rsi_v < RSI_OVERSOLD else "dim")

    # Header
    layout["header"].update(Panel(
        Text.assemble(
            (" CRYPTO PULSE v5.0-BTC ", "bold #f7931a"),
            (" | scan #", "dim"), (str(stats.scans), "white"),
            (" | ", "dim"), (stats.elapsed, "yellow"),
            (" | next: ", "dim"), (f"{int(countdown)}s", "bold cyan"),
            (" | ", "dim"), mode_text,
            (" | WS:", "dim"), (ws_st[:4], ws_col),
            (f" {FEED.latency:.0f}ms {FEED.ticks_per_sec()}t/s", "dim"),
            (" | RSI:", "dim"), (f"{rsi_v:.0f}", rsi_c),
            (" | CB:", "dim"), (f"{stats.total_pnl:+.1f}$/{-max_dl:.0f}$",
                                "bold red" if cb_warn else "dim"),
        ),
        style="on #080a0c", border_style="#f7931a" if spk_now else "#1a2030",
    ))

    # BTC price panel
    active_markets = scan_state.get("active_markets", [])
    best_score = scan_state.get("best_score", 0.0)
    avg_edge = scan_state.get("avg_edge", 0.0)

    recent = FEED.last_n(14)
    price_tbl = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    price_tbl.add_column(justify="left", no_wrap=True)
    price_tbl.add_column(justify="right", no_wrap=True)
    for i in range(0, len(recent) - 1, 2):
        p1 = recent[i]
        p2 = recent[i + 1] if i + 1 < len(recent) else p1
        c1 = "green" if p1 >= (recent[i - 1] if i > 0 else p1) else "red"
        c2 = "green" if p2 >= p1 else "red"
        arrow1 = "^" if c1 == "green" else "v"
        arrow2 = "^" if c2 == "green" else "v"
        price_tbl.add_row(f"[{c1}]{arrow1} ${p1:,.2f}[/]",
                          f"[{c2}]{arrow2} ${p2:,.2f}[/]")
    m15c = "green" if m15 > 0 else ("red" if m15 < 0 else "dim")
    m30c = "green" if m30 > 0 else ("red" if m30 < 0 else "dim")
    m60c = "green" if m60 > 0 else ("red" if m60 < 0 else "dim")
    spike_str = "[bold yellow blink]SPIKE[/]" if spk_now else ""
    price_tbl.add_row(f"[bold]NOW ${cur:,.2f}[/]", spike_str)
    price_tbl.add_row(f"[dim]15s [{m15c}]{m15:+.4f}%[/]", f"[dim]30s [{m30c}]{m30:+.4f}%[/]")
    price_tbl.add_row(f"[dim]60s [{m60c}]{m60:+.4f}%[/]", f"[dim]RSI [{rsi_c}]{rsi_v:.1f}[/]")
    wd_disp = 0.0
    if active_markets:
        sp = active_markets[0].start_price
        wd_disp = (FEED.current - sp) / sp * 100 if sp > 0 else 0.0
    wdc = "green" if wd_disp > 0 else ("red" if wd_disp < 0 else "dim")
    wda = "^" if wd_disp > 0.01 else ("v" if wd_disp < -0.01 else "-")
    price_tbl.add_row(f"[dim]dwin [{wdc}]{wda}{wd_disp:+.3f}%[/{wdc}]",
                      f"[dim]best: [cyan]{best_score:.3f}[/][/]")
    price_tbl.add_row(f"[dim]mkts: {len(active_markets)}[/]",
                      f"[dim]edge~: [yellow]{avg_edge:.3f}[/][/]")
    layout["feed"].update(Panel(price_tbl, title="[bold #f7931a]BTC/USDT[/]",
                                border_style="#f7931a", style="on #080a0c"))

    # Decision log
    log_t = Table(box=box.SIMPLE, show_header=True, header_style="dim",
                  style="on #080a0c", expand=True)
    log_t.add_column("", width=2)
    log_t.add_column("Dir", width=5)
    log_t.add_column("Score", width=6, justify="right")
    log_t.add_column("Size", width=7, justify="right")
    log_t.add_column("P&L", width=9, justify="right")
    log_t.add_column("15s", width=8, justify="right")
    log_t.add_column("60s", width=8, justify="right")
    log_t.add_column("Reason", max_width=22)
    entries = list(scan_state.get("log", []))[:20]
    if not entries:
        log_t.add_row("", "[dim]Waiting...[/]", "", "", "", "", "", "")
    for e in entries:
        typ = e.get("type", "skip")
        em15 = e.get("mom15", 0)
        em60 = e.get("mom60", 0)
        em15c = "green" if em15 > 0 else "red"
        em60c = "green" if em60 > 0 else "red"
        size_e = e.get("size", 0)
        entry_e = e.get("entry", 0)
        if typ == "snipe":
            icon = "+"
            dc = "green" if e.get("dir") == "UP" else "red"
            dir_s = f"[bold {dc}]{e.get('dir', '?')}[/]"
            sc_s = f"[cyan]{e.get('score', 0):.3f}[/]"
            size_s = f"[dim]{size_e:.1f}$[/]" if size_e else ""
            pnl_s = f"[dim]@ {entry_e:.3f}[/]" if entry_e else ""
        elif typ == "exit":
            icon = "x"
            pnl = e.get("pnl", 0)
            dc = "green" if pnl >= 0 else "red"
            dir_s = f"[{dc}]{e.get('dir', '?')}[/]"
            sc_s = f"[bold {dc}]{pnl:+.2f}$[/]"
            size_s = f"[dim]{size_e:.1f}$[/]" if size_e else ""
            pnl_s = f"[bold {dc}]{e.get('pnl_pct', 0):+.1f}%[/]"
            em15 = em60 = 0
        else:
            icon = "-"
            dir_s = f"[dim]{e.get('dir', '-')}[/]"
            sc_s = f"[dim]{e.get('score', 0):.3f}[/]" if e.get("score") else ""
            size_s = ""
            pnl_s = ""
        log_t.add_row(icon, dir_s, sc_s, size_s, pnl_s,
                      f"[{em15c}]{em15:+.4f}%[/]" if em15 else "",
                      f"[{em60c}]{em60:+.4f}%[/]" if em60 else "",
                      f"[dim]{e.get('reason', '')[:22]}[/]")
    layout["center"].update(Panel(log_t, title="[dim]LOG[/]",
                                  border_style="#1a2030", style="on #080a0c"))

    # Positions
    pos_t = Table(box=box.SIMPLE, show_header=True, header_style="dim",
                  style="on #080a0c", expand=True)
    pos_t.add_column("Dir", width=5)
    pos_t.add_column("Entry", width=6, justify="right")
    pos_t.add_column("Now", width=6, justify="right")
    pos_t.add_column("P&L", width=8, justify="right")
    pos_t.add_column("T", width=11, justify="right")
    if not positions:
        pos_t.add_row("[dim]None[/]", "", "", "", "")
    for pos in positions:
        fee_live = fee_usdc(pos.size_usdc, pos.fee_rate_used) + \
                   fee_usdc(pos.current_price * pos.shares_held, pos.fee_rate_used)
        net_live = pos.pnl_gross - fee_live
        pct = net_live / max(pos.size_usdc, 0.001) * 100
        pc2 = "green" if pct >= 0 else "red"
        dc = "green" if pos.direction == "UP" else "red"
        rem = pos.market.remaining_sec
        rem_str = f"[bold yellow]{rem:.0f}s[/]" if rem < 30 else f"[yellow]{rem:.0f}s[/]"
        hold_str = "[bold cyan]HOLD[/]" if pos.holding_expiry else f"[dim]sl:{pos.trail_sl:.3f}[/]"
        pos_t.add_row(f"[bold {dc}]{pos.direction}[/]",
                      f"{pos.entry_price:.3f}", f"{pos.current_price:.3f}",
                      f"[{pc2}]{pct:+.1f}%[/]",
                      f"{rem_str} {hold_str}")
    layout["pos"].update(Panel(pos_t, title="[dim]POSITIONS[/]",
                               border_style="#1a2030", style="on #080a0c"))

    # Session stats
    pc2 = "green" if stats.total_pnl >= 0 else "red"
    st = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    st.add_column(style="dim")
    st.add_column(justify="right")
    st.add_row("Bankroll", f"[bold green]{bankroll:.2f} USDC.e[/]")
    st.add_row("P&L", f"[bold {pc2}]{stats.total_pnl:+.2f} USDC[/]")
    st.add_row("Win rate", f"[cyan]{stats.win_rate:.1f}%[/] ({stats.wins}W/{stats.losses}L)")
    st.add_row("Positions", f"[yellow]{len(positions)}[/]/{MAX_OPEN_POS}")
    st.add_row("Snipes", f"[green]{stats.snipes}[/]")
    st.add_row("HoldExp", f"[cyan]{stats.held_expiry}[/]")
    st.add_row("Spikes", f"[bold yellow]{stats.spikes_seen}[/]")
    layout["sess"].update(Panel(st, title="[dim]SESSION[/]",
                                border_style="#1a2030", style="on #080a0c"))

    # Performance
    pc3 = "green" if stats.btc_pnl >= 0 else "red"
    pf = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    pf.add_column()
    pf.add_column(justify="right")
    pf.add_column(justify="right")
    pf.add_row("[#f7931a]BTC[/]",
               f"[{pc3}]{stats.btc_pnl:+.2f}$[/]",
               f"[dim]{stats.btc_w}W/{stats.btc_l}L[/]")
    layout["perf"].update(Panel(pf, title="[dim]PERF[/]",
                                border_style="#1a2030", style="on #080a0c"))

    # Spike monitor
    sp_t = Table(box=box.SIMPLE, show_header=True, header_style="dim",
                 style="on #080a0c", expand=True)
    sp_t.add_column("!", width=2)
    sp_t.add_column("Dir", width=5)
    sp_t.add_column("Move 15s", width=10, justify="right")
    sp_t.add_column("Move 60s", width=10, justify="right")
    sp_t.add_column("Score~", width=8, justify="right")
    sp_t.add_column("Action", width=16)
    spikes = list(scan_state.get("spikes", []))[:6]
    if not spikes:
        sp_t.add_row("", "[dim]Waiting for volatility...[/]", "", "", "", "")
    for s in spikes:
        dc = "green" if s.get("dir") == "UP" else "red"
        sv = s.get("score", 0)
        strong = s.get("strong", False)
        sc_c = "cyan" if sv >= SETTINGS.min_score else ("yellow" if sv > 0.20 else "dim")
        sp_t.add_row(
            "!" if strong else ".",
            f"[bold {dc}]{s.get('dir', '?')}[/]",
            f"[bold {dc}]{s.get('mom15', 0):+.4f}%[/]" if strong else f"[{dc}]{s.get('mom15', 0):+.4f}%[/]",
            f"[{dc}]{s.get('mom60', 0):+.4f}%[/]",
            f"[{sc_c}]{sv:.3f}~[/]",
            f"[dim]{s.get('action', '')[:16]}[/]",
        )
    from pulse.feed import SPIKE_INTERRUPT
    int_str = "[bold yellow]INTERRUPT[/]" if SPIKE_INTERRUPT.is_set() else "[dim]-[/]"
    layout["spikes"].update(Panel(sp_t,
        title=f"[bold yellow]SPIKE MONITOR[/] {int_str} [dim](score~=indicative)[/]",
        border_style="#2a1a00", style="on #0a0800"))

    # Controls
    sc_col = "green" if SETTINGS.min_score >= 0.60 else ("yellow" if SETTINGS.min_score >= 0.40 else "red")
    bar = "#" * int(SETTINGS.min_score * 20) + "." * (20 - int(SETTINGS.min_score * 20))
    auto_txt = "[bold yellow]AUTO[/]" if SETTINGS.auto_score else "[dim]AUTO[/]"
    layout["controls"].update(Panel(
        Text.from_markup(
            f" [{sc_col}]{bar}[/{sc_col}] {SETTINGS.display} "
            f"| [bold]-[/] [bold]+[/] score [bold]r[/] reset [bold]a[/] {auto_txt} [bold]q[/] quit"
            f" | [dim]best:[/] [cyan]{best_score:.3f}[/]"
        ),
        title="[dim]CONTROLS[/]", border_style="#2a3040", style="on #0d0f14"))

    layout["footer"].update(Panel(
        Text.assemble(
            f" TP:+{TP_DELTA:.0%} SL:-{SL_DELTA:.0%} Trail:{TRAILING_DISTANCE}"
            f" | Hold>{HOLD_THRESHOLD:.2f}@{HOLD_MIN_REMAINING:.0f}s"
            f" | Entry:{MIN_ENTRY_PRICE:.2f}-{MAX_ENTRY_PRICE:.2f}"
            f" | Spike:>{SPIKE_THRESHOLD:.2%}"
            f" | Window:{ENTRY_WINDOW_MIN}-{ENTRY_WINDOW_MAX}min"
            f" | Scan:{si}s CB:-{max_dl:.0f}$ | v5.0-BTC",
        ),
        style="on #080a0c", border_style="#1a2030",
    ))
    return layout
