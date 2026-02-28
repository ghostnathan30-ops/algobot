"""
AlgoBot — Backtesting Package
==============================
Phase 3: Backtesting Engine
Phase 4: Validation Suite

Modules:
  data_loader        — Loads and prepares market data with all signals
  trade              — Trade dataclass (one complete trade lifecycle)
  engine             — Event-driven bar-by-bar backtest engine
  metrics            — Performance statistics (Sharpe, PF, drawdown, etc.)
  walk_forward       — 7-window walk-forward validation
  monte_carlo        — 10,000-simulation Monte Carlo stress test
  stress_tester      — Double costs, remove best trades, risk scaling tests
  regime_tester      — Crisis scenario tests (2008, 2020, 2022, etc.)
  validation_runner  — Master orchestrator for all six validation stages
"""
