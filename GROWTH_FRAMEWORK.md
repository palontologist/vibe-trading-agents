# Growth Framework: $10 -> $10,000 in 10 Days

An aggressive multi-signal trading system designed to compound $10 into $10,000 through high-leverage, multi-asset paper trading.

## Target Math

| Day | Target Equity | Daily Return | Multiplier |
|-----|---------------|--------------|------------|
| 0 | $10.00 | — | 1.0x |
| 1 | $20.00 | +100% | 2.0x |
| 2 | $40.00 | +100% | 4.0x |
| 3 | $80.00 | +100% | 8.0x |
| 5 | $320.00 | +100% | 32.0x |
| 7 | $1,280.00 | +100% | 128.0x |
| 10 | $10,000.00 | +100% | 1,000.0x |

**Required**: ~100% daily compounding (double each day) via 10x leverage + aggressive position sizing.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Growth Orchestrator                       │
│  (Continuous 15s loop: fetch → signals → trade → monitor)   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Strategy   │  │   Portfolio  │  │     Risk     │      │
│  │  (5-signal   │  │   (Kelly     │  │  (Trailing   │      │
│  │  confluence) │  │  criterion)  │  │   stops)     │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                  │              │
│  ┌──────┴─────────────────┴──────────────────┴──────┐      │
│  │              Data Fetchers (3 sources)            │      │
│  │  Hyperliquid │ Binance │ Frankfurter ECB          │      │
│  └──────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

## Signal Stack (5 Layers)

Each signal outputs a direction [-1.0, 1.0] and confidence [0-100]. Final signal = weighted average.

### 1. Sentiment Contrarian (25% weight)
```
F&G < 20  → +0.8 direction, 85% confidence (Extreme Fear → LONG)
F&G < 30  → +0.5 direction, 70% confidence (Fear → LONG)
F&G > 80  → -0.8 direction, 80% confidence (Extreme Greed → SHORT)
F&G > 70  → -0.5 direction, 65% confidence (Greed → SHORT)
```
Boost: +10% confidence if F&G trending toward extreme.

### 2. Momentum (25% weight)
- **RSI(14)**: <30 oversold (+0.4), >70 overbought (-0.4)
- **MA alignment**: Price > MA10 > MA20 > MA50 = bullish (+0.4)
- **5-bar momentum**: >3% move = +0.2 directional signal

### 3. Breakout (20% weight)
- **Bollinger squeeze**: Bandwidth < 70% of historical average
- **Volume surge**: Current volume > 1.5x 20-bar average
- **Breakout**: Price above upper BB + volume surge = +0.7

### 4. Rotation (15% weight)
From 赛博六壬交易员 strategy:
- **Entry**: High-beta asset replaces weakest held position
- **Exit**: Close weakest performer (lowest PnL)
- **6-hour forced close**: Time-based exit management
- **-2% loss exit**: Cut losing positions

### 5. Factor Alpha (15% weight)
Cross-sectional ranking inspired by Alpha Zoo:
- **Momentum factor**: 10-day return
- **Volatility factor**: 10-day realized vol (inverse-weighted)
- Combined score clipped to [-1.0, 1.0]

## Position Sizing (Kelly Criterion)

```python
kelly = (win_rate * win_loss_ratio - (1 - win_rate)) / win_loss_ratio
scaled = kelly * kelly_fraction * (confidence / 100)
position_size = min(scaled, max_position_pct)  # max 80%
```

- **Quarter-Kelly** (kelly_fraction=0.25) for conservative sizing
- **Dynamic boost**: +20% size when day_progress > 70%
- **Asset-specific leverage**: BTC/ETH 10x, alts 5x, commodities 10x

## Risk Management

| Rule | Threshold | Action |
|------|-----------|--------|
| Hard stop loss | -3% | Close immediately |
| Trailing stop | -1.5% from peak | Close immediately |
| Max hold time | 6 hours | Close (if profitable) |
| Daily loss limit | -20% of equity | Halt all trading |
| Circuit breaker | -30% drawdown | Close all positions |
| Max positions | 5 | Block new entries |
| Correlation limit | 2 per group | Block correlated entries |

### Correlation Groups
```python
{
    "btc_group": {"BTC", "ETH"},
    "alt_l1": {"SOL", "NEAR", "SUI"},
    "defi": {"AAVE", "LINK", "RENDER"},
    "meme": {"DOGE", "WIF", "JUP"},
    "commodity": {"XAU", "XAG"},
}
```

## Asset Universe

| Class | Assets | Source | Leverage |
|-------|--------|--------|----------|
| **Crypto** | BTC, ETH, SOL, WIF, JUP, RENDER, DOGE, SUI, LINK, NEAR, AAVE, FET | Hyperliquid | 5-10x |
| **Forex** | USD/MXN, USD/BRL, USD/ZAR, USD/NZD, USD/HUF, USD/MYR, USD/PLN | Frankfurter ECB | 10x |
| **Commodities** | XAU, XAG, NATGAS | Binance CCXT | 5-10x |

## Quick Start

```bash
cd agent

# Run growth framework
python -m paper_trading.growth.launcher

# Or with environment variables
INITIAL_CASH=10 LEVERAGE=10 TARGET_EQUITY=10000 TARGET_DAYS=10 \
  python -m paper_trading.growth.launcher

# Run backtest validation first
python -m paper_trading.growth.backtest
```

## Configuration

```python
{
    "initial_cash": 10,           # Starting capital
    "leverage": 10,               # Max leverage
    "target_equity": 10000,       # Target equity
    "target_days": 10,            # Days to target
    "crypto_interval": 15,        # Seconds between crypto ticks
    "strategy": {
        "min_confidence": 45,     # Minimum signal confidence
        "min_confluence": 0.25,   # Minimum combined signal
        "signal_weights": {
            "sentiment": 0.25,    # F&G contrarian
            "momentum": 0.25,     # MA + RSI
            "breakout": 0.20,     # Bollinger + volume
            "rotation": 0.15,     # Weakest out, high-beta in
            "factor": 0.15,       # Cross-sectional ranking
        }
    },
    "portfolio": {
        "initial_cash": 10,
        "leverage": 10,
        "max_positions": 5,
        "max_position_pct": 0.80,
        "kelly_fraction": 0.25,
    },
    "risk": {
        "max_daily_loss_pct": 0.20,
        "max_drawdown_pct": 0.30,
        "max_position_loss_pct": 0.03,
        "trailing_stop_pct": 0.015,
        "max_hold_seconds": 21600,  # 6 hours
        "max_positions": 5,
    }
}
```

## Backtest Results

### Synthetic Data (180 days)
| Metric | Value |
|--------|-------|
| Initial Cash | $10.00 |
| Final Equity | $12.34 |
| Total Return | 23.4% |
| Multiplier | 1.2x |
| Max Drawdown | 17.5% |
| Sharpe Ratio | 1.25 |
| Total Trades | 310 |
| Win Rate | 25% |

**Note**: Conservative results on random synthetic data. Real market data with trending behavior + 10x leverage is expected to produce significantly higher returns.

## Live Paper Trading

### First Trades (June 20, 2026)
```
Fear & Greed Index: 23 (Extreme Fear)

OPEN LONG XAG:  0.0860 @ $65.14 (notional=$5.60, margin=$0.56)
  Signal: F&G=23 Fear contrarian + MA10>MA20 + Factor ranking

OPEN LONG NATGAS: 1.5353 @ $3.26 (notional=$5.00, margin=$1.00)
  Signal: F&G=23 Fear contrarian + Factor ranking
```

## File Structure

```
agent/paper_trading/growth/
├── __init__.py        # Package exports
├── strategy.py        # 5-signal confluence engine (530 lines)
├── portfolio.py       # Kelly sizing + multi-asset allocation (417 lines)
├── risk.py            # Trailing stops, hard stops, daily limits (234 lines)
├── orchestrator.py    # Continuous trading loop (395 lines)
├── launcher.py        # Entry point (71 lines)
└── backtest.py        # Historical validation (336 lines)
```

## Inspirations

- **赛博六壬交易员**: F&G contrarian signals, rotation strategy, 6-hour forced close
- **Alpha Zoo** (HKUDS/Vibe-Trading): Cross-sectional factor ranking framework
- **Kelly Criterion**: Optimal position sizing for maximum growth
- **Hyperliquid**: Fast execution, deep liquidity, up to 50x leverage

## Disclaimer

This is a **paper trading** system for research and educational purposes. The $10 → $10K target is aspirational and extremely aggressive. Past backtest results do not guarantee future performance. Real trading involves risk of loss.
