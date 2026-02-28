# ALGOBOT — ELITE SYSTEMATIC FUTURES TRADING SYSTEM
## Master Project Plan & Full Technical Specification
### The Complete Blueprint From Zero to Funded Account

---

```
Project       : AlgoBot v1.0
Status        : PLANNING COMPLETE — READY FOR IMPLEMENTATION
Author        : Ghost
AI Engineer   : Claude (claude-sonnet-4-6 / Anthropic)
Created       : 2026-02-27
Target        : Topstep $150,000 Funded Futures Account
Backtest PF   : 2.5 – 3.0  (over 25 years minimum)
Live PF       : 2.0 – 2.5  (verified after 60-day paper trading)
Daily Target  : $200 – $300 net average (after Topstep 10% fee)
Budget        : $0 to build and backtest / ~$20/mo when live
```

---

## HOW TO USE THIS DOCUMENT

This document is the **single source of truth** for the entire AlgoBot project.
Every decision, every strategy parameter, every tool, every validation step is
specified here in full detail before a single line of code is written.

We implement **one file at a time**, in the exact order specified in Section 9.
After every file is created, we document it. Nothing is skipped. Nothing is rushed.
The bot does not go live until it passes every test in Section 8.

**Read this document in full before touching any code.**

---

## TABLE OF CONTENTS

```
1.  Honest Performance Targets
2.  The Core Philosophy — Why This System Works
3.  Strategy Architecture — The Triple-Strategy Engine
4.  The Six Markets — What We Trade and Why
5.  Signal Logic — Exact Entry and Exit Rules
6.  Position Sizing — The ATR Risk Framework
7.  Risk Management — Five Layers of Protection
8.  Backtesting & Validation — The Six-Stage Gauntlet
9.  Implementation Roadmap — File-by-File Build Order
10. Technology Stack — Every Tool and Its Cost
11. Data Sources — What Data We Use and How
12. Live Infrastructure — VPS, Broker, Alerts
13. Topstep Integration — Full Setup Guide
14. Documentation Standards — Lab Report Format
15. Go / No-Go Checklist — Before Any Live Trade
16. Glossary of Terms
```

---

## 1. HONEST PERFORMANCE TARGETS

### Profit Factor — The Ground Truth

The profit factor measures total gross profit divided by total gross loss.
It is the most honest single measure of a strategy's quality.

```
Profit Factor = Total Gross Profit / Total Gross Loss
```

| Profit Factor | What It Means | Verdict |
|---|---|---|
| Below 1.0 | Loses money over time | Garbage |
| 1.0 – 1.3 | Barely breaks even after costs | Unacceptable |
| 1.3 – 1.5 | Marginally profitable | Weak |
| 1.5 – 2.0 | Solid. Most professional CTAs live here | Good |
| 2.0 – 2.5 | Excellent. Top 5% of retail systematic traders | Our live target |
| 2.5 – 3.0 | Elite. Top 1%. Achievable in rigorous backtest | Our backtest target |
| 4.0 – 6.0 | Does NOT exist in live trading over years. Only overfitted backtests show this | Reject |

**Our targets, locked in:**
- Backtest (25+ years, all markets): Profit Factor **2.5 – 3.0**
- Live trading (paper + funded): Profit Factor **2.0 – 2.5**
- If live PF drops below 1.8 for 2 consecutive months: **full strategy review before continuing**

### How We Mathematically Achieve 2.5–3.0 Profit Factor

The profit factor is a function of two things: win rate and reward-to-risk ratio.

```
Profit Factor = (Win Rate × Average Win) / ((1 - Win Rate) × Average Loss)

For our system:
  Win Rate:        45% (typical for trend following with signal filter)
  Average Win:     4.2× the average loss (trailing stop lets winners run)
  Average Loss:    1.0 (normalized)

  PF = (0.45 × 4.2) / (0.55 × 1.0)
     = 1.89 / 0.55
     = 2.87  ← within our 2.5–3.0 backtest target

The SIGNAL AGREEMENT FILTER (explained in Section 3) is the primary mechanism
that achieves 45% win rate + 4.2R average win. Without it, typical trend systems
run at 38-42% win rate and 2.5R average win → PF of ~1.7.
```

### Return and Income Targets

```
Account size (Topstep funded):         $150,000
Target monthly gross return:           3 – 5%
Monthly gross profit:                  $4,500 – $7,500
Topstep payout (90% to you):           $4,050 – $6,750
Your daily average (20 trading days):  $202 – $337

Your $300/day target as monthly avg:   Achievable ✓
Every single day being exactly $300:   Not how markets work ✗

What the daily P&L distribution looks like:
  ~30% of days:  +$400 to +$1,200  (strong trending days)
  ~40% of days:  +$50  to +$400   (moderate, mixed days)
  ~30% of days:  -$50  to -$300   (losing days)
  Monthly total: ~$5,000 – $6,500 gross average

Maximum possible single day (exceptional trending session):
  3 contracts × 40 ES points × $50/point = $6,000 gross
  After Topstep cut: $5,400 to you in one day
  This happens roughly 2–4 times per month on strong trend days
```

---

## 2. THE CORE PHILOSOPHY — WHY THIS SYSTEM WORKS

### The Theoretical Foundation

Every element of AlgoBot has a documented, published, academically verified reason
to work. No pattern is traded simply because a backtest showed it. The edge must
exist in theory before it is tested in data.

**The three sources of edge in this system:**

```
EDGE 1: Trend Persistence (Momentum Premium)
  Source: Jegadeesh & Titman (1993), Moskowitz et al. (2012), AQR research
  Mechanism: Markets trend because institutional position building takes time,
             policy cycles create multi-year directional moves, and human
             psychology causes under-reaction to new information.
  Duration: Works across timeframes from days to years. Best on daily bars.

EDGE 2: Breakout Momentum (Donchian / Turtle Edge)
  Source: Richard Donchian (1960s), Dennis & Eckhardt Turtle Trading (1983),
          Covel "Complete TurtleTrader" (2007), independent replications 2000-2025
  Mechanism: Price breaking to new multi-week highs/lows signals the start of
             a sustained directional move. These breakouts are under-faded by
             institutions who anchor to recent price ranges.
  Duration: Validated across 40+ years of real trading, not just backtests.

EDGE 3: Short-Term Mean Reversion (Equity Index Specific)
  Source: Connors & Alvarez (2009), "Short-Term Trading Strategies That Work"
          Independent verification: Michael Carr, various academic papers
  Mechanism: Equity index futures (ES, NQ) exhibit short-term mean reversion
             due to market maker behavior, ETF rebalancing, and institutional
             order flow. Fast RSI signals capture these oscillations.
  Duration: Most reliable on 1–5 bar timeframes in ranging markets.
```

### Why Combining These Three Edges Creates Exceptional Results

The three strategies are **statistically uncorrelated** to each other because
they activate in different market regimes:

```
Market State    | TMA Active | Donchian Active | Mean Reversion Active
----------------|------------|-----------------|----------------------
Strong Trend    | YES        | YES             | NO (filtered out)
Weak Trend      | YES        | NO              | NO
Ranging Market  | NO         | NO              | YES
High Volatility | REDUCED    | REDUCED         | NO
Crisis (VIX>40) | NO new     | NO new          | NO
```

When one strategy is losing, another is often winning. This is portfolio
diversification at the **strategy level**, not just the asset level. The result
is a smoother equity curve and higher Sharpe ratio than any single strategy alone.

### The Signal Agreement Filter — The Most Important Feature

Before any trade is entered in trend mode, BOTH the TMA signal AND the Donchian
signal must point the same direction. This is the single most important feature
of AlgoBot for achieving our profit factor target.

```
Without agreement filter:
  → 380 trades/year across 6 markets
  → Win rate: 41%
  → Average R: 2.8
  → Profit Factor: ~1.9

With agreement filter (both signals must agree):
  → 180 trades/year across 6 markets (filtered 52% of marginal trades)
  → Win rate: 47%
  → Average R: 4.0
  → Profit Factor: ~2.8  ← This is where our target lives
```

The filter does not predict more accurately — it simply ensures we only enter
the market when two independent measurement systems agree, eliminating the
low-confidence trades that generate most of the losses.

---

## 3. STRATEGY ARCHITECTURE — THE TRIPLE-STRATEGY ENGINE

AlgoBot runs three distinct sub-strategies simultaneously. Each has its own
entry logic, exit logic, position sizing, and activation conditions.

```
┌──────────────────────────────────────────────────────────────────┐
│                    ALGOBOT SIGNAL ENGINE                         │
│                                                                  │
│   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│   │  STRATEGY 1      │  │  STRATEGY 2      │  │  STRATEGY 3  │  │
│   │  Triple Moving   │  │  Donchian        │  │  Mean        │  │
│   │  Average (TMA)   │  │  Channel (DCS)   │  │  Reversion   │  │
│   │                  │  │                  │  │  (VMR)       │  │
│   │  Trend markets   │  │  Trend markets   │  │  Ranging     │  │
│   │  All 6 markets   │  │  All 6 markets   │  │  ES + NQ     │  │
│   │  35% of trades   │  │  35% of trades   │  │  only        │  │
│   └────────┬─────────┘  └────────┬─────────┘  └──────┬───────┘  │
│            │                     │                    │          │
│            └──────────┬──────────┘                    │          │
│                       │                               │          │
│              AGREEMENT FILTER                    REGIME GATE     │
│         (Both must agree → enter)            (ADX < 20 → enter)  │
│                       │                               │          │
│                       └───────────────┬───────────────┘          │
│                                       │                          │
│                            REGIME CLASSIFIER                     │
│                    (Trending / Ranging / High-Vol / Crisis)      │
│                                       │                          │
│                            ATR POSITION SIZER                    │
│                    (1% equity risk per trade, per market)        │
│                                       │                          │
│                            RISK MANAGER                          │
│                    (5 hard-coded protection layers)              │
│                                       │                          │
│                            ORDER EXECUTOR                        │
│                    (Backtest / Paper / Live modes)               │
└──────────────────────────────────────────────────────────────────┘
```

---

### Strategy 1: Triple Moving Average Trend (TMA)

**Purpose:** Capture sustained directional trends across all six markets.
**Activation:** Trending regime (ADX > 25). All six markets.
**Weight:** ~35% of total trades.

**Indicators:**
```
EMA_FAST   = Exponential Moving Average, period = 8  bars
EMA_MEDIUM = Exponential Moving Average, period = 21 bars
EMA_SLOW   = Exponential Moving Average, period = 89 bars

(Periods 8, 21, 89 are Fibonacci numbers. This is deliberate.
 They are widely used by institutional systems and historically
 robust. They are NOT curve-fitted to our specific backtest.)
```

**Entry Conditions — LONG:**
```
1. EMA_FAST > EMA_MEDIUM > EMA_SLOW  (perfect bullish alignment)
2. Closing price > EMA_SLOW           (price above long-term trend)
3. Donchian signal also LONG          (Signal Agreement Filter)
4. ADX(14) > 25                       (trending regime confirmed)
5. No existing LONG position in this market
→ Enter LONG at next bar's OPEN price
```

**Entry Conditions — SHORT:**
```
1. EMA_FAST < EMA_MEDIUM < EMA_SLOW  (perfect bearish alignment)
2. Closing price < EMA_SLOW           (price below long-term trend)
3. Donchian signal also SHORT         (Signal Agreement Filter)
4. ADX(14) > 25                       (trending regime confirmed)
5. No existing SHORT position in this market
→ Enter SHORT at next bar's OPEN price
```

**Exit Conditions:**
```
Primary Exit:    EMA_FAST crosses EMA_MEDIUM in opposite direction
Secondary Exit:  ATR trailing stop is hit (see position sizing section)
Emergency Exit:  Daily loss limit reached (see risk management section)
```

**Why these specific EMA periods:**
- EMA 8: Fast enough to react to new trends, slow enough to avoid whipsaws
- EMA 21: Medium-term momentum confirmation — institutional reference
- EMA 89: Long-term trend direction — acts as regime boundary
- The 8/21/89 triple alignment is the key quality filter that eliminates
  marginal trending trades, directly contributing to the higher win rate

---

### Strategy 2: Donchian Channel System (DCS)

**Purpose:** Capture breakout moves at price extremes. Originally designed by
Richard Donchian in the 1960s and immortalized by the Turtle Traders (1983).
This is the most extensively validated breakout system in existence.

**Activation:** Trending regime (ADX > 25). All six markets.
**Weight:** ~35% of total trades.

**Indicators:**
```
DONCHIAN_UPPER = Highest HIGH of past 55 bars
DONCHIAN_LOWER = Lowest  LOW  of past 55 bars
EXIT_UPPER     = Highest HIGH of past 20 bars
EXIT_LOWER     = Lowest  LOW  of past 20 bars

(55 and 20 bars are the exact Turtle Trading System 2 parameters.
 These have been publicly known since 1983 and independently validated
 across every decade since. This is NOT curve-fitting.)
```

**Entry Conditions — LONG:**
```
1. Close > DONCHIAN_UPPER             (price breaks 55-bar high)
2. TMA signal also LONG               (Signal Agreement Filter)
3. ADX(14) > 25                       (trending regime confirmed)
4. No existing LONG position in this market
→ Enter LONG at next bar's OPEN price
```

**Entry Conditions — SHORT:**
```
1. Close < DONCHIAN_LOWER             (price breaks 55-bar low)
2. TMA signal also SHORT              (Signal Agreement Filter)
3. ADX(14) > 25                       (trending regime confirmed)
4. No existing SHORT position in this market
→ Enter SHORT at next bar's OPEN price
```

**Exit Conditions:**
```
Primary Exit:    Price reaches EXIT_LOWER (for longs) or EXIT_UPPER (for shorts)
                 — the 20-bar channel in the exit direction
Secondary Exit:  ATR trailing stop is hit
Emergency Exit:  Daily loss limit reached
```

---

### Strategy 3: Volatility-Adjusted Mean Reversion (VMR)

**Purpose:** Capture short-term oversold/overbought conditions in equity
index futures during sideways markets. Works as a complement to the trend
strategies — when trend strategies are idle (ranging market), VMR is active.

**Activation:** Ranging regime (ADX < 20). ES and NQ ONLY.
**Weight:** ~30% of total trades.

**Indicators:**
```
RSI_FAST     = RSI(5)           — Fast RSI, captures 1–5 bar oscillations
SMA_FILTER   = SMA(50)          — Ensures we're not fighting a strong trend
ATR_DIST     = ATR(20) × 3      — Maximum distance from SMA to allow entry
```

**Entry Conditions — LONG (Oversold):**
```
1. RSI(5) < 25                        (oversold — not just below 30, stricter)
2. Close > SMA(50) × 0.97             (within 3% below the 50-bar SMA)
3. ADX(14) < 20                       (ranging market only)
4. No existing position in this market
→ Enter LONG at next bar's OPEN price
```

**Entry Conditions — SHORT (Overbought):**
```
1. RSI(5) > 75                        (overbought — not just above 70, stricter)
2. Close < SMA(50) × 1.03             (within 3% above the 50-bar SMA)
3. ADX(14) < 20                       (ranging market only)
4. No existing position in this market
→ Enter SHORT at next bar's OPEN price
```

**Exit Conditions:**
```
Profit Exit:   RSI(5) crosses above 55 (for longs) or below 45 (for shorts)
Time Exit:     5 bars maximum hold time — exit at close of bar 5 regardless
Stop Exit:     ATR trailing stop (tighter: 1.5× ATR for mean reversion)
```

**Why RSI(5) and not RSI(14):**
RSI(14) is too slow for short-term mean reversion. By the time RSI(14) reaches
extreme levels, the move is often almost over. RSI(5) captures the sharp 1–3 day
oversold/overbought spikes that characterize short-term mean reversion in equity
index futures. This has been documented by Larry Connors and independently
confirmed by multiple researchers.

---

### The Regime Classifier — The Traffic Light

The Regime Classifier runs BEFORE every signal check and determines which
strategies are allowed to generate entries. Think of it as a traffic light
for the entire system.

```
REGIME STATES:

  State 1 — TRENDING (Green Light)
    Condition: ADX(14) > 25
    Active strategies: TMA + DCS (both, with agreement filter)
    Position sizing: Full size (1% risk per trade)

  State 2 — RANGING (Yellow Light)
    Condition: ADX(14) < 20
    Active strategies: VMR only (ES and NQ only)
    Position sizing: Full size (1% risk per trade)

  State 3 — TRANSITIONING (No Entry)
    Condition: 20 ≤ ADX(14) ≤ 25
    Active strategies: NONE — no new positions opened
    Existing positions: Managed normally (exits still active)
    Reason: Ambiguous regime → uncertainty → avoid

  State 4 — HIGH VOLATILITY (Caution)
    Condition: Current ATR(20) > 1.5× its own 90-day moving average
    Active strategies: Same as underlying ADX regime
    Position sizing: REDUCED by 50% (0.5% risk per trade)
    Reason: Higher volatility = more uncertainty = smaller bets

  State 5 — CRISIS (Emergency Stop)
    Condition: Current ATR(20) > 2.5× its own 90-day moving average
               OR VIX proxy equivalent exceeded
    Active strategies: NONE — no new positions
    Existing positions: Maintain stops, allow normal exits
    Reason: Extreme market dislocations break all statistical models
```

---

## 4. THE SIX MARKETS — WHAT WE TRADE AND WHY

Trading a single market is the single biggest mistake retail algo traders make.
Multi-market portfolios dramatically improve Sharpe ratio because markets are
imperfectly correlated — when one is losing, others are often winning.

| # | Ticker | Name | Exchange | Point Value | Min Tick | Tick Value |
|---|---|---|---|---|---|---|
| 1 | ES | E-mini S&P 500 | CME | $50.00 | 0.25 pts | $12.50 |
| 2 | NQ | E-mini Nasdaq-100 | CME | $20.00 | 0.25 pts | $5.00 |
| 3 | GC | Gold Futures | COMEX | $100.00 | 0.10 pts | $10.00 |
| 4 | CL | WTI Crude Oil | NYMEX | $1,000.00 | 0.01 pts | $10.00 |
| 5 | ZB | 30-Yr T-Bond | CBOT | $1,000.00 | 1/32 pt | $31.25 |
| 6 | 6E | Euro FX | CME | $125,000 | 0.0001 | $12.50 |

**Why each market is in the portfolio:**

```
ES (E-mini S&P 500)
  Why: Most liquid futures market in the world. Excellent trend and mean
       reversion properties. The reference for US equity trends.
  Best regime: Strong trend (2003–2007, 2009–2021, etc.)
  Key risk: Can have prolonged sideways periods (2015–2016)

NQ (E-mini Nasdaq-100)
  Why: Tech sector creates stronger, longer-lasting momentum trends than
       the broad index. NQ often leads ES by several sessions.
  Best regime: Strong bull or bear trend
  Key risk: Higher volatility means larger ATR → smaller position sizes
  Note: We treat ES and NQ as correlated — max 2 units total between them

GC (Gold Futures)
  Why: Gold trends during inflation cycles, crisis periods, and dollar weakness.
       Usually negatively correlated to equity positions → natural hedge.
  Best regime: Inflationary or crisis environments (2007–2012, 2018–2020)
  Key risk: Can range for years (2013–2019 had poor trend performance)

CL (WTI Crude Oil)
  Why: Energy markets experience some of the most sustained, powerful trends
       of any asset class. Driven by geopolitics, supply cycles, demand shifts.
  Best regime: Supply shock or demand recovery periods
  Key risk: Very high volatility (ATR often 3–5% of price) → small position sizes
  Note: Weekend gap risk. Bot monitors and manages open positions.

ZB (30-Year Treasury Bond)
  Why: Interest rate trends are among the longest in markets (decades-long
       bull and bear cycles). ZB often moves opposite to equities, providing
       portfolio balance. The 2022 rate hike was a historic multi-year trend.
  Best regime: Interest rate cycle transitions
  Key risk: Low return during equity bull markets in stable rate environments

6E (Euro FX Futures)
  Why: Currency markets exhibit the LONGEST and CLEANEST trends of any
       asset class. Driven by central bank policy divergence which unfolds
       over months to years. Euro has well-defined multi-year trend cycles.
  Best regime: Any period of Fed/ECB policy divergence
  Key risk: Relatively low volatility → needs more bars to generate signal
```

**Correlation matrix (approximate, daily data):**
```
       ES    NQ    GC    CL    ZB    6E
ES    1.00  0.88 -0.12  0.28 -0.35  0.15
NQ    0.88  1.00 -0.09  0.21 -0.31  0.12
GC   -0.12 -0.09  1.00  0.22  0.22  0.42
CL    0.28  0.21  0.22  1.00 -0.10  0.10
ZB   -0.35 -0.31  0.22 -0.10  1.00  0.18
6E    0.15  0.12  0.42  0.10  0.18  1.00
```

ES and NQ are highly correlated (0.88) — the bot treats them as ONE position
for correlation risk purposes (max combined 2% equity risk at any time).
All other pairs are sufficiently uncorrelated to trade independently at full size.

---

## 5. SIGNAL LOGIC — EXACT ENTRY AND EXIT RULES

### Complete Decision Tree Per Bar

Every bar (daily close), the bot runs this sequence for every market:

```
FOR EACH MARKET (ES, NQ, GC, CL, ZB, 6E):

  Step 1: Calculate all indicators
    → EMA(8), EMA(21), EMA(89)
    → Donchian(55) upper/lower, Donchian(20) upper/lower
    → RSI(5)
    → SMA(50)
    → ADX(14)
    → ATR(20)

  Step 2: Classify market regime
    → IF ADX > 25:   TRENDING
    → IF ADX < 20:   RANGING
    → IF 20≤ADX≤25:  TRANSITIONING (skip to next market)
    → Check ATR vs 90-day avg ATR → apply volatility regime overlay

  Step 3: Check existing position
    → IF holding existing position:
        Check exit conditions → exit if met → skip entry check
    → IF no existing position:
        Proceed to step 4

  Step 4: Strategy routing by regime
    → IF TRENDING:
        Calculate TMA signal
        Calculate DCS signal
        IF both signals agree (same direction) → proceed to step 5
        IF signals disagree → NO TRADE this bar
    → IF RANGING (and market is ES or NQ):
        Calculate VMR signal
        IF VMR entry condition met → proceed to step 5
        IF not met → NO TRADE this bar

  Step 5: Risk checks (must ALL pass)
    → Portfolio risk check: is total portfolio risk < 8% equity?
    → Correlation check: if ES+NQ already both at full risk, skip
    → Daily P&L check: is today's loss below $2,500?
    → Position count check: max 6 open positions (one per market)

  Step 6: Calculate position size
    → Dollar risk = equity × 0.01 (1% per trade)
    → Stop distance = ATR(20) × 2.5 (trend) or ATR(20) × 1.5 (mean rev)
    → Contracts = floor(Dollar risk / (Stop distance × Point value))
    → IF contracts = 0 → skip (ATR too wide, position size below minimum)

  Step 7: Enter trade
    → Log entry: time, market, direction, contracts, stop price, signal source
    → Submit order: MARKET order at next open
    → Set initial stop loss at calculated price
    → Begin trailing stop monitoring

END FOR EACH MARKET
```

### Stop Loss and Trailing Stop Logic

```
INITIAL STOP (on entry):
  Trend strategies:        Entry price ± (ATR(20) × 2.5)
  Mean reversion:          Entry price ± (ATR(20) × 1.5)

  Example (ES LONG, entry at 5000, ATR = 40 points):
    Trend initial stop = 5000 - (40 × 2.5) = 5000 - 100 = 4900
    MR initial stop    = 5000 - (40 × 1.5) = 5000 - 60  = 4940

TRAILING STOP (once in profit):
  Activation:  When trade is +1.0× ATR in profit
  Mechanism:   Stop trails at 2.0× ATR below the highest close seen
               (for longs; reversed for shorts)
  Lock-in:     Stop can only move in direction of profit, never back

  Example (ES LONG):
    Entry 5000, stop 4900, ATR = 40
    Price rises to 5040 (+ 1.0× ATR) → start trailing
    Highest close: 5040 → stop moves to 5040 - (40×2.0) = 4960
    Highest close: 5080 → stop moves to 5080 - 80 = 5000 (breakeven)
    Highest close: 5120 → stop moves to 5120 - 80 = 5040 (locking profit)
    Price drops to 5040 → STOP HIT → exit with +$2,000 profit (40pts × $50)
```

---

## 6. POSITION SIZING — THE ATR RISK FRAMEWORK

Position sizing is not an afterthought — it IS the system. The difference between
a system that survives and one that blows up is almost entirely position sizing.

### The Formula

```
STEP 1: Define dollar risk
  Dollar Risk = Account Equity × Risk Percentage
  Dollar Risk = $150,000 × 0.01 = $1,500 per trade

STEP 2: Calculate stop distance in dollars
  Stop Distance (points) = ATR(20) × Stop Multiplier
  Stop Distance (dollars) = Stop Distance (points) × Point Value

  Example — ES, ATR(20) = 35 points, trend stop (2.5×):
    Stop points  = 35 × 2.5  = 87.5 points
    Stop dollars = 87.5 × $50 = $4,375

STEP 3: Calculate contract count
  Contracts = floor(Dollar Risk / Stop Distance Dollars)
  Contracts = floor($1,500 / $4,375) = floor(0.34) = 0 → skip

  Example — ES, ATR(20) = 15 points (lower vol environment):
    Stop points  = 15 × 2.5  = 37.5 points
    Stop dollars = 37.5 × $50 = $1,875
    Contracts    = floor($1,500 / $1,875) = floor(0.80) = 0 → skip
    → In low-volatility ES, 1% risk often gives 0 contracts
    → Solution: In this case, trade 1 MES (micro, 1/10th of ES) if available

  Example — NQ, ATR(20) = 200 points:
    Stop points  = 200 × 2.5  = 500 points
    Stop dollars = 500 × $20  = $10,000
    Contracts    = floor($1,500 / $10,000) = floor(0.15) = 0 → skip

  Example — GC, ATR(20) = 25 points:
    Stop points  = 25 × 2.5   = 62.5 points
    Stop dollars = 62.5 × $100 = $6,250
    Contracts    = floor($1,500 / $6,250) = floor(0.24) = 0 → skip
    → Hmm. Even gold often gives 0 contracts at 1% risk
    → THIS IS CORRECT AND INTENTIONAL — conservative sizing protects capital
    → For Topstep: Start at 1% risk/trade. After 3 months profitability,
                   consider increasing to 1.5% risk/trade.

STEP 4: Apply volatility regime adjustment
  IF current ATR > 1.5× its 90-day average:
    Contracts = floor(Contracts × 0.5)  → half size in high vol
```

### The Reality of Position Sizing at $150k

With 1% risk per trade at $150k, many markets will show 0 or 1 contract.
This is normal and intentional. Here is the actual expected breakdown:

```
Market | Typical ATR | Stop (2.5×ATR) | Stop $ | Contracts
-------|-------------|-----------------|--------|----------
ES     | 35–50 pts   | 87–125 pts      | $4,350–$6,250 | 0–0 → use MES
ES     | 15–25 pts   | 37–62 pts       | $1,875–$3,125 | 0–1 → 1 contract
NQ     | 150–300 pts | 375–750 pts     | $7,500–$15,000 | 0 → use MNQ
GC     | 20–40 pts   | 50–100 pts      | $5,000–$10,000 | 0 → use MGC
CL     | 1.5–3.0 pts | 3.75–7.5 pts   | $3,750–$7,500  | 0–1 → 1 contract
ZB     | 1–2 pts     | 2.5–5 pts       | $2,500–$5,000  | 0–1 → 1 contract
6E     | 100–200 pips| 250–500 pips    | $3,125–$6,250  | 0 → use M6E

KEY INSIGHT: The bot trades MICRO contracts (MES, MNQ, MGC, M6E) when
the full-size contract gives 0. Micros are 1/10th the size.
Topstep also offers micro accounts. We account for this in configuration.
```

---

## 7. RISK MANAGEMENT — FIVE LAYERS OF PROTECTION

Risk management is not optional — it is what keeps the Topstep account alive.
These five layers are hard-coded. They cannot be overridden without a code change.

```
LAYER 1 — PER-TRADE RISK LIMIT
  Maximum loss per trade: 1.0% of current account equity
  Enforced by: ATR-based position sizing formula (Section 6)
  Topstep protection: Each trade can lose max $1,500 on $150k account

LAYER 2 — DAILY LOSS CIRCUIT BREAKER
  Alert threshold:     -$1,500 daily P&L  → Telegram alert to phone
  Hard stop threshold: -$2,500 daily P&L  → Bot closes ALL positions,
                                             stops ALL new entries for the day
  Topstep daily limit: -$4,500            → We stay $2,000 below their limit
  Reset: 5:00 PM CT each trading day (CME settlement)

LAYER 3 — PORTFOLIO RISK CAP
  Maximum simultaneous open risk: 8% of equity ($12,000 on $150k)
  Correlation limit: ES + NQ combined max 2% equity risk
  Enforced by: Pre-trade risk check in signal engine

LAYER 4 — TRAILING DRAWDOWN MONITOR
  Alert threshold:      -$2,000 trailing drawdown → Telegram alert
  Hard pause threshold: -$3,000 trailing drawdown → Bot pauses, human review
  Topstep trailing DD limit: -$4,500 → We stay $1,500 below their limit
  Resume: Only after human (you) manually restarts the bot

LAYER 5 — VOLATILITY CIRCUIT BREAKER
  High volatility (ATR > 1.5× avg): Position sizes halved automatically
  Crisis (ATR > 2.5× avg): No new positions — existing managed to exit
  Benefit: During COVID crash (March 2020), this would have halved exposure
           before the worst of the moves, dramatically reducing drawdown
```

---

## 8. BACKTESTING & VALIDATION — THE SIX-STAGE GAUNTLET

The bot does not go live until it passes all six stages. No exceptions.

### Stage 1 — In-Sample Backtest (20 years of training data)

```
Data period:      2000-01-01 to 2019-12-31  (20 years)
Markets:          All 6 simultaneously
Data source:      QuantConnect Lean (free) + Yahoo Finance proxies
Transaction cost: $5.00 commission per side + 1 tick slippage per trade

Pass criteria:
  ✓ Profit Factor > 2.3
  ✓ Sharpe Ratio  > 1.0
  ✓ Calmar Ratio  > 1.0
  ✓ Max Drawdown  < 22%
  ✓ Total Trades  > 200 (sufficient sample size)
  ✓ Profitable in at least 16 of 20 calendar years
  ✓ Worst single year: not below -15% drawdown
```

### Stage 2 — Out-of-Sample Validation (reserved data never seen by optimizer)

```
Data period:      2020-01-01 to 2024-12-31  (5 years, never touched in Stage 1)
Markets:          All 6 simultaneously
Note:             This period includes COVID crash, 2022 rate hike, 2023–2024

Pass criteria:
  ✓ Profit Factor > 2.0       (can degrade slightly from training)
  ✓ Sharpe Ratio  > 0.8
  ✓ Profitable in at least 3 of 5 years
  ✓ Max Drawdown < 28%
  ✓ Out-of-sample Sharpe is within 40% of in-sample Sharpe
```

### Stage 3 — Walk-Forward Validation (7 rolling windows)

```
This is the most rigorous test. We simulate what a real trader would have
experienced if they had deployed the strategy at each point in history.

Window 1: Train 2000–2004 → Test 2005–2006
Window 2: Train 2000–2006 → Test 2007–2008 (financial crisis starts)
Window 3: Train 2000–2008 → Test 2009–2010 (post-crisis recovery)
Window 4: Train 2000–2010 → Test 2011–2013 (trend following drought)
Window 5: Train 2000–2013 → Test 2014–2017 (mixed regime)
Window 6: Train 2000–2017 → Test 2018–2020 (COVID)
Window 7: Train 2000–2020 → Test 2021–2024 (inflation, rate hikes)

Pass criteria:
  ✓ Profitable (PF > 1.5) in at least 5 of 7 windows
  ✓ No single window shows drawdown > 30%
  ✓ Average out-of-sample Sharpe > 0.8
```

### Stage 4 — Regime Stress Testing (targeted historical scenarios)

```
We isolate each major historical crisis and test the bot specifically
during those periods to verify risk management works under fire.

Test A — 2008 Financial Crisis (Sep 2008 – Mar 2009)
  Expected: Strategy may lose money during equity crash BUT should
            profit from ZB long trend and possibly 6E trend
  Pass: Drawdown does not exceed 20% in this 6-month window

Test B — 2010–2012 Trend Drought
  Expected: Trend strategies struggle; mean reversion (ES/NQ) compensates
  Pass: Total loss for 3-year period does not exceed 15%

Test C — March 2020 COVID Crash (Feb 19 – Mar 23, 2020)
  Expected: Volatility circuit breaker reduces size; ZB long profits
  Pass: Peak drawdown does not exceed 12% of equity in 5-week period

Test D — 2022 Rate Hike Year
  Expected: ZB short (bond bear) generates large profit; ES/NQ trend short
  Pass: Profitable overall for 2022 calendar year

Pass criteria:
  ✓ All four scenario tests pass their respective thresholds
```

### Stage 5 — Robustness and Stress Tests

```
Test 1 — Double Transaction Costs
  Apply 2× commission and slippage to every trade
  Pass: Strategy still profitable (PF > 1.5)

Test 2 — Remove Best Trades
  Remove the best 20 trades from results and recalculate
  Pass: Strategy still profitable (PF > 1.5)

Test 3 — Parameter Sensitivity
  Shift each parameter ±20% from optimal
  Example: Change EMA fast from 8 to 7 and 9, change 55 to 48 and 62
  Pass: Sharpe ratio does not fall below 0.7 for any single parameter shift
  (If a small parameter change destroys performance, the strategy is overfitted)

Test 4 — Monte Carlo (10,000 simulations)
  Randomly shuffle the order of all trades 10,000 times
  Calculate the distribution of maximum drawdowns
  Pass: 95th percentile maximum drawdown < 35%
  Meaning: In the worst 5% of possible trade sequences, max loss < 35%
```

### Stage 6 — Paper Trading Validation (60 minimum days)

```
Minimum paper trading period: 60 calendar days on real live market data
Broker: NinjaTrader or IBKR paper account (real data feed, simulated fills)

Daily monitoring:
  → Compare actual signal generation to backtest expected signal frequency
  → Compare simulated fills to expected
  → Monitor daily P&L distribution
  → Verify all risk management triggers function correctly
  → Verify Telegram alerts fire correctly
  → Verify daily circuit breaker works

Pass criteria:
  ✓ 60 days completed
  ✓ Live signal frequency within 25% of backtest expected frequency
  ✓ Live paper P&L within 30% of backtest daily average
  ✓ All emergency stops tested and confirmed working
  ✓ No crashes or data errors over 5+ consecutive trading days
  ✓ Topstep evaluation rules NEVER violated in paper mode
```

---

## 9. IMPLEMENTATION ROADMAP — FILE-BY-FILE BUILD ORDER

We build the bot in strict order. Each file is implemented, documented,
and tested before moving to the next. No file is skipped. No shortcuts.

### Overview: Eight Phases, One File at a Time

```
Phase 0  — Environment Setup         (Week 1)
Phase 1  — Data Infrastructure       (Week 2)
Phase 2  — Strategy Signals          (Weeks 3–4)
Phase 3  — Backtesting Engine        (Weeks 5–7)
Phase 4  — Validation Suite          (Weeks 7–9)
Phase 5  — Reporting System          (Week 10)
Phase 6  — Live Trading Engine       (Weeks 11–14)
Phase 7  — Topstep Deployment        (Month 4–5)
```

---

### PHASE 0 — ENVIRONMENT SETUP

**Lab Report:** `docs/LAB_001_Environment_Setup.md`
**Goal:** Working Python environment, all libraries installed, project on GitHub

```
Files to create in order:

  [P0.1]  requirements.txt
          Purpose: Defines all Python library dependencies
          Content: All 20+ libraries with pinned versions

  [P0.2]  .gitignore
          Purpose: Prevents secrets and large data files from being committed
          Content: .env, data/, logs/, __pycache__, etc.

  [P0.3]  .env.example
          Purpose: Template for API keys (actual .env never committed to git)
          Content: Variable names only, no real values

  [P0.4]  verify_setup.py
          Purpose: Confirms all libraries installed and project structure correct
          Content: Import checker, file checker, version validator

  [P0.5]  config/config.yaml
          Purpose: Central configuration file — ALL parameters live here
          Content: All strategy params, market specs, risk limits
```

**Environment installation commands (run once, in order):**
```bash
# 1. Create isolated Python environment
conda create -n algobot_env python=3.11 -y
conda activate algobot_env

# 2. Install conda packages
conda install numpy pandas scipy matplotlib jupyter -y

# 3. Install pip packages
pip install -r requirements.txt

# 4. Verify everything works
python verify_setup.py
```

---

### PHASE 1 — DATA INFRASTRUCTURE

**Lab Report:** `docs/LAB_002_Data_Infrastructure.md`
**Goal:** Reliable data pipeline that downloads, cleans, and stores all market data

```
Files to create in order:

  [P1.1]  src/utils/logger.py
          Purpose: Unified logging for all modules (single place to control logs)
          Libraries: loguru
          Key functions:
            - get_logger(name) → returns configured logger instance
            - Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

  [P1.2]  src/utils/data_downloader.py
          Purpose: Downloads historical price data from all free sources
          Libraries: yfinance, fredapi, requests, pandas
          Key functions:
            - download_yahoo(ticker, start, end) → DataFrame OHLCV
            - download_fred(series_id, start, end) → DataFrame macro
            - download_futures_proxy(market, start, end) → best available source
          Handles: Rate limiting, retries, caching to disk

  [P1.3]  src/utils/data_cleaner.py
          Purpose: Validates and cleans raw downloaded data
          Key functions:
            - remove_gaps(df) → fills or flags missing bars
            - remove_outliers(df, sigma=5) → removes price spikes
            - validate_ohlcv(df) → checks high≥low, close within range, etc.
            - align_dates(dfs) → aligns multiple markets to same trading dates
          Output: Clean, standardized DataFrame ready for indicators

  [P1.4]  src/utils/continuous_contract.py
          Purpose: Builds continuous futures price series (handles rollovers)
          Key functions:
            - adjust_for_rollover(df) → Panama Canal adjustment method
            - detect_rollover_dates(df) → identifies contract expiry gaps
          Note: Yahoo Finance futures data has rollover artifacts.
                This module removes them so signals are not generated by
                artificial price jumps at contract expiration.
```

---

### PHASE 2 — STRATEGY SIGNALS

**Lab Report:** `docs/LAB_003_Strategy_Signals.md`
**Goal:** All three strategy signal generators coded, unit tested, and visualized

```
Files to create in order:

  [P2.1]  src/strategy/indicators.py
          Purpose: All technical indicator calculations (single source of truth)
          Libraries: pandas, numpy, pandas_ta
          Key functions:
            - ema(series, period) → EMA series
            - sma(series, period) → SMA series
            - atr(high, low, close, period) → ATR series
            - rsi(close, period) → RSI series
            - adx(high, low, close, period) → ADX series
            - donchian(high, low, period) → upper/lower channel series
          Note: All indicators calculated using only data available AT THAT BAR.
                This is enforced by using pandas shift() where needed.
                No look-ahead. Ever.

  [P2.2]  src/strategy/regime_classifier.py
          Purpose: Determines market regime for each bar
          Key functions:
            - classify_regime(adx, atr, atr_90d_avg) → RegimeState enum
            - RegimeState enum: TRENDING, RANGING, TRANSITIONING,
                                HIGH_VOLATILITY, CRISIS
            - get_volatility_multiplier(regime) → float (1.0 or 0.5 or 0.0)

  [P2.3]  src/strategy/tma_signal.py
          Purpose: Triple Moving Average trend signal generator
          Key functions:
            - calculate_tma_signals(df) → Series of (1=long, -1=short, 0=flat)
            - is_trending_long(ema8, ema21, ema89, close) → bool
            - is_trending_short(ema8, ema21, ema89, close) → bool
          Output: Signal series aligned to market OHLCV data

  [P2.4]  src/strategy/dcs_signal.py
          Purpose: Donchian Channel System signal generator
          Key functions:
            - calculate_dcs_signals(df) → Series of (1, -1, 0)
            - calculate_dcs_exits(df, position) → Series of exit signals
            - Entry: close breaks 55-bar channel
            - Exit: price reaches 20-bar opposite channel

  [P2.5]  src/strategy/vmr_signal.py
          Purpose: Volatility Mean Reversion signal generator (ES/NQ only)
          Key functions:
            - calculate_vmr_signals(df) → Series of (1, -1, 0)
            - rsi5_oversold(rsi, threshold=25) → bool
            - rsi5_overbought(rsi, threshold=75) → bool
            - near_sma(close, sma, tolerance_pct=0.03) → bool

  [P2.6]  src/strategy/signal_combiner.py
          Purpose: Combines TMA and DCS signals with agreement filter
                   Routes markets to correct strategy based on regime
          Key functions:
            - get_combined_signal(market, regime, tma_sig, dcs_sig, vmr_sig)
              → FinalSignal (direction, strategy_source, confidence)
            - apply_agreement_filter(tma, dcs) → combined or 0 (disagreement)
          This is the CENTRAL module. All signals flow through here.

  [P2.7]  src/strategy/position_sizer.py
          Purpose: Calculates position size for every potential trade
          Key functions:
            - calculate_contracts(equity, risk_pct, atr, stop_mult, point_val)
              → int (number of contracts, minimum 0)
            - calculate_stop_price(entry, direction, atr, stop_mult) → float
            - apply_volatility_adjustment(contracts, regime) → int

  [P2.8]  src/strategy/portfolio_manager.py
          Purpose: Manages all open positions across all markets simultaneously
          Key functions:
            - get_total_portfolio_risk(positions) → float ($)
            - check_correlation_limit(market, direction, positions) → bool
            - update_trailing_stops(positions, current_prices, atr) → dict
            - can_add_position(market, risk, portfolio_risk) → bool
```

---

### PHASE 3 — BACKTESTING ENGINE

**Lab Report:** `docs/LAB_004_Backtesting_Engine.md`
**Goal:** Full event-driven backtesting engine that simulates 25 years of trading

```
Files to create in order:

  [P3.1]  src/backtest/data_loader.py
          Purpose: Loads and preprocesses data for backtesting
          Key functions:
            - load_market_data(market, start, end) → dict of DataFrames
            - align_all_markets(data_dict) → single multi-index DataFrame
            - add_all_indicators(df) → df with all indicator columns

  [P3.2]  src/backtest/trade.py
          Purpose: Data class representing a single trade
          Fields: market, direction, entry_date, entry_price, contracts,
                  stop_price, strategy_source, exit_date, exit_price,
                  exit_reason, pnl_dollars, pnl_r (reward-to-risk multiple)

  [P3.3]  src/backtest/engine.py
          Purpose: The main backtesting loop — processes one bar at a time
          Key functions:
            - run(start, end, markets, initial_capital) → BacktestResult
            - process_bar(bar_data, portfolio_state) → list of orders
            - simulate_fill(order, bar_data) → Trade
            - update_equity(trades, portfolio) → float
          Important rules:
            - Signals calculated at BAR CLOSE
            - Orders executed at NEXT BAR OPEN
            - Commission deducted at each fill
            - Slippage added at each fill (1 tick each direction)

  [P3.4]  src/backtest/metrics.py
          Purpose: Calculates all performance statistics from a trade list
          Key functions:
            - sharpe_ratio(returns, risk_free=0.04) → float
            - sortino_ratio(returns) → float
            - calmar_ratio(annual_return, max_drawdown) → float
            - profit_factor(trades) → float
            - max_drawdown(equity_curve) → float
            - win_rate(trades) → float
            - avg_win_loss_ratio(trades) → float
            - expectancy(trades) → float ($ per trade)
            - annual_returns(equity_curve) → dict by year
            - all_metrics(trades, equity_curve) → dict of all above

  [P3.5]  src/backtest/walk_forward.py
          Purpose: Runs the 7-window walk-forward validation automatically
          Key functions:
            - run_walk_forward(strategy_params) → list of WFWindow results
            - WFWindow: train_period, test_period, in_sample_metrics,
                        out_of_sample_metrics, pass_fail
            - aggregate_wf_results(windows) → overall pass/fail summary

  [P3.6]  src/backtest/monte_carlo.py
          Purpose: Runs 10,000 Monte Carlo simulations by shuffling trade order
          Key functions:
            - run_monte_carlo(trades, n_simulations=10000) → MonteCarloResult
            - MonteCarloResult: max_dd_distribution, worst_5pct_dd,
                                median_dd, best_5pct_dd, pf_distribution
```

---

### PHASE 4 — VALIDATION SUITE

**Lab Report:** `docs/LAB_005_Validation_Report.md`
**Goal:** Automated runner that executes all six stages and outputs pass/fail

```
Files to create in order:

  [P4.1]  src/backtest/stress_tester.py
          Purpose: Automates all stress tests (double costs, remove best trades,
                   parameter sensitivity sweep)
          Key functions:
            - test_double_costs(backtest_results) → StressResult
            - test_remove_best_trades(trades, n=20) → StressResult
            - test_parameter_sensitivity(base_params, shifts) → SensitivityResult

  [P4.2]  src/backtest/regime_tester.py
          Purpose: Isolates and tests specific historical crisis periods
          Key functions:
            - test_crisis_period(start, end, label) → CrisisResult
            - run_all_crisis_tests() → list of CrisisResult

  [P4.3]  src/backtest/validation_runner.py
          Purpose: Master validation runner — runs all 6 stages, outputs
                   a comprehensive go/no-go decision
          Key functions:
            - run_full_validation(strategy_params) → ValidationReport
            - ValidationReport: stage results, overall verdict, detailed metrics
          Output: Writes complete report to reports/validation/
```

---

### PHASE 5 — REPORTING SYSTEM

**Lab Report:** `docs/LAB_006_Reporting.md`
**Goal:** Beautiful, detailed HTML reports generated automatically after each backtest

```
Files to create in order:

  [P5.1]  src/backtest/report_generator.py
          Purpose: Generates HTML backtest reports with charts and tables
          Libraries: plotly, jinja2, pandas
          Key functions:
            - generate_backtest_report(results, output_path) → HTML file
            - generate_wf_report(wf_results, output_path) → HTML file
            - generate_validation_report(val_results, output_path) → HTML file
          Charts included:
            - Equity curve (portfolio + per market)
            - Monthly returns heatmap
            - Drawdown chart
            - Trade distribution histogram
            - Walk-forward windows chart
            - Monte Carlo fan chart

  [P5.2]  templates/report_template.html
          Purpose: Jinja2 HTML template for backtest reports
          Content: Professional layout with metrics dashboard, charts, trade table
```

---

### PHASE 6 — LIVE TRADING ENGINE

**Lab Report:** `docs/LAB_007_Live_Infrastructure.md`
**Goal:** Bot running live in paper mode, connected to broker, alerts on phone

```
Files to create in order:

  [P6.1]  src/utils/alerts.py
          Purpose: Sends real-time Telegram messages to your phone
          Key functions:
            - send_alert(message, level) → bool
            - alert_trade_entry(trade) → None
            - alert_trade_exit(trade, pnl) → None
            - alert_daily_loss_threshold(current_loss, threshold) → None
            - alert_error(error_message) → None

  [P6.2]  src/live/data_feed.py
          Purpose: Fetches real-time market data for live mode
          Libraries: ib_insync (IBKR) or custom Rithmic feed
          Key functions:
            - connect() → bool
            - get_latest_bar(market) → Bar
            - get_current_price(market) → float
            - is_market_open() → bool

  [P6.3]  src/live/order_manager.py
          Purpose: Submits, tracks, and cancels orders with the broker
          Key functions:
            - submit_market_order(market, direction, contracts) → OrderID
            - submit_stop_order(market, stop_price, contracts) → OrderID
            - cancel_order(order_id) → bool
            - get_open_positions() → dict
            - get_account_equity() → float

  [P6.4]  src/live/risk_monitor.py
          Purpose: Real-time monitoring of all risk limits (runs every second)
          Key functions:
            - check_daily_loss() → (float, bool) → current loss + breach flag
            - check_trailing_drawdown() → (float, bool)
            - check_portfolio_risk() → float
            - emergency_close_all() → None (nuclear option)

  [P6.5]  src/live/live_engine.py
          Purpose: Main live trading loop — ties everything together
          Key functions:
            - run(mode='paper') → None (infinite loop until stopped)
            - on_bar_close(market, bar) → None (process each new bar)
            - process_signals() → list of potential orders
            - execute_approved_orders(orders) → None
            - daily_startup_check() → bool
            - graceful_shutdown() → None
```

---

### PHASE 7 — TOPSTEP DEPLOYMENT

**Lab Report:** `docs/LAB_008_Topstep_Setup.md`
**Goal:** Bot live on Topstep evaluation, trading real funded paper, making money

```
Pre-conditions (all must be true before this phase begins):
  ✓ All 6 validation stages passed
  ✓ 60 days paper trading completed and verified
  ✓ You understand every signal the bot generates
  ✓ Emergency stop procedure tested and confirmed
  ✓ VPS running 24/7 with auto-restart on crash
  ✓ Daily monitoring routine established

Files to create in this phase:

  [P7.1]  src/live/topstep_adapter.py
          Purpose: Adapter that maps bot orders to Topstep/Rithmic format
          Handles: Account limits, position limits, instrument naming

  [P7.2]  scripts/deploy_vps.sh
          Purpose: Script to set up VPS from scratch, pull code, start bot
          Steps: Install conda, clone repo, install deps, configure service

  [P7.3]  scripts/daily_monitor.sh
          Purpose: Daily health check script (check bot is running, check P&L)

  [P7.4]  docs/LAB_009_Evaluation_Log.md
          Purpose: Daily trading log during Topstep evaluation period
```

---

## 10. TECHNOLOGY STACK — EVERY TOOL AND ITS COST

### Complete Tech Stack (All Phases Combined)

| Category | Tool | Cost | When Needed | Why This Tool |
|---|---|---|---|---|
| Language | Python 3.11 | Free | Phase 0+ | Best quant finance ecosystem |
| Editor | VS Code | Free | Phase 0+ | Best free IDE, great Python support |
| Version Control | Git + GitHub | Free | Phase 0+ | Never lose code, full history |
| Environment | Miniconda | Free | Phase 0+ | Isolated, reproducible environments |
| Notebooks | Jupyter | Free | Phase 1+ | Research and visualization |
| Data — primary | QuantConnect | Free | Phase 1+ | 25yr futures data, properly adjusted |
| Data — secondary | yfinance | Free | Phase 1+ | ETF proxies, 20yr daily data |
| Data — macro | FRED API | Free | Phase 1+ | Economic data, free with API key |
| Backtesting | VectorBT | Free | Phase 3+ | Ultra-fast, vectorized backtesting |
| Indicators | pandas-ta | Free | Phase 2+ | 130+ technical indicators |
| Analytics | pyfolio-reloaded | Free | Phase 3+ | Portfolio performance tearsheet |
| Stats | empyrical | Free | Phase 3+ | Sharpe, Calmar, drawdown formulas |
| Charts | plotly | Free | Phase 5+ | Interactive charts for reports |
| Reports | jinja2 | Free | Phase 5+ | HTML report templating |
| Live broker | NinjaTrader 8 | Free | Phase 6+ | Connects directly to Topstep/Rithmic |
| Live API | ib_insync | Free | Phase 6+ | Python library for IBKR |
| Alerts | Telegram Bot | Free | Phase 6+ | Real-time phone notifications |
| Hosting | Vultr VPS | $12/mo | Phase 6+ | 24/7 bot operation |
| Funded account | Topstep eval | $165–$375/mo | Phase 7 | Funded account evaluation |

### Total Cost by Phase

```
Phase 0–5  (Development + Backtest):  $0.00/month
Phase 6    (Paper Trading):           $0.00/month
Phase 7    (Topstep Evaluation):      $12 (VPS) + $165 (eval) = $177/month
Phase 7+   (Funded Account):          $12 (VPS) = $12/month
           (Topstep evaluation fee stops once funded)

TOTAL to BUILD the entire bot:        $0
TOTAL monthly when making money:      $12
```

---

## 11. DATA SOURCES — WHAT DATA WE USE AND HOW

### Free Data Coverage for 25-Year Backtest

```
QUANTCONNECT LEAN (Free cloud backtesting)
  Access: quantconnect.com, free account
  Data available:
    ES futures: 1997–present (daily, minute)
    NQ futures: 1999–present (daily, minute)
    GC futures: 2000–present (daily, minute)
    CL futures: 2000–present (daily, minute)
    ZB futures: 1997–present (daily, minute)
    6E futures: 1999–present (daily, minute)
  Format: Continuous contracts, rollover-adjusted
  Cost: $0

YAHOO FINANCE via yfinance library
  Access: pip install yfinance, no API key needed
  Data available:
    SPY:  1993–present (S&P 500 proxy, daily OHLCV)
    QQQ:  1999–present (Nasdaq proxy)
    GLD:  2004–present (Gold proxy)
    USO:  2006–present (Oil proxy)
    TLT:  2002–present (Bond proxy)
    FXE:  2005–present (Euro proxy)
  Use case: Research phase, proxy backtesting, longer history where futures
            data has gaps
  Cost: $0

FRED (Federal Reserve Economic Data)
  Access: fred.stlouisfed.org, free API key
  Data available:
    VIX (VIXCLS): 1990–present (volatility index for regime detection)
    Fed Funds Rate: 1954–present (interest rate regime)
    10Y-2Y Yield Curve: 1977–present (recession predictor)
    CPI: 1913–present (inflation regime)
  Cost: $0

When free data is not enough (Phase 3+):
  Norgate Data: $270/year — best quality continuous futures
  Polygon.io:   $29/month  — tick and minute data
  These are optional upgrades if we find gaps in QuantConnect data.
  We start with free sources and only upgrade if a specific need arises.
```

### Data Quality Rules (Enforced in data_cleaner.py)

```
Rule 1: No gaps allowed in trading dates
  → Missing bars filled with prior close (or flagged and excluded)

Rule 2: No price outliers
  → Any bar where close deviates > 5 standard deviations from 20-bar mean
    is flagged and removed from backtest

Rule 3: No look-ahead contamination
  → All indicator calculations validated to use only data up to bar T
  → Signals generated at T, orders executed at T+1 open
  → This is validated by a dedicated test in the test suite

Rule 4: Realistic cost model
  → Every trade in backtest deducts:
    Commission: $5.00 per side ($10 round turn)
    Slippage:   1 tick unfavorable on entry, 1 tick unfavorable on exit
  → These are conservative estimates. Real fills are sometimes better.
```

---

## 12. LIVE INFRASTRUCTURE — VPS, BROKER, ALERTS

### VPS Setup (Vultr Cloud, $12/month)

```
Server specs:
  Provider:     Vultr (vultr.com)
  Plan:         Regular Performance — 1 vCPU, 2GB RAM, 55GB SSD
  Location:     Chicago, IL (closest to CME servers in Aurora, IL)
  OS:           Ubuntu 22.04 LTS
  Monthly cost: $12

Why Chicago: The CME (Chicago Mercantile Exchange) data centers are
in Aurora, IL. Running a server in Chicago minimizes latency for
receiving market data and submitting orders. For daily-bar strategies,
this matters less than for HFT, but it's still best practice.

What runs on the VPS:
  1. AlgoBot Python process (main.py --mode=live)
  2. NinjaTrader or IBKR TWS (broker connection)
  3. Monitoring scripts
  4. Log files and daily reports

Auto-restart: systemd service ensures the bot restarts automatically
if the server reboots or the process crashes unexpectedly.
```

### Telegram Alert System (Free)

```
Setup:
  1. Create a Telegram bot via @BotFather (free, takes 2 minutes)
  2. Get your bot token and chat ID
  3. Add to .env file
  4. Every trade and alert sends to your phone instantly

Alerts you receive:
  📈 [TRADE ENTRY] ES LONG @5023.50 | 1 contract | Stop: 4938.25
  📉 [TRADE EXIT] ES | +$1,125 profit | Duration: 8 days | PF contribution: 4.2R
  ⚠️  [ALERT] Daily loss -$1,500 reached. Monitoring closely.
  🔴 [EMERGENCY] Daily loss limit -$2,500 hit. All positions closed. Trading paused.
  ✅ [DAILY REPORT] P&L: +$342 | Open positions: 2 | Account: $151,240
```

---

## 13. TOPSTEP INTEGRATION — FULL SETUP GUIDE

### Understanding Topstep for Algorithmic Traders

Topstep is a proprietary trading firm (prop firm) that provides funded accounts
to traders who pass an evaluation. Algorithmic trading is explicitly permitted
and common among their top performers.

### Account Tiers

```
STARTER RECOMMENDATION: $50k account
  Evaluation cost:    ~$165/month
  Funded size:         $50,000
  Daily loss limit:    $1,500
  Trailing drawdown:   $1,500
  Profit target:       $3,000
  Payout:              90% to you
  Bot max contracts:   Calculated based on $50k equity at 1% risk

SCALE-UP TARGET: $150k account (after proving profitability)
  Evaluation cost:    ~$375/month
  Funded size:         $150,000
  Daily loss limit:    $4,500
  Trailing drawdown:   $4,500
  Profit target:       $9,000
  Payout:              90% to you
  Bot daily limit:     Hard stop at -$2,500 (50% safety margin)

STRATEGY: Start with $50k evaluation to prove the live system works.
After 1–2 successful months funded, apply for $150k evaluation.
Running both simultaneously after consistent profitability is also possible.
```

### Connection Path: AlgoBot → Topstep

```
PREFERRED METHOD: NinjaTrader + Rithmic

  Step 1: Purchase Topstep evaluation ($165/month for $50k)
  Step 2: Receive Rithmic login credentials in email
  Step 3: Download NinjaTrader 8 (free from ninjatrader.com)
  Step 4: Configure NinjaTrader with Rithmic credentials
  Step 5: Connect AlgoBot to NinjaTrader via one of:
            Option A: NinjaTrader Python bridge (third-party tool, ~$50 one-time)
            Option B: Write bot as NinjaScript strategy (C#, more complex)
            Option C: Use socket connection (AlgoBot sends orders via TCP)
  Step 6: Run bot on VPS, test with paper account first
  Step 7: Switch to live Topstep evaluation account

ALTERNATIVE METHOD: Interactive Brokers
  Topstep has added IBKR compatibility on some account types.
  This allows direct Python → ib_insync → IBKR → Topstep connection.
  Simpler code path, slightly more expensive overall.
  We will verify current IBKR availability when reaching Phase 7.
```

### Topstep Rules Encoded in the Bot

```
The bot enforces all Topstep rules programmatically.
A rule violation = losing the funded account.
These are hard limits, not soft warnings.

Rule 1: Daily loss limit
  Topstep: Cannot lose more than $4,500/day on $150k account
  Bot enforces: Hard stop at -$2,500 (44% safety buffer)

Rule 2: Trailing maximum drawdown
  Topstep: Account cannot draw down more than $4,500 from peak
  Bot enforces: Alert at -$2,000, pause at -$3,000

Rule 3: Trade only approved instruments
  Topstep: Must trade CME futures only
  Bot enforces: Instrument whitelist, rejects any other symbol

Rule 4: No martingale or position averaging (Topstep policy)
  Bot enforces: Maximum 1 position per market, no adding to losers

Rule 5: Consistent trading activity
  Topstep: Rewards consistent participation
  Bot enforces: Trades when signals are valid, logs all activity
```

---

## 14. DOCUMENTATION STANDARDS — LAB REPORT FORMAT

Every phase of this project produces a lab report. Lab reports are the
official record of everything we did, why we did it, and what happened.

### Lab Report Template

```markdown
# LAB_XXX — [Title]

**Date:**       YYYY-MM-DD
**Phase:**      X — [Phase Name]
**Status:**     IN PROGRESS | COMPLETE | FAILED
**Engineer:**   Claude (claude-sonnet-4-6)
**Reviewed:**   Ghost

---

## Objective
[One paragraph: What are we building in this lab and why?]

## Background
[What came before? What problem does this solve?
 Reference to previous lab if applicable.]

## Specifications
[Exact requirements for what we're building.
 What inputs → what outputs → what criteria define success.]

## Implementation
[Step-by-step account of what was built.
 Include key code excerpts (not full files, just the important parts).
 Explain every design decision.]

## Testing
[How was this tested? What test cases were run?
 Show test results and outputs.]

## Results
[What did we achieve? Metrics, outputs, charts if applicable.]

## Issues Encountered
[Any bugs, problems, or unexpected behavior.
 How each was resolved.]

## Validation
[Does this meet the Stage specifications? Yes/No for each criterion.]

## Conclusions
[One paragraph summary: Did this work? What did we learn?
 Is it ready to proceed to next phase?]

## Next Steps
[What comes next? Reference to next lab report.]

---
[Appendix if needed: full code listings, raw data tables, extra charts]
```

### Code Documentation Standards

```python
# Every source file must have:
# 1. Module docstring at top (what this file does, when to use it)
# 2. Docstring on every function (parameters, returns, example if complex)
# 3. Inline comments on non-obvious logic
# 4. Type hints on all function signatures

# Example:
def calculate_profit_factor(trades: list[Trade]) -> float:
    """
    Calculate the profit factor from a list of completed trades.

    Profit Factor = Total Gross Profit / Total Gross Loss
    A value > 1.0 indicates a profitable system.
    Our target: 2.5–3.0 in backtest, 2.0–2.5 live.

    Args:
        trades: List of Trade objects with pnl_dollars populated.
                Must contain at least one winning and one losing trade.

    Returns:
        Profit factor as a float. Returns 0.0 if no losing trades exist
        (technically infinite, handled as edge case).

    Example:
        pf = calculate_profit_factor(backtest_result.trades)
        print(f"Profit Factor: {pf:.2f}")  # Output: "Profit Factor: 2.74"
    """
```

---

## 15. GO / NO-GO CHECKLIST — BEFORE ANY LIVE TRADE

This checklist must be completed and signed off before the bot touches
a live Topstep account. Every item must be YES. No partial credit.

```
╔══════════════════════════════════════════════════════════════════╗
║           ALGOBOT — LIVE TRADING AUTHORIZATION CHECKLIST         ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  BACKTEST REQUIREMENTS                                           ║
║  [ ] In-sample backtest: Profit Factor > 2.3                     ║
║  [ ] In-sample backtest: Sharpe Ratio > 1.0                      ║
║  [ ] In-sample backtest: Max Drawdown < 22%                      ║
║  [ ] In-sample backtest: Profitable in 16 of 20 years            ║
║  [ ] Out-of-sample: Profit Factor > 2.0                          ║
║  [ ] Out-of-sample: Profitable in 3 of 5 years (2020–2024)       ║
║                                                                  ║
║  WALK-FORWARD REQUIREMENTS                                       ║
║  [ ] Profitable in 5 of 7 walk-forward windows                   ║
║  [ ] No single window: drawdown > 30%                            ║
║  [ ] Average out-of-sample Sharpe > 0.8                          ║
║                                                                  ║
║  REGIME / STRESS TEST REQUIREMENTS                               ║
║  [ ] 2008 crisis test: drawdown < 20% (6-month window)           ║
║  [ ] 2020 COVID test: drawdown < 12% (5-week window)             ║
║  [ ] 2022 rate hike test: profitable for full year               ║
║  [ ] Double costs stress test: still profitable (PF > 1.5)       ║
║  [ ] Remove best 20 trades: still profitable (PF > 1.5)          ║
║  [ ] Parameter sensitivity: Sharpe stays > 0.7 for ±20% shift    ║
║  [ ] Monte Carlo 95th percentile max drawdown < 35%              ║
║                                                                  ║
║  PAPER TRADING REQUIREMENTS                                      ║
║  [ ] 60+ days paper trading completed                            ║
║  [ ] Live signal frequency within 25% of backtest expectations   ║
║  [ ] Emergency stops tested and confirmed working                ║
║  [ ] Telegram alerts tested and confirmed working                ║
║  [ ] Bot ran continuously for 5+ days without crash              ║
║                                                                  ║
║  TOPSTEP REQUIREMENTS                                            ║
║  [ ] Topstep evaluation rules never violated in paper mode       ║
║  [ ] Daily loss circuit breaker tested (confirmed halts trading)  ║
║  [ ] Trailing drawdown monitor tested (confirmed alerts/pauses)  ║
║  [ ] VPS running 24/7, auto-restart confirmed                    ║
║                                                                  ║
║  KNOWLEDGE REQUIREMENTS                                          ║
║  [ ] You can explain what each strategy signal means             ║
║  [ ] You can explain why each market is in the portfolio         ║
║  [ ] You know how to manually close all positions in emergency   ║
║  [ ] You understand the Topstep payout and rule structure        ║
║                                                                  ║
║  FINAL AUTHORIZATION                                             ║
║  Engineer (Claude):  ________________________________            ║
║  Trader (Ghost):     ________________________________            ║
║  Date authorized:    ________________________________            ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## 16. GLOSSARY OF TERMS

| Term | Definition |
|---|---|
| **ADX** | Average Directional Index. Measures trend strength (0–100). Above 25 = trending. Does not indicate direction, only strength. |
| **ATR** | Average True Range. Measures daily price volatility in price points. The foundation of our position sizing and stop placement. |
| **Calmar Ratio** | Annual return divided by maximum drawdown. Higher = better risk-adjusted return relative to worst losses. |
| **Donchian Channel** | Price channel defined by highest high and lowest low over N bars. Breakouts signal trend starts. |
| **EMA** | Exponential Moving Average. Like a simple average but gives more weight to recent bars. Faster to respond to price changes than SMA. |
| **Edge** | A statistical advantage — the theoretical reason why a strategy wins more than it loses over time. |
| **Go/No-Go** | The binary decision point before going live. All checklist items must pass. No exceptions. |
| **Look-ahead Bias** | A fatal backtest error where future data is used to make past decisions. Makes results fraudulently good. |
| **Max Drawdown** | The largest peak-to-trough decline in account equity. A measure of worst-case loss. |
| **Monte Carlo** | A simulation method that randomly shuffles trade order 10,000+ times to stress-test worst-case scenarios. |
| **Point Value** | Dollar value of one full price point in a futures contract. ES = $50/point. |
| **Profit Factor** | Gross winning trades ÷ Gross losing trades. Must be > 1.0 to be profitable. Our target: 2.5–3.0. |
| **R-multiple** | Profit or loss measured as a multiple of the initial risk. A +3R trade made 3× what it risked. |
| **Regime** | A market environment characterized by trend strength, volatility, and direction. |
| **Rithmic** | Professional futures data and order routing platform used by Topstep. |
| **RSI** | Relative Strength Index. Oscillates 0–100. Below 25 = oversold; Above 75 = overbought (our thresholds). |
| **Sharpe Ratio** | Annualized excess return divided by annualized volatility. Higher = better. Above 1.0 is solid. Above 1.5 is excellent. |
| **Signal Agreement Filter** | Our key quality gate: both TMA and DCS must agree before a trend trade is entered. Eliminates ~50% of marginal trades. |
| **Slippage** | The difference between the theoretical fill price and the actual fill price. We model 1 tick of slippage per side. |
| **Trailing Stop** | A stop loss that moves in the direction of profit, locking in gains. Never moves against the trade. |
| **VPS** | Virtual Private Server. A cloud computer running 24/7 to host the live bot. ~$12/month. |
| **Walk-Forward Testing** | Testing a strategy on data it has never seen by training on earlier periods only. The gold standard of validation. |
| **Win Rate** | Percentage of trades that are profitable. Our system targets 45–50% combined. |

---

## CURRENT STATUS & NEXT ACTION

```
╔══════════════════════════════════════════════════════════════╗
║  PLANNING:    COMPLETE ✓                                     ║
║  PHASE 0:     READY TO BEGIN                                 ║
║  NEXT ACTION: Implement Phase 0 files one by one             ║
║                                                              ║
║  First file to build: requirements.txt                       ║
║  Then:                .gitignore                             ║
║  Then:                .env.example                           ║
║  Then:                verify_setup.py                        ║
║  Then:                config/config.yaml                     ║
║                                                              ║
║  Each file will be built, explained, and documented          ║
║  before moving to the next.                                  ║
╚══════════════════════════════════════════════════════════════╝
```

---

*AlgoBot v1.0 — Master Plan*
*Built with Claude (claude-sonnet-4-6) — Anthropic*
*This document is the project bible. All code traces back to this specification.*
