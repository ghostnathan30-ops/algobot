"""
AlgoBot — Backtesting Engine
==============================
Module:  src/backtest/engine.py
Phase:   3 — Backtesting Engine
Purpose: Event-driven bar-by-bar simulation of AlgoBot's strategy.
         Processes each trading day across all markets in strict chronological
         order, applying all risk management rules identically to how the live
         bot will operate.

Design:
  The engine is NOT vectorised. It steps through time one bar at a time.
  This is intentional: it allows us to correctly model:
    - Dynamic position sizing (risk_per_trade based on current equity)
    - Daily hard stop reset each morning
    - Portfolio-level risk checks before each new entry
    - Per-position trailing stops that update bar-by-bar
    - Intrabar stop execution (using bar's Low/High for stop checks)

Bar processing order (each date):
  1. Increment bars_held for all open positions
  2. Check intrabar stop hits for all open positions
     (Low < stop_price for longs, High > stop_price for shorts)
  3. Check trailing stop hits
  4. Check signal-based exits (DCS exit, TMA flip, VMR exit, VMR timeout)
  5. Check daily hard stop: if daily_loss >= $2,500, close all and halt
  6. Open new positions where signal fires and portfolio risk allows
  7. Update trailing stop levels for surviving positions
  8. Record daily P&L and equity

Cost model (ETF proxy backtest):
  - Entry slippage: entry_price × SLIPPAGE_PCT (added for longs, subtracted for shorts)
  - Exit slippage:  exit_price  × SLIPPAGE_PCT (subtracted for longs, added for shorts)
  - Commission:     COMMISSION_PER_RT flat per trade (both sides combined)
  - Default: 0.05% slippage per side, $10 commission per round-turn
  These represent realistic conservative ETF execution costs.
  Futures-specific costs (tick slippage, $5/side) apply in Phase 6 live mode.

Key configuration parameters (from config.yaml):
  position_sizing.risk_per_trade_pct      (1.0%)
  position_sizing.trailing_activation_r   (1.0 = trailing starts at +1R)
  position_sizing.trailing_stop_atr       (2.0 = trail 2.0 ATR below high)
  risk.daily_loss_hard_stop_usd           ($2,500)
  risk.daily_loss_alert_usd               ($1,500)
  risk.trailing_dd_pause_usd              ($3,000)
  risk.max_portfolio_risk_pct             (8.0%)
  risk.max_equity_risk_pct                (2.0% for ES+NQ combined)
  markets.<MARKET>.commission             ($5/side → $10 round-turn)
  markets.<MARKET>.slippage_ticks         (1 tick)
"""

from __future__ import annotations

import datetime
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger
from src.backtest.trade import OpenPosition, Trade, BacktestResult, close_position
from src.backtest.metrics import calculate_all_metrics

log = get_logger(__name__)

# ── Execution cost constants for ETF proxy backtesting ───────────────────────
SLIPPAGE_PCT_PER_SIDE = 0.0005   # 0.05% of price per entry/exit side
COMMISSION_PER_RT     = 10.0     # $10 round-trip per trade

# ETF point value (fractional sizing, position sizer uses $1/unit)
ETF_POINT_VALUE = 1.0

# Correlated market groups (for portfolio risk limit checks)
CORRELATED_PAIRS = [("ES", "NQ")]  # ES+NQ combined max equity risk 2%


# ── Engine ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Event-driven bar-by-bar backtesting engine.

    Usage:
        from src.backtest.data_loader import load_all_markets, load_config
        from src.backtest.engine import BacktestEngine

        config = load_config()
        data   = load_all_markets("2000-01-01", "2024-12-31", config)
        engine = BacktestEngine(config, initial_capital=150_000.0)
        result = engine.run(data, "2000-01-01", "2024-12-31")

        print(result)
        print(f"Profit Factor: {result.metrics['profit_factor']:.2f}")
    """

    def __init__(self, config: dict, initial_capital: float = 150_000.0):
        self.config          = config
        self.initial_capital = initial_capital
        self._reset()

    # ── Public API ────────────────────────────────────────────────────────────

    def run(
        self,
        market_data: dict,
        start_date: str,
        end_date: str,
    ) -> BacktestResult:
        """
        Run the full backtest.

        Args:
            market_data: dict of market_code -> DataFrame (from load_all_markets)
                         Each DataFrame must have combined_signal, combined_new_entry,
                         pos_size_trend, pos_size_vmr, stop_dist_trend, stop_dist_vmr,
                         regime, High, Low, Close, atr, and signal exit columns.
            start_date:  First date to begin trading (signals from before this are skipped)
            end_date:    Last date to trade

        Returns:
            BacktestResult with trades, equity_curve, daily_pnl, and metrics.
        """
        self._reset()

        start = pd.Timestamp(start_date)
        end   = pd.Timestamp(end_date)

        # Build sorted union of all trading dates in the window
        all_dates = sorted(
            set.intersection(*[set(df.index) for df in market_data.values()])
        )
        trade_dates = [d for d in all_dates if start <= d <= end]

        if not trade_dates:
            raise ValueError(f"No trading dates between {start_date} and {end_date}")

        log.info(
            "Backtest starting: {n} trading days, {start} to {end}, "
            "{m} markets, initial equity=${cap:,.0f}",
            n=len(trade_dates), start=start_date, end=end_date,
            m=len(market_data), cap=self.initial_capital,
        )

        # ── Main loop ──────────────────────────────────────────────────────────
        prev_date = None
        for ts in trade_dates:
            self._process_day(ts, market_data, prev_date)
            prev_date = ts

        # Force-close any positions still open at end of data
        last_ts = trade_dates[-1]
        for market in list(self.open_positions.keys()):
            pos = self.open_positions[market]
            row = market_data[market].loc[last_ts]
            self._close_position(
                market, last_ts, len(market_data[market].loc[:last_ts]) - 1,
                row["Close"], "end_of_data",
            )

        # ── Build equity series and metrics ───────────────────────────────────
        equity_series = pd.Series(self.equity_curve).sort_index()
        daily_pnl_series = pd.Series(self.daily_pnl_history).sort_index()

        metrics = calculate_all_metrics(
            trades=self.closed_trades,
            equity_curve=equity_series,
            initial_capital=self.initial_capital,
        )

        log.info(
            "Backtest complete: {n} trades | PF={pf:.2f} | Sharpe={sr:.2f} | "
            "MaxDD={dd:.1f}% | WinRate={wr:.0f}%",
            n=len(self.closed_trades),
            pf=metrics.get("profit_factor", 0),
            sr=metrics.get("sharpe_ratio", 0),
            dd=metrics.get("max_drawdown_pct", 0),
            wr=metrics.get("win_rate_pct", 0),
        )

        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            markets=list(market_data.keys()),
            initial_capital=self.initial_capital,
            trades=self.closed_trades,
            equity_curve=equity_series,
            daily_pnl=daily_pnl_series,
            metrics=metrics,
        )

    # ── Private: state reset ──────────────────────────────────────────────────

    def _reset(self):
        """Reset engine state for a fresh backtest."""
        self.equity           = self.initial_capital
        self.open_positions   = {}     # market -> OpenPosition
        self.closed_trades    = []     # list[Trade]
        self.equity_curve     = {}     # Timestamp -> float
        self.daily_pnl_history = {}    # Timestamp -> float
        self._trade_counter   = 0
        self._current_day_pnl = 0.0
        self._hard_stop_active = False  # Daily hard stop hit today
        self._peak_equity     = self.initial_capital  # For trailing drawdown

    # ── Private: single day processing ───────────────────────────────────────

    def _process_day(
        self,
        ts: pd.Timestamp,
        market_data: dict,
        prev_date: Optional[pd.Timestamp],
    ):
        """Process one trading day across all markets."""
        date = ts.date()

        # ── Reset daily state at start of new day ──────────────────────────────
        if prev_date is None or ts.date() != prev_date.date():
            self._current_day_pnl = 0.0
            self._hard_stop_active = False

        # ── Increment bars held for all open positions ─────────────────────────
        for pos in self.open_positions.values():
            pos.bars_held += 1

        # ── Process exits for each market ─────────────────────────────────────
        for market in list(self.open_positions.keys()):
            if market not in market_data:
                continue
            df = market_data[market]
            if ts not in df.index:
                continue
            row = df.loc[ts]
            bar_idx = df.index.get_loc(ts)
            self._process_exits(market, ts, bar_idx, row)

        # ── Daily hard stop check ──────────────────────────────────────────────
        daily_limit = float(
            self.config.get("risk", {}).get("daily_loss_hard_stop_usd", 2500.0)
        )
        daily_alert = float(
            self.config.get("risk", {}).get("daily_loss_alert_usd", 1500.0)
        )

        if self._current_day_pnl <= -daily_alert and self._current_day_pnl > -daily_limit:
            log.warning(
                "{date}: Daily loss alert: ${loss:.0f} (alert threshold: ${thr:.0f})",
                date=date, loss=-self._current_day_pnl, thr=daily_alert,
            )

        if self._current_day_pnl <= -daily_limit:
            log.warning(
                "{date}: DAILY HARD STOP HIT. Loss=${loss:.0f}. "
                "Closing all positions. No new entries today.",
                date=date, loss=-self._current_day_pnl,
            )
            self._hard_stop_active = True
            # Close any remaining open positions
            for market in list(self.open_positions.keys()):
                if market in market_data and ts in market_data[market].index:
                    row = market_data[market].loc[ts]
                    bar_idx = market_data[market].index.get_loc(ts)
                    self._close_position(market, ts, bar_idx, row["Close"], "daily_hard_stop")

        # ── New entries (only if daily hard stop not active) ──────────────────
        if not self._hard_stop_active:
            for market in list(market_data.keys()):
                if market in self.open_positions:
                    continue  # Already have a position in this market
                df = market_data[market]
                if ts not in df.index:
                    continue
                row = df.loc[ts]
                bar_idx = df.index.get_loc(ts)
                self._process_entry(market, ts, bar_idx, row)

        # ── Update trailing stops for surviving long positions ─────────────────
        for market, pos in self.open_positions.items():
            if market not in market_data or ts not in market_data[market].index:
                continue
            row = market_data[market].loc[ts]
            self._update_trailing_stop(pos, row)

            # Update MAE/MFE
            close = float(row["Close"])
            if pos.direction == "LONG":
                excursion = close - pos.entry_price_adj
                adverse   = pos.entry_price_adj - float(row["Low"])
            else:
                excursion = pos.entry_price_adj - close
                adverse   = float(row["High"]) - pos.entry_price_adj

            pos.max_favorable_excursion = max(pos.max_favorable_excursion, max(0.0, excursion))
            pos.max_adverse_excursion   = max(pos.max_adverse_excursion, max(0.0, adverse))

        # ── Record equity ──────────────────────────────────────────────────────
        # Mark-to-market: add unrealised P&L to closed-trade equity
        unrealised = self._unrealised_pnl(ts, market_data)
        self.equity_curve[ts]      = self.equity + unrealised
        self.daily_pnl_history[ts] = self._current_day_pnl

        # Update peak equity for trailing drawdown tracking
        current_equity = self.equity_curve[ts]
        self._peak_equity = max(self._peak_equity, current_equity)

        # Trailing drawdown warning
        trailing_dd_limit = float(
            self.config.get("risk", {}).get("trailing_dd_pause_usd", 3000.0)
        )
        trailing_dd = self._peak_equity - current_equity
        if trailing_dd >= trailing_dd_limit:
            log.warning(
                "{date}: Trailing drawdown alert: ${dd:.0f} from peak ${pk:.0f}",
                date=date, dd=trailing_dd, pk=self._peak_equity,
            )

    # ── Private: exits ────────────────────────────────────────────────────────

    def _process_exits(
        self,
        market: str,
        ts: pd.Timestamp,
        bar_idx: int,
        row: pd.Series,
    ):
        """Check and execute exit conditions for an open position."""
        if market not in self.open_positions:
            return

        pos = self.open_positions[market]
        bar_low   = float(row.get("Low",   row["Close"]))
        bar_high  = float(row.get("High",  row["Close"]))
        bar_close = float(row["Close"])

        # ── 1. Intrabar stop loss hit ──────────────────────────────────────────
        stop_hit = False
        if pos.direction == "LONG" and bar_low <= pos.stop_price:
            stop_hit = True
            exit_price_raw = pos.stop_price
            exit_reason    = "stop_loss"
        elif pos.direction == "SHORT" and bar_high >= pos.stop_price:
            stop_hit = True
            exit_price_raw = pos.stop_price
            exit_reason    = "stop_loss"

        if stop_hit:
            # Apply slippage on exit (fills worse than the stop level)
            slippage_delta = exit_price_raw * SLIPPAGE_PCT_PER_SIDE
            if pos.direction == "LONG":
                exit_price_adj = exit_price_raw - slippage_delta   # worse fill
            else:
                exit_price_adj = exit_price_raw + slippage_delta
            self._close_position(
                market, ts, bar_idx, exit_price_adj, exit_reason,
                exit_price_raw=exit_price_raw,
            )
            return

        # ── 2. Profit target hit (Phase 6+ — fixed R exit for trend trades) ─────
        if pos.stop_dist > 0 and pos.profit_target_r > 0 and pos.strategy == "TREND":
            if pos.direction == "LONG":
                target_price = pos.entry_price_adj + pos.stop_dist * pos.profit_target_r
                if bar_high >= target_price:
                    slippage_delta = target_price * SLIPPAGE_PCT_PER_SIDE
                    self._close_position(
                        market, ts, bar_idx, target_price - slippage_delta,
                        "profit_target", exit_price_raw=target_price,
                    )
                    return
            else:
                target_price = pos.entry_price_adj - pos.stop_dist * pos.profit_target_r
                if bar_low <= target_price:
                    slippage_delta = target_price * SLIPPAGE_PCT_PER_SIDE
                    self._close_position(
                        market, ts, bar_idx, target_price + slippage_delta,
                        "profit_target", exit_price_raw=target_price,
                    )
                    return

        # ── 3. Trailing stop hit ───────────────────────────────────────────────
        if pos.trailing_active and pos.trailing_stop_price is not None:
            ts_hit = False
            if pos.direction == "LONG" and bar_low <= pos.trailing_stop_price:
                ts_hit = True
                trail_exit = pos.trailing_stop_price
            elif pos.direction == "SHORT" and bar_high >= pos.trailing_stop_price:
                ts_hit = True
                trail_exit = pos.trailing_stop_price

            if ts_hit:
                slippage_delta = trail_exit * SLIPPAGE_PCT_PER_SIDE
                if pos.direction == "LONG":
                    trail_exit_adj = trail_exit - slippage_delta
                else:
                    trail_exit_adj = trail_exit + slippage_delta
                self._close_position(
                    market, ts, bar_idx, trail_exit_adj, "trailing_stop",
                    exit_price_raw=trail_exit,
                )
                return

        # ── 3. Signal-based exits (end of bar, use close) ─────────────────────
        exit_reason = self._check_signal_exit(pos, row)
        if exit_reason:
            slippage_delta = bar_close * SLIPPAGE_PCT_PER_SIDE
            if pos.direction == "LONG":
                exit_price_adj = bar_close - slippage_delta
            else:
                exit_price_adj = bar_close + slippage_delta
            self._close_position(
                market, ts, bar_idx, exit_price_adj, exit_reason,
                exit_price_raw=bar_close,
            )

    def _check_signal_exit(self, pos: OpenPosition, row: pd.Series) -> Optional[str]:
        """
        Check strategy-specific exit signals.

        Returns exit reason string if should exit, None if should hold.
        """
        sig = pos.signal_source

        if sig in ("AGREE_LONG", "PB_LONG"):
            # Trend exit: DCS 20-bar channel exit OR TMA flip
            if row.get("dcs_exit_long", False):
                return "dcs_exit"
            if int(row.get("tma_signal", 0)) == -1:
                return "tma_flip"

        elif sig in ("AGREE_SHORT", "PB_SHORT"):
            if row.get("dcs_exit_short", False):
                return "dcs_exit"
            if int(row.get("tma_signal", 0)) == 1:
                return "tma_flip"

        elif sig in ("VMR_LONG", "VMR_SHORT"):
            # VMR exit: RSI recovery OR max hold timeout
            vmr_max_hold = int(
                self.config.get("strategy", {}).get("vmr", {}).get("max_hold_bars", 5)
            )
            if pos.bars_held >= vmr_max_hold:
                return "vmr_timeout"
            if sig == "VMR_LONG" and row.get("vmr_exit_long", False):
                return "vmr_exit"
            if sig == "VMR_SHORT" and row.get("vmr_exit_short", False):
                return "vmr_exit"

        return None

    # ── Private: entries ──────────────────────────────────────────────────────

    def _process_entry(
        self,
        market: str,
        ts: pd.Timestamp,
        bar_idx: int,
        row: pd.Series,
    ):
        """Check and execute entry for a market with no open position."""
        # Must have a new entry signal this bar
        if not bool(row.get("combined_new_entry", False)):
            return

        signal = str(row.get("combined_signal", "NO_TRADE"))
        if signal == "NO_TRADE":
            return

        size_mult = float(row.get("combined_size_mult", 0.0))
        if size_mult <= 0:
            return

        # ── Portfolio risk check ───────────────────────────────────────────────
        is_trend = bool(row.get("combined_is_trend", False))
        is_vmr   = bool(row.get("combined_is_vmr",   False))

        if is_trend:
            position_size = float(row.get("pos_size_trend", 0.0))
            stop_dist     = float(row.get("stop_dist_trend", 0.0))
            strategy      = "TREND"
        elif is_vmr:
            position_size = float(row.get("pos_size_vmr", 0.0))
            stop_dist     = float(row.get("stop_dist_vmr", 0.0))
            strategy      = "VMR"
        else:
            return

        if position_size <= 0 or stop_dist <= 0:
            return

        direction    = "LONG" if signal in ("AGREE_LONG", "VMR_LONG", "PB_LONG") else "SHORT"
        entry_price  = float(row["Close"])
        atr          = float(row.get("atr", 0.0))

        # Dollar risk for this trade
        risk_dollars = position_size * stop_dist * ETF_POINT_VALUE

        # ── Portfolio risk guard: max 8% total open risk ──────────────────────
        max_portfolio_risk_pct = float(
            self.config.get("risk", {}).get("max_portfolio_risk_pct", 8.0)
        ) / 100.0
        current_open_risk = self._total_open_risk()
        if current_open_risk + risk_dollars > self.equity * max_portfolio_risk_pct:
            log.debug(
                "{market}: Entry blocked by portfolio risk cap "
                "(open=${open:.0f} + new=${new:.0f} > {cap:.0%} × equity=${eq:.0f})",
                market=market,
                open=current_open_risk, new=risk_dollars,
                cap=max_portfolio_risk_pct, eq=self.equity,
            )
            return

        # ── Correlated pair check: ES+NQ max 2% combined ──────────────────────
        max_eq_risk_pct = float(
            self.config.get("risk", {}).get("max_equity_risk_pct", 2.0)
        ) / 100.0
        for pair in CORRELATED_PAIRS:
            if market in pair:
                partner = pair[0] if market == pair[1] else pair[1]
                partner_risk = (
                    self.open_positions[partner].initial_risk_dollars
                    if partner in self.open_positions else 0.0
                )
                if partner_risk + risk_dollars > self.equity * max_eq_risk_pct:
                    log.debug(
                        "{market}: Entry blocked by correlated pair cap ({pair})",
                        market=market, pair=pair,
                    )
                    return

        # ── Apply entry slippage ───────────────────────────────────────────────
        slippage_delta = entry_price * SLIPPAGE_PCT_PER_SIDE
        if direction == "LONG":
            entry_price_adj = entry_price + slippage_delta   # Pay more to buy
            stop_price = entry_price_adj - stop_dist
        else:
            entry_price_adj = entry_price - slippage_delta   # Sell for less
            stop_price = entry_price_adj + stop_dist

        # ── Calculate trailing stop activation level ───────────────────────────
        trail_act_r = float(
            self.config.get("position_sizing", {}).get("trailing_activation_r", 1.0)
        )
        if direction == "LONG":
            r_activation = entry_price_adj + stop_dist * trail_act_r
        else:
            r_activation = entry_price_adj - stop_dist * trail_act_r

        # ── Read profit target params from config ──────────────────────────────
        ps_cfg = self.config.get("position_sizing", {})
        profit_target_r  = float(ps_cfg.get("profit_target_r",   2.5))
        breakeven_move_r = float(ps_cfg.get("breakeven_move_r",  1.5))

        # ── Open the position ──────────────────────────────────────────────────
        self._trade_counter += 1
        pos = OpenPosition(
            trade_id=self._trade_counter,
            market=market,
            direction=direction,
            strategy=strategy,
            signal_source=signal,
            entry_date=ts.date(),
            entry_bar_idx=bar_idx,
            entry_price=entry_price,
            entry_price_adj=entry_price_adj,
            position_size=position_size,
            stop_price=stop_price,
            point_value=ETF_POINT_VALUE,
            initial_risk_dollars=risk_dollars,
            regime_at_entry=str(row.get("regime", "")),
            size_multiplier=size_mult,
            atr_at_entry=atr,
            stop_dist=stop_dist,
            profit_target_r=profit_target_r if strategy == "TREND" else 0.0,
            breakeven_move_r=breakeven_move_r if strategy == "TREND" else 0.0,
        )
        pos.r_activation_level = r_activation
        self.open_positions[market] = pos

        log.debug(
            "ENTRY {market} {dir} {strat} | {date} | "
            "price={p:.4f} stop={s:.4f} size={sz:.3f} risk=${r:.0f}",
            market=market, dir=direction, strat=strategy,
            date=ts.date(), p=entry_price_adj,
            s=stop_price, sz=position_size, r=risk_dollars,
        )

    # ── Private: close position ───────────────────────────────────────────────

    def _close_position(
        self,
        market: str,
        ts: pd.Timestamp,
        bar_idx: int,
        exit_price_adj: float,
        exit_reason: str,
        exit_price_raw: Optional[float] = None,
    ):
        """Close an open position and record the completed Trade."""
        if market not in self.open_positions:
            return

        pos = self.open_positions.pop(market)
        if exit_price_raw is None:
            exit_price_raw = exit_price_adj

        trade = close_position(
            pos=pos,
            exit_date=ts.date(),
            exit_bar_idx=bar_idx,
            exit_price_raw=exit_price_raw,
            exit_price_adj=exit_price_adj,
            exit_reason=exit_reason,
            commission_per_rt=COMMISSION_PER_RT,
        )

        self.closed_trades.append(trade)
        self.equity          += trade.pnl_net
        self._current_day_pnl += trade.pnl_net

        log.debug(
            "EXIT {market} {dir} | {date} @{p:.4f} ({reason}) | "
            "PnL=${pnl:+.2f} ({r:+.2f}R)",
            market=market, dir=pos.direction,
            date=ts.date(), p=exit_price_adj,
            reason=exit_reason, pnl=trade.pnl_net, r=trade.pnl_r,
        )

    # ── Private: trailing stop update ────────────────────────────────────────

    def _update_trailing_stop(self, pos: OpenPosition, row: pd.Series):
        """Update trailing stop price and check breakeven trigger."""
        trail_atr_mult = float(
            self.config.get("position_sizing", {}).get("trailing_stop_atr", 2.0)
        )
        atr = float(row.get("atr", pos.atr_at_entry))
        bar_close = float(row["Close"])

        # ── Breakeven trigger: move stop to entry when breakeven_move_r is hit ─
        if (
            not pos.breakeven_activated
            and pos.stop_dist > 0
            and pos.breakeven_move_r > 0
            and pos.strategy == "TREND"
        ):
            if pos.direction == "LONG":
                be_trigger = pos.entry_price_adj + pos.stop_dist * pos.breakeven_move_r
                if bar_close >= be_trigger:
                    pos.stop_price = max(pos.stop_price, pos.entry_price_adj)
                    pos.breakeven_activated = True
                    log.debug(
                        "{market}: Breakeven triggered at {r:.1f}R — stop moved to {s:.4f}",
                        market=pos.market, r=pos.breakeven_move_r, s=pos.stop_price,
                    )
            else:
                be_trigger = pos.entry_price_adj - pos.stop_dist * pos.breakeven_move_r
                if bar_close <= be_trigger:
                    pos.stop_price = min(pos.stop_price, pos.entry_price_adj)
                    pos.breakeven_activated = True
                    log.debug(
                        "{market}: Breakeven triggered at {r:.1f}R — stop moved to {s:.4f}",
                        market=pos.market, r=pos.breakeven_move_r, s=pos.stop_price,
                    )

        trail_distance = atr * trail_atr_mult

        if pos.direction == "LONG":
            # Check if trailing stop should activate
            r_level = getattr(pos, "r_activation_level", pos.entry_price_adj + pos.stop_price)
            if bar_close >= r_level:
                pos.trailing_active = True

            if pos.trailing_active:
                pos.highest_close = max(pos.highest_close, bar_close)
                new_trail = pos.highest_close - trail_distance
                # Trailing stop can only move UP (never lower it)
                if pos.trailing_stop_price is None:
                    pos.trailing_stop_price = new_trail
                else:
                    pos.trailing_stop_price = max(pos.trailing_stop_price, new_trail)
                # Never trail below initial stop
                pos.trailing_stop_price = max(pos.trailing_stop_price, pos.stop_price)

        elif pos.direction == "SHORT":
            r_level = getattr(pos, "r_activation_level", pos.entry_price_adj - pos.stop_price)
            if bar_close <= r_level:
                pos.trailing_active = True

            if pos.trailing_active:
                pos.lowest_close = min(pos.lowest_close, bar_close)
                new_trail = pos.lowest_close + trail_distance
                if pos.trailing_stop_price is None:
                    pos.trailing_stop_price = new_trail
                else:
                    pos.trailing_stop_price = min(pos.trailing_stop_price, new_trail)
                # Never trail above initial stop
                pos.trailing_stop_price = min(pos.trailing_stop_price, pos.stop_price)

    # ── Private: portfolio helpers ────────────────────────────────────────────

    def _total_open_risk(self) -> float:
        """Sum of initial_risk_dollars for all open positions."""
        return sum(p.initial_risk_dollars for p in self.open_positions.values())

    def _unrealised_pnl(self, ts: pd.Timestamp, market_data: dict) -> float:
        """Mark-to-market unrealised P&L across all open positions."""
        total = 0.0
        for market, pos in self.open_positions.items():
            if market in market_data and ts in market_data[market].index:
                close = float(market_data[market].loc[ts, "Close"])
                total += pos.unrealised_pnl(close)
        return total
