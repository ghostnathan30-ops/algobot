"""
AlgoBot — Performance Metrics Calculator
==========================================
Module:  src/backtest/metrics.py
Phase:   3 — Backtesting Engine
Purpose: Calculates all performance statistics from a list of Trade objects
         and an equity curve Series.

All metrics conform to industry-standard definitions used by:
  - CTA performance reporting (NFA/CFTC standards)
  - pyfolio-reloaded (Quantopian/Robinhood lineage)
  - empyrical (quantitative finance standard library)

Key metrics and their meaning for AlgoBot:
  profit_factor      — Total wins / total losses. Target: ≥ 2.5 (backtest), ≥ 2.0 (live)
  sharpe_ratio       — Risk-adjusted return (annualised). Target: ≥ 1.0
  sortino_ratio      — Like Sharpe but only penalises downside vol. Target: ≥ 1.5
  calmar_ratio       — Annualised return / max drawdown. Target: ≥ 0.5
  max_drawdown_pct   — Peak-to-trough equity decline. Target: ≤ 22% (in-sample)
  win_rate_pct       — % of trades with positive P&L. Target: ≈ 45-58%
  avg_win_loss_ratio — Average win / average loss in $. Target: ≥ 2.5
  expectancy_per_trade — Average net P&L per trade ($). Must be positive.
  trades_per_month   — Frequency check vs backtest expectations
  annual_returns     — Per-year return breakdown (% profitable years)
"""

from __future__ import annotations

import datetime
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)

# Risk-free rate assumption (US 10-yr avg, conservative)
DEFAULT_RISK_FREE_ANNUAL = 0.04   # 4% per year
TRADING_DAYS_PER_YEAR    = 252


# ── Individual metric functions ───────────────────────────────────────────────

def profit_factor(trades: list) -> float:
    """
    Profit Factor = Total Gross Profit / Total Gross Loss.

    The single most important measure of strategy quality.
    Only closed, realised trades are counted.
    Returns float('inf') if there are no losing trades.

    Args:
        trades: list of Trade objects with pnl_net populated.

    Returns:
        Profit factor as a float.
    """
    gross_profit = sum(t.pnl_net for t in trades if t.pnl_net > 0)
    gross_loss   = abs(sum(t.pnl_net for t in trades if t.pnl_net < 0))

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return round(gross_profit / gross_loss, 4)


def sharpe_ratio(
    daily_returns: pd.Series,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> float:
    """
    Annualised Sharpe Ratio = (Mean Excess Return / Std Dev) × sqrt(252).

    Uses daily returns (P&L / prior day equity).
    Risk-free rate default: 4% annual = 0.0159% daily.

    Args:
        daily_returns:    pd.Series of daily percentage returns (not dollar)
        risk_free_annual: Annual risk-free rate (decimal, e.g., 0.04)

    Returns:
        Annualised Sharpe ratio. Negative = strategy loses money.
    """
    if len(daily_returns) < 2:
        return 0.0

    rf_daily = risk_free_annual / TRADING_DAYS_PER_YEAR
    excess   = daily_returns - rf_daily
    std_dev  = excess.std(ddof=1)

    if std_dev == 0 or np.isnan(std_dev):
        return 0.0

    return round(excess.mean() / std_dev * np.sqrt(TRADING_DAYS_PER_YEAR), 4)


def sortino_ratio(
    daily_returns: pd.Series,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> float:
    """
    Annualised Sortino Ratio = (Mean Excess Return / Downside Dev) × sqrt(252).

    Like Sharpe but only penalises downside volatility.
    A more appropriate measure for trend-following (which has fat-tailed wins).

    Args:
        daily_returns:    pd.Series of daily percentage returns
        risk_free_annual: Annual risk-free rate (decimal)

    Returns:
        Annualised Sortino ratio.
    """
    if len(daily_returns) < 2:
        return 0.0

    rf_daily      = risk_free_annual / TRADING_DAYS_PER_YEAR
    excess        = daily_returns - rf_daily
    downside      = excess[excess < 0]
    downside_std  = np.sqrt((downside**2).mean()) if len(downside) > 0 else 0.0

    if downside_std == 0 or np.isnan(downside_std):
        return 0.0 if excess.mean() <= 0 else float("inf")

    return round(excess.mean() / downside_std * np.sqrt(TRADING_DAYS_PER_YEAR), 4)


def max_drawdown(equity_curve: pd.Series) -> tuple:
    """
    Maximum peak-to-trough drawdown.

    Args:
        equity_curve: pd.Series of equity values (DatetimeIndex)

    Returns:
        (max_dd_pct: float, max_dd_duration_days: int)
          max_dd_pct: Maximum drawdown as negative percentage (e.g., -18.5)
          max_dd_duration_days: Longest consecutive days spent in drawdown
    """
    if len(equity_curve) < 2:
        return 0.0, 0

    peak        = equity_curve.expanding().max()
    drawdown    = (equity_curve - peak) / peak * 100.0
    max_dd_pct  = float(drawdown.min())   # Most negative value

    # Duration: longest consecutive run of bars below peak
    in_dd      = (drawdown < 0)
    dd_duration = 0
    current    = 0
    for val in in_dd:
        if val:
            current += 1
            dd_duration = max(dd_duration, current)
        else:
            current = 0

    return round(max_dd_pct, 2), dd_duration


def calmar_ratio(
    annualized_return_pct: float,
    max_dd_pct: float,
) -> float:
    """
    Calmar Ratio = Annualised Return / |Max Drawdown|.

    Measures how much return you get per unit of worst-case loss.
    Target: ≥ 0.5 (earn at least 50 cents per dollar of max drawdown).

    Args:
        annualized_return_pct: Annualised return as percentage (e.g., 25.0)
        max_dd_pct:            Max drawdown as negative percentage (e.g., -18.5)

    Returns:
        Calmar ratio as float.
    """
    abs_dd = abs(max_dd_pct)
    if abs_dd == 0:
        return 0.0
    return round(annualized_return_pct / abs_dd, 4)


def win_rate(trades: list) -> float:
    """
    Win rate = number of winning trades / total trades (%).

    Args:
        trades: list of Trade objects

    Returns:
        Win rate as percentage (e.g., 45.5 means 45.5%)
    """
    if not trades:
        return 0.0
    winners = sum(1 for t in trades if t.pnl_net > 0)
    return round(winners / len(trades) * 100.0, 2)


def avg_win_loss_ratio(trades: list) -> float:
    """
    Average Win / Average Loss ratio (absolute dollar values).

    Also known as "Reward-to-Risk ratio".
    A value of 3.0 means your average winner is 3× your average loser.

    Args:
        trades: list of Trade objects

    Returns:
        Ratio as float. 0.0 if no winners or no losers.
    """
    wins   = [t.pnl_net for t in trades if t.pnl_net > 0]
    losses = [abs(t.pnl_net) for t in trades if t.pnl_net < 0]

    if not wins or not losses:
        return 0.0

    return round(np.mean(wins) / np.mean(losses), 4)


def expectancy_per_trade(trades: list) -> float:
    """
    Average net P&L per trade in dollars.

    Positive = strategy has an edge.
    Negative = strategy loses money on average.

    Args:
        trades: list of Trade objects

    Returns:
        Average P&L per trade in dollars.
    """
    if not trades:
        return 0.0
    return round(sum(t.pnl_net for t in trades) / len(trades), 2)


def avg_r_multiple(trades: list) -> float:
    """
    Average R-multiple per trade.

    R = pnl_net / initial_risk_dollars.
    A value of 1.5 means you earn 1.5× your initial risk on average.
    Target: ≥ 1.0 (earn more than you risk on average).

    Args:
        trades: list of Trade objects with pnl_r populated

    Returns:
        Average R-multiple.
    """
    if not trades:
        return 0.0
    return round(np.mean([t.pnl_r for t in trades]), 4)


def trades_per_month(trades: list) -> float:
    """
    Average number of trades per calendar month.

    Used to compare live signal frequency against backtest expectations.
    Large deviation (>25%) indicates a regime shift.

    Args:
        trades: list of Trade objects with entry_date populated

    Returns:
        Trades per month as float.
    """
    if len(trades) < 2:
        return float(len(trades))

    dates = [t.entry_date for t in trades]
    min_d = min(dates)
    max_d = max(dates)
    months = (max_d.year - min_d.year) * 12 + (max_d.month - min_d.month) + 1
    return round(len(trades) / max(months, 1), 2)


def annualized_return(
    equity_curve: pd.Series,
    initial_capital: float,
) -> float:
    """
    Annualised compound return as percentage.

    Formula: ((final / initial) ^ (252 / n_days)) - 1

    Args:
        equity_curve:    pd.Series of equity values with DatetimeIndex
        initial_capital: Starting equity

    Returns:
        Annualised return as percentage (e.g., 22.5 means 22.5%/year).
    """
    if len(equity_curve) < 2 or initial_capital <= 0:
        return 0.0

    final  = float(equity_curve.iloc[-1])
    n_days = len(equity_curve)
    years  = n_days / TRADING_DAYS_PER_YEAR

    if years <= 0 or initial_capital <= 0:
        return 0.0

    ratio = final / initial_capital
    if ratio <= 0:
        return -100.0

    return round((ratio ** (1.0 / years) - 1.0) * 100.0, 2)


def annual_returns_by_year(equity_curve: pd.Series) -> dict:
    """
    Return percentage for each calendar year.

    Uses year-start and year-end equity values.

    Args:
        equity_curve: pd.Series of equity values with DatetimeIndex

    Returns:
        dict mapping year (int) -> annual return percentage (float)
    """
    if len(equity_curve) == 0:
        return {}

    result = {}
    years  = equity_curve.index.year.unique()

    for yr in sorted(years):
        yr_data = equity_curve[equity_curve.index.year == yr]
        if len(yr_data) < 2:
            continue
        start_eq = float(yr_data.iloc[0])
        end_eq   = float(yr_data.iloc[-1])
        if start_eq > 0:
            result[int(yr)] = round((end_eq / start_eq - 1.0) * 100.0, 2)

    return result


def profit_factor_by_market(trades: list) -> dict:
    """
    Profit factor broken down by market.

    Helps identify which markets contribute most to (or drag down) performance.

    Args:
        trades: list of Trade objects

    Returns:
        dict mapping market_code -> profit_factor
    """
    markets = set(t.market for t in trades)
    result  = {}

    for m in sorted(markets):
        m_trades = [t for t in trades if t.market == m]
        result[m] = profit_factor(m_trades)

    return result


def profit_factor_by_strategy(trades: list) -> dict:
    """Profit factor broken down by strategy (TREND vs VMR)."""
    strategies = set(t.strategy for t in trades)
    return {
        s: profit_factor([t for t in trades if t.strategy == s])
        for s in sorted(strategies)
    }


def exit_reason_breakdown(trades: list) -> dict:
    """Count of trades by exit reason."""
    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    return dict(sorted(reasons.items(), key=lambda x: x[1], reverse=True))


# ── Master metrics function ────────────────────────────────────────────────────

def calculate_all_metrics(
    trades: list,
    equity_curve: pd.Series,
    initial_capital: float,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> dict:
    """
    Calculate all performance metrics from trades and equity curve.

    This is the primary function called by BacktestEngine.run().
    Returns a comprehensive dictionary matching the config.yaml metrics list.

    Args:
        trades:          list of Trade objects (closed trades only)
        equity_curve:    pd.Series of equity values (DatetimeIndex)
        initial_capital: Starting equity
        risk_free_annual: Annual risk-free rate for Sharpe/Sortino

    Returns:
        dict with all metrics. Key names match config.yaml reporting.metrics list.
    """
    if not trades:
        log.warning("calculate_all_metrics: No trades to analyse")
        return _empty_metrics()

    # ── Daily returns (from equity curve) ─────────────────────────────────────
    if len(equity_curve) > 1:
        daily_returns = equity_curve.pct_change().dropna()
    else:
        daily_returns = pd.Series(dtype=float)

    # ── Core metrics ──────────────────────────────────────────────────────────
    pf  = profit_factor(trades)
    sr  = sharpe_ratio(daily_returns, risk_free_annual)
    so  = sortino_ratio(daily_returns, risk_free_annual)
    dd_pct, dd_duration = max_drawdown(equity_curve)
    ann_ret = annualized_return(equity_curve, initial_capital)
    calmar  = calmar_ratio(ann_ret, dd_pct)

    total_return = (
        (float(equity_curve.iloc[-1]) / initial_capital - 1.0) * 100.0
        if len(equity_curve) > 0 else 0.0
    )

    wr    = win_rate(trades)
    wl    = avg_win_loss_ratio(trades)
    exp   = expectancy_per_trade(trades)
    avg_r = avg_r_multiple(trades)
    tpm   = trades_per_month(trades)

    annual_rets   = annual_returns_by_year(equity_curve)
    pf_by_market  = profit_factor_by_market(trades)
    pf_by_strat   = profit_factor_by_strategy(trades)
    exit_reasons  = exit_reason_breakdown(trades)

    # ── Trend vs VMR trade counts ──────────────────────────────────────────────
    trend_trades = [t for t in trades if t.is_trend]
    vmr_trades   = [t for t in trades if t.is_vmr]

    # ── Year profitability count ───────────────────────────────────────────────
    profitable_years = sum(1 for v in annual_rets.values() if v > 0)
    total_years      = len(annual_rets)

    metrics = {
        # Core performance
        "total_return_pct":         round(total_return, 2),
        "annualized_return_pct":    ann_ret,
        "profit_factor":            pf,
        "sharpe_ratio":             sr,
        "sortino_ratio":            so,
        "calmar_ratio":             calmar,

        # Drawdown
        "max_drawdown_pct":             dd_pct,
        "max_drawdown_duration_days":   dd_duration,

        # Win/loss statistics
        "win_rate_pct":             wr,
        "avg_win_loss_ratio":       wl,
        "avg_r_multiple":           avg_r,
        "expectancy_per_trade_usd": exp,

        # Trade counts
        "total_trades":             len(trades),
        "trend_trades":             len(trend_trades),
        "vmr_trades":               len(vmr_trades),
        "trades_per_month":         tpm,

        # Profitability over time
        "profitable_years":         profitable_years,
        "total_years":              total_years,
        "annual_returns_by_year":   annual_rets,

        # Breakdown by dimension
        "profit_factor_by_market":   pf_by_market,
        "profit_factor_by_strategy": pf_by_strat,
        "exit_reason_breakdown":     exit_reasons,

        # Capital
        "initial_capital":          initial_capital,
        "final_equity":             float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else initial_capital,
    }

    log.info(
        "Metrics: PF={pf:.2f} | Sharpe={sr:.2f} | Sortino={so:.2f} | "
        "MaxDD={dd:.1f}% | WinRate={wr:.0f}% | AnnReturn={ar:.1f}% | "
        "Trades={n} | E[trade]=${exp:.0f}",
        pf=pf, sr=sr, so=so, dd=dd_pct, wr=wr,
        ar=ann_ret, n=len(trades), exp=exp,
    )

    return metrics


# ── Validation check ──────────────────────────────────────────────────────────

def check_validation_thresholds(metrics: dict, thresholds: dict) -> dict:
    """
    Check whether a set of metrics passes the validation thresholds
    defined in config.yaml (validation.in_sample or validation.out_of_sample).

    Args:
        metrics:    dict from calculate_all_metrics()
        thresholds: dict from config["validation"]["in_sample"] or ["out_of_sample"]

    Returns:
        dict with keys:
          "passed"        — bool: True if all checks pass
          "checks"        — dict of check_name -> {"passed": bool, "value": float, "threshold": float}
          "fail_reasons"  — list of strings describing failures
    """
    checks      = {}
    fail_reasons = []

    def check(name, value, threshold, comparison):
        """comparison: 'ge' (>=) or 'le' (<=)."""
        if comparison == "ge":
            passed = value >= threshold
        else:
            passed = value <= threshold

        checks[name] = {"passed": passed, "value": value, "threshold": threshold}
        if not passed:
            op = ">=" if comparison == "ge" else "<="
            fail_reasons.append(f"{name}: {value:.3f} {op} {threshold} FAILED")

    # Profit Factor
    min_pf = thresholds.get("min_profit_factor", 2.0)
    check("profit_factor", metrics.get("profit_factor", 0), min_pf, "ge")

    # Sharpe Ratio
    min_sr = thresholds.get("min_sharpe_ratio", 0.8)
    check("sharpe_ratio", metrics.get("sharpe_ratio", 0), min_sr, "ge")

    # Max Drawdown (must be BELOW threshold — drawdown is a negative number)
    max_dd = thresholds.get("max_drawdown_pct", 28.0)
    actual_dd = abs(metrics.get("max_drawdown_pct", 0))
    check("max_drawdown_pct", actual_dd, max_dd, "le")

    # Profitable years (if specified)
    if "min_profitable_years" in thresholds:
        min_yr = thresholds["min_profitable_years"]
        check("profitable_years", metrics.get("profitable_years", 0), min_yr, "ge")

    return {
        "passed":       len(fail_reasons) == 0,
        "checks":       checks,
        "fail_reasons": fail_reasons,
    }


# ── Empty metrics (for edge cases) ───────────────────────────────────────────

def _empty_metrics() -> dict:
    return {
        "total_return_pct": 0.0, "annualized_return_pct": 0.0,
        "profit_factor": 0.0, "sharpe_ratio": 0.0, "sortino_ratio": 0.0,
        "calmar_ratio": 0.0, "max_drawdown_pct": 0.0,
        "max_drawdown_duration_days": 0, "win_rate_pct": 0.0,
        "avg_win_loss_ratio": 0.0, "avg_r_multiple": 0.0,
        "expectancy_per_trade_usd": 0.0, "total_trades": 0,
        "trend_trades": 0, "vmr_trades": 0, "trades_per_month": 0.0,
        "profitable_years": 0, "total_years": 0,
        "annual_returns_by_year": {}, "profit_factor_by_market": {},
        "profit_factor_by_strategy": {}, "exit_reason_breakdown": {},
        "initial_capital": 0.0, "final_equity": 0.0,
    }
