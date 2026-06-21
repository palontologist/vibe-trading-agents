<p align="center">
  <b>English</b> | <a href="README_zh.md">中文</a> | <a href="README_ja.md">日本語</a> | <a href="README_ko.md">한국어</a> | <a href="README_ar.md">العربية</a>
</p>

<p align="center">
  <img src="assets/icon.png" width="120" alt="Vibe-Trading Logo"/>
</p>

<h1 align="center">Vibe-Trading: AI-Powered Multi-Agent Trading System</h1>

<p align="center">
  <b>From Research to Live Execution — Paper Trading, Backtesting, and Real-Time Agents Across Crypto, Forex, and Commodities</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Backend-FastAPI-009688?style=flat" alt="FastAPI">
  <a href="https://pypi.org/project/vibe-trading-ai/"><img src="https://img.shields.io/pypi/v/vibe-trading-ai?style=flat&logo=pypi&logoColor=white" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow?style=flat" alt="License"></a>
</p>

---

## What This Fork Adds

Built on top of [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading), this fork extends the original research/backtest framework with a **complete paper and live trading system** — including multi-exchange support, forex integration, aggressive scalping strategies, and real-time autonomous execution.

### Key Additions

| Module | What It Does |
|--------|--------------|
| **Paper Trading Engine** | 28-module autonomous trading system with paper execution, risk management, and trade logging |
| **Multi-Agent Orchestrator** | 5-agent pipeline (Analyst → Signal → Risk → Executor → Auditor) with technical-only signals |
| **Realtime Orchestrator** | Continuous tick-level trading loop with trailing stops, Kelly sizing, and position management |
| **Forex Paper Trading** | Real ECB data (Frankfurter API) for USD/MXN, BRL, ZAR, NZD, HUF, MYR, PLN with simulated intraday volatility |
| **Multi-Exchange Support** | Simultaneous trading on Binance (XAU, XAG, NATGAS) and Hyperliquid (PAXG, SPX) |
| **Aggressive Forex Scalper** | 10x leverage, 60% position sizing, RSI+Bollinger signals, targeting volatile exotic pairs |
| **Growth Framework** | $10 → $10K target: 5-signal confluence, Kelly sizing, daily compounding, multi-asset |
| **Wallet Connector** | Read-only Base wallet integration for on-chain balance monitoring |
| **Live Hyperliquid Execution** | 74 real orders executed on Hyperliquid mainnet across 7 assets |
| **Performance Report** | Full agent scorecard with PnL analysis, bias detection, and improvement areas |

---

## Paper Trading System

### Architecture

```
MarketAnalyst → SignalGenerator → RiskAssessor → TradeExecutor → PortfolioManager
      |               |               |               |               |
   Indicators    RSI+Bollinger    Exposure Check    Order Execution   PnL Tracking
   Regime Det.   MA Crossover     Max Position      Paper/Live        Trade Log
   Volume Rat.   Confidence       Daily Limits      Hyperliquid       Equity Curve
```

### Modules

| Module | Purpose |
|--------|---------|
| `multi_agent.py` | 5-agent framework — Analyst, Signal, Risk, Executor, Auditor |
| `realtime_orchestrator.py` | Continuous autonomous trading loop (crypto + commodities) |
| `forex_paper_launcher.py` | Multi-exchange paper trader (Binance + Hyperliquid) |
| `aggressive_forex_launcher.py` | High-frequency forex scalper with Frankfurter ECB API |
| `sniper_launcher.py` | V13 momentum sniper — W token with trailing stops (70% WR) |
| `paper_engine.py` | Paper trade execution engine with slippage/fee simulation |
| `risk_manager.py` | Position limits, daily loss limits, leverage caps |
| `wallet_connector.py` | Read-only Base wallet reader (ETH, USDC, WETH) |
| `hyperliquid_executor.py` | Live Hyperliquid mainnet order execution |
| `agent_scorecard.py` | Post-trade agent performance scoring |

### Quick Start — Paper Trading

```bash
# Install dependencies
pip install -r paper_requirements.txt

cd agent

# Run crypto commodity paper trader ($10, 3x leverage)
python -m paper_trading.forex_paper_launcher

# Run aggressive forex scalper ($10, 10x leverage)
python -m paper_trading.aggressive_forex_launcher

# Run original Hyperliquid orchestrator
python -m paper_trading.realtime_orchestrator
```

### Configuration

```python
# Aggressive Forex Config
{
    "initial_cash": 10,
    "leverage": 10,
    "max_positions": 5,
    "position_pct": 0.60,   # 60% per trade
    "tp_pct": 0.004,         # 0.4% take profit
    "sl_pct": 0.0025,        # 0.25% stop loss
    "tick_interval": 30,
    "pairs": ["MXN", "BRL", "ZAR", "NZD", "HUF", "MYR", "PLN"]
}
```

---

## Live Trading Results

### Hyperliquid Mainnet (May 18-19, 2026)

| Metric | Value |
|--------|-------|
| Total Orders | 74 |
| Total Volume | $420.88 |
| Assets Traded | BTC, ETH, SOL, WIF, JUP, RENDER, ENA |
| Avg Trade Size | $9.35 |
| Short Bias | 98% (44/45 opens were sells) |
| Wallet | `0xCfC39b4DB2b974b77C0131f696ECcd93dC4DEfB7` |

**Key Observations:**
- Heavy short bias (44 of 45 entries were sells)
- Micro-scalping approach (~$9.35 average position)
- Expanded to altcoins (WIF, JUP, RENDER, ENA) on Day 2
- Rapid position changes within minutes

---

## Paper Trading Performance

### Crypto Perps (May 16)

| Trade | Side | Entry | Exit | PnL |
|-------|------|-------|------|-----|
| BTC-USDT | SHORT | $78,933 | $79,067 | -$10.14 |
| ETH-USDT | SHORT | $2,223 | $2,226 | -$7.68 |
| SOL-USDT | SHORT | $88.51 | $88.62 | -$8.04 |

**Total PnL:** -$25.86 (0% win rate)
**Issue:** Risk assessor calculated $5.5M exposure instead of actual ~$8K (calculation bug)

### Commodity Paper (June 20, Live)

5 positions across XAU, XAG, NATGAS, PAXG, SPX — $10 capital, 3x leverage.
Strongest signal: XAU LONG (87% confidence, RSI oversold).

### Aggressive Forex (June 20, Live)

USD/MXN LONG @ 17.3369 — $60 notional (60% of equity), 10x leverage.
7 volatile exotic pairs monitored with simulated intraday moves from ECB base rates.

### Sniper V13 — Momentum Scalper (June 21, Live)

| Metric | Value |
|--------|-------|
| Total PnL | +$0.178 (+1.8%) |
| Trades | 10 |
| Win Rate | 70% (7/10) |
| R:R | 0.94:1 |
| EV | +$0.020/trade |
| Best Trade | +$0.114 (23.9%) |

**Strategy**: Catch W token momentum (0.25%+ moves in 5 min), trailing stops at 0.35% peak, 95% position sizing with 20x leverage.

---

## Growth Framework: $10 → $10,000 in 10 Days

Aggressive multi-signal system targeting 100% daily compounding through high-leverage, multi-asset trading.

### Signal Stack (5 Layers)

| Signal | Weight | Logic |
|--------|--------|-------|
| **Sentiment Contrarian** | 25% | F&G < 25 → contrarian LONG, F&G > 75 → contrarian SHORT |
| **Momentum** | 25% | MA crossover + RSI(14) confirmation |
| **Breakout** | 20% | Bollinger squeeze + volume surge detection |
| **Rotation** | 15% | Weakest performer exit, high-beta entry (6h forced close) |
| **Factor Alpha** | 15% | Cross-sectional momentum/volatility ranking |

### Risk Controls

| Rule | Threshold |
|------|-----------|
| Hard stop loss | -3% |
| Trailing stop | -1.5% from peak |
| Max hold time | 6 hours |
| Daily loss limit | -20% |
| Circuit breaker | -30% drawdown |
| Max positions | 5 |

### Quick Start

```bash
cd agent
python -m paper_trading.growth.backtest    # Validate first
python -m paper_trading.growth.launcher    # Run live paper
```

**Full documentation**: [GROWTH_FRAMEWORK.md](GROWTH_FRAMEWORK.md)

---

## Data Sources

| Source | Markets | Method |
|--------|---------|--------|
| **Hyperliquid API** | PAXG, SPX, crypto perps | Real-time tick prices |
| **CCXT / Binance** | XAU, XAG, NATGAS | OHLCV candles |
| **Frankfurter API (ECB)** | USD/MXN, BRL, ZAR, NZD, HUF, MYR, PLN | Daily ECB rates + simulated intraday volatility |
| **Polymarket API** | Prediction markets (politics, crypto, events) | Binary outcome trading |

---

## Supported Markets

### Crypto Perpetual Futures (Hyperliquid)
- **880+ assets** including meme coins (POPCAT, W, JUP), major L1s (SOL, ETH, BTC)
- Up to **50x leverage**, 24/7 trading
- Best for momentum scalping and trend following
- See [docs/MARKETS.md](docs/MARKETS.md) for volatility data and asset rankings

### Polymarket Prediction Markets
- **Binary outcome** markets (Yes/No) on real-world events
- Categories: Politics, Crypto prices, Sports, Entertainment
- Prices represent probability ($0.65 = 65% chance)
- USDC settlement on Polygon blockchain
- Resolution-based profit: $1.00 if correct, $0.00 if wrong

### Forex (Currency Pairs)
- **Major pairs**: EUR/USD, GBP/USD, USD/JPY
- **Exotic pairs**: USD/MXN, USD/BRL, USD/ZAR (higher volatility)
- 24/5 trading, pip-based pricing
- Frankfurter ECB API for exchange rates

### Commodities
- **Energy**: Crude Oil (CL), Natural Gas (NG)
- **Metals**: Gold (GC), Silver (SI), Platinum (PL)
- **Agricultural**: Corn, Wheat, Soybeans
- Contract-based sizing, exchange hours

### Stocks/Equities
- NYSE, NASDAQ listings
- Fractional shares supported
- ETFs (SPY, QQQ) and individual stocks
- Standard market hours (9:30-16:00 ET)

For detailed market analysis, volatility data, and trading configurations, see **[docs/MARKETS.md](docs/MARKETS.md)**.

---

## Original Vibe-Trading Features

This fork retains all original capabilities from HKUDS/Vibe-Trading:

- **74 Trading Skills** — Specialist skills with persistent cross-session memory
- **29 Swarm Presets** — Pre-built multi-agent trading team workflows
- **7 Backtest Engines** — Cross-market composite testing with statistical validation
- **Multi-Platform Export** — TradingView (Pine Script v6), TDX (通达信), MetaTrader 5 (MQL5)
- **27 Tools** — Trade journal analyzer, shadow account, correlation heatmap, and more
- **6 Data Sources** — A-shares, HK/US equities, crypto, futures, forex

### CLI Reference

```bash
pip install vibe-trading-ai
vibe-trading                    # Interactive mode
vibe-trading --task "analyze BTC"  # Single task
vibe-trading --backtest <strategy>  # Run backtest
vibe-trading --swarm-presets    # List available agent teams
```

---

## Project Structure

```
Vibe-Trading/
├── agent/
│   ├── paper_trading/          # Paper trading system (28+ modules)
│   │   ├── growth/             # $10 → $10K Growth Framework
│   │   │   ├── strategy.py     # 5-signal confluence engine
│   │   │   ├── portfolio.py    # Kelly criterion sizing
│   │   │   ├── risk.py         # Trailing stops, hard stops, limits
│   │   │   ├── orchestrator.py # Continuous trading loop
│   │   │   ├── launcher.py     # Entry point
│   │   │   └── backtest.py     # Historical validation
│   │   ├── sniper_launcher.py      # V13 Momentum sniper (W token)
│   │   ├── multi_agent.py          # 5-agent orchestration framework
│   │   ├── realtime_orchestrator.py # Continuous autonomous trading loop
│   │   ├── forex_paper_launcher.py  # Multi-exchange commodity trader
│   │   ├── aggressive_forex_launcher.py # High-freq forex scalper
│   │   ├── paper_engine.py         # Paper trade execution
│   │   ├── risk_manager.py         # Risk controls
│   │   ├── wallet_connector.py     # Base wallet reader
│   │   ├── hyperliquid_executor.py # Live Hyperliquid execution
│   │   └── agent_scorecard.py      # Performance scoring
│   ├── paper_runs/             # Paper trade logs & equity curves
│   │   ├── growth/             # Growth framework logs
│   │   ├── sniper/             # Sniper V12/V13 logs
│   │   ├── forex_commodity/    # Commodity paper runs
│   │   └── aggressive_forex/   # Forex paper runs
│   ├── backtest_runs/          # Backtest results
│   ├── src/                    # Core trading source
│   └── backtest/               # Backtest engine
├── docs/
│   └── MARKETS.md              # Supported markets documentation
├── frontend/                   # React Web UI
├── GROWTH_FRAMEWORK.md         # $10 → $10K documentation
├── paper_requirements.txt      # Paper trading dependencies
├── run-vibe-trading.sh         # Systemd launcher script
└── vibe-trading.service        # Systemd service file
```

---

## Running as a Service

```bash
# Install systemd service
sudo cp vibe-trading.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start vibe-trading

# Check status
sudo systemctl status vibe-trading
journalctl -u vibe-trading -f
```

---

## Agent Scorecard

| Metric | Score | Notes |
|--------|-------|-------|
| **Aggressiveness** | 8/10 | 10x leverage, 60% sizing, 74 live orders in 40hrs |
| **Reliability** | 4/10 | Exposure calc bug, 0% paper win rate, simulated forex data |

### Known Issues
1. Risk assessor calculates $5.5M exposure instead of actual ~$8K
2. No trailing stops — only fixed TP/SL
3. Heavy short bias in live trades (98% sells)
4. Forex data uses synthetic intraday moves (ECB updates once daily)

### Improvement Roadmap
- [ ] Fix risk exposure calculation bug
- [ ] Add trailing stop losses
- [ ] Implement position staggering
- [ ] Integrate real-time forex data (OANDA / Alpha Vantage API)
- [ ] Track live trade PnL on-chain
- [ ] Add correlation-based position limits
- [x] **Growth Framework** — 5-signal confluence, Kelly sizing, $10→$10K target
- [ ] Upstream integration — Alpha Zoo (456 factors), 18 data sources, 10 brokers

---

## Configuration

### Environment Variables

```bash
# LLM Provider (for agent analysis)
export VENICE_API_KEY="your-key"
export VENICE_BASE_URL="https://api.venice.ai/api/v1"

# Hyperliquid (for live execution)
export PRIVATE_KEY="0x..."

# Paper Trading
export INITIAL_CASH=10
export LEVERAGE=10
export DAILY_TARGET=5
export TICK_INTERVAL=30

# Growth Framework ($10 → $10K)
export TARGET_EQUITY=10000
export TARGET_DAYS=10
```

### Paper Trading Config

```python
{
    "initial_cash": 10,
    "leverage": 10,
    "max_positions": 5,
    "position_pct": 0.60,
    "tp_pct": 0.004,
    "sl_pct": 0.0025,
    "tick_interval": 30,
    "pairs": ["MXN", "BRL", "ZAR", "NZD", "HUF", "MYR", "PLN"],
    "exchanges": ["binance", "hyperliquid"]
}
```

---

## License

MIT — see [LICENSE](LICENSE)

---

*Forked from [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) v0.1.7 — extended with paper trading, live execution, forex capabilities, and growth framework.*

---

## Upstream Exploration (v0.1.7 → v0.1.10)

Inspected 234 new commits from upstream HKUDS/Vibe-Trading. Key additions not yet integrated:

| Feature | Description | Status |
|---------|-------------|--------|
| **Alpha Zoo** | 456 pre-built quant factors (Qlib158, Alpha101, GTJA191, Academic) | Explored |
| **Global Data Layer** | 18 OHLCV sources (Eastmoney, Sina, Stooq, Finnhub, Alpha Vantage, Tiingo, FMP) | Explored |
| **10 Broker Connectors** | Alpaca, Binance, OKX, Tiger, Longbridge, Futu, Dhan, Shoonya, IBKR, Robinhood | Explored |
| **Live Trading Runtime** | Persistent runner, job scheduling, mandate system, enforcement | Explored |
| **Research Autopilot** | Hypothesis → signal engine → backtest loop | Explored |
| **Security Hardening** | Scanner, auth, shell tool gating, path validation | Explored |

**Note**: Upstream removed the legacy `paper_trading/` package in favor of new `agent/src/trading/` and `agent/src/live/` architecture. Direct merge would conflict with our custom paper trading system.
