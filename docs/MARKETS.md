# Supported Markets

Vibe Trading supports multiple market types for paper trading and live trading. This document covers all supported markets, their characteristics, and how to configure strategies for each.

## Table of Contents
- [Crypto Perpetual Futures (Hyperliquid)](#crypto-perpetual-futures-hyperliquid)
- [Polymarket Prediction Markets](#polymarket-prediction-markets)
- [Forex (Currency Pairs)](#forex-currency-pairs)
- [Commodities](#commodities)
- [Stocks/Equities](#stocksequities)

---

## Crypto Perpetual Futures (Hyperliquid)

### Overview
- **Exchange**: Hyperliquid (DEX)
- **Assets**: 880+ perpetual futures contracts
- **Leverage**: Up to 50x
- **API**: REST API at `https://api.hyperliquid.xyz/info`
- **Min Notional**: $10 per position

### Key Features
- 24/7 trading
- No KYC required
- On-chain orderbook
- Cross and isolated margin modes

### Popular Assets for Sniping
| Asset | 5min Volatility | 20x Leverage | Notes |
|-------|----------------|--------------|-------|
| POPCAT | 0.41% | 8.2% | Meme coin, high volatility |
| W | 0.36% | 7.2% | Wormhole token, trending moves |
| JUP | 0.22% | 4.5% | Jupiter, steady momentum |
| SOL | 0.15% | 3.0% | Major L1, liquid market |
| ETH | 0.10% | 2.0% | Major, less volatile |
| BTC | 0.08% | 1.6% | Benchmark, lowest volatility |

### API Response Format
```json
{
  "SOL": "142.50",
  "ETH": "3250.75",
  "W": "0.01124",
  "JUP": "0.2215"
}
```

### Configuration
```python
# In sniper_launcher.py or growth_trader.py
ASSETS = ["SOL", "ETH", "BTC"]  # Or meme coins for higher vol
LEVERAGE = 20
POSITION_PCT = 0.30  # 30% of capital per trade
```

---

## Polymarket Prediction Markets

### Overview
- **Platform**: Polymarket (Polygon blockchain)
- **Type**: Binary outcome markets (Yes/No)
- **Resolution**: Based on real-world events
- **Settlement**: USDC on Polygon

### Market Types
1. **Political Events**
   - Elections (US, EU, etc.)
   - Policy decisions
   - Political appointments

2. **Crypto Prices**
   - "Will BTC be above $100k on June 30?"
   - Price targets and milestones

3. **World Events**
   - Wars and conflicts
   - Natural disasters
   - Scientific discoveries

4. **Sports**
   - Championship winners
   - Season records

5. **Entertainment**
   - Awards (Oscars, Grammy)
   - Box office records

### Price Structure
- Prices range from $0.01 to $0.99
- Represents probability of outcome
- Example: $0.65 = 65% implied probability
- Settlement: $1.00 if correct, $0.00 if wrong

### API Access
```python
# Polymarket API
import requests

# Get market data
markets = requests.get("https://gamma-api.polymarket.com/markets").json()

# Get event prices
events = requests.get("https://gamma-api.polymarket.com/events").json()
```

### Configuration
```python
# In polymarket_trader.py
MIN_LIQUIDITY = 10000  # Minimum $10k liquidity
MIN_VOLUME = 5000      # Minimum $5k daily volume
MAX_EXPIRY_DAYS = 30   # Only trade markets expiring within 30 days
```

---

## Forex (Currency Pairs)

### Overview
- **Market**: Decentralized over-the-counter (OTC)
- **Trading Hours**: 24/5 (Monday-Friday)
- **Major Pairs**: EUR/USD, GBP/USD, USD/JPY
- **Min Size**: Often micro-lots (0.01 lots = 1,000 units)

### Major Pairs (Most Liquid)
| Pair | Spread | Volatility | Best Time (UTC) |
|------|--------|------------|-----------------|
| EUR/USD | 0.1-0.3 pips | Medium | 13:00-16:00 |
| GBP/USD | 0.2-0.5 pips | High | 13:00-16:00 |
| USD/JPY | 0.1-0.3 pips | Medium | 00:00-03:00 |
| USD/CHF | 0.2-0.5 pips | Low | 08:00-11:00 |

### Exotic Pairs (Higher Volatility)
| Pair | Spread | Volatility |
|------|--------|------------|
| USD/ZAR | 5-10 pips | High |
| USD/TRY | 10-20 pips | Very High |
| EUR/PLN | 3-8 pips | Medium |

### Price Format
```
EUR/USD: 1.0850 (1 pip = 0.0001)
USD/JPY: 149.50 (1 pip = 0.01)
```

### Configuration
```python
# In forex_trader.py
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]
TIMEFRAME = "5m"  # 5-minute candles
SL_PIPS = 20      # Stop loss in pips
TP_PIPS = 40      # Take profit in pips
```

---

## Commodities

### Overview
- **Types**: Energy, Metals, Agriculture
- **Exchanges**: CME, NYMEX, ICE
- **Trading Hours**: Varies by commodity
- **Min Size**: Contract-based (varies)

### Energy Commodities
| Commodity | Symbol | Tick Size | Trading Hours (CT) |
|-----------|--------|-----------|-------------------|
| Crude Oil (WTI) | CL | $0.01 | 17:00-16:00 |
| Natural Gas | NG | $0.001 | 17:00-16:00 |
| Heating Oil | HO | $0.0001 | 17:00-16:00 |

### Precious Metals
| Commodity | Symbol | Tick Size | Trading Hours (CT) |
|-----------|--------|-----------|-------------------|
| Gold | GC | $0.10 | 17:00-16:00 |
| Silver | SI | $0.005 | 17:00-16:00 |
| Platinum | PL | $0.10 | 17:00-16:00 |
| Palladium | PA | $0.05 | 17:00-16:00 |

### Agricultural
| Commodity | Symbol | Tick Size |
|-----------|--------|-----------|
| Corn | ZC | $0.0025/bu |
| Wheat | ZW | $0.0025/bu |
| Soybeans | ZS | $0.0025/bu |
| Coffee | KC | $0.0005/lb |

### Configuration
```python
# In commodity_trader.py
COMMODITIES = ["CL", "GC", "SI"]  # Crude, Gold, Silver
TIMEFRAME = "15m"
POSITION_SIZE = 1  # 1 contract
```

---

## Stocks/Equities

### Overview
- **Market**: NYSE, NASDAQ, etc.
- **Trading Hours**: 9:30-16:00 ET
- **Fractional Shares**: Yes (for most brokers)
- **Min Size**: 1 share or $1 fractional

### Popular Categories
1. **Tech Giants**: AAPL, MSFT, GOOGL, AMZN, NVDA
2. **ETFs**: SPY, QQQ, IWM, DIA
3. **Meme Stocks**: GME, AMC, BBBY
4. **Crypto Proxies**: COIN, MARA, RIOT

### Configuration
```python
# In stock_trader.py
TICKERS = ["SPY", "QQQ", "NVDA", "AAPL"]
TIMEFRAME = "5m"
POSITION_VALUE = 1000  # $1000 per position
```

---

## Cross-Market Strategies

### Multi-Asset Momentum
```python
# Trade across correlated assets
STRATEGY = {
    "crypto": ["BTC", "ETH"],  # Crypto momentum
    "forex": ["EUR/USD"],       # USD weakness
    "commodities": ["GC"],      # Gold as safe haven
}
```

### Risk Parity
```python
# Allocate capital based on volatility
ALLOCATIONS = {
    "crypto": 0.40,  # 40% (high vol)
    "forex": 0.30,   # 30% (medium vol)
    "commodities": 0.20,  # 20% (medium vol)
    "stocks": 0.10,  # 10% (lower vol)
}
```

### Market Regime Detection
```python
# Adapt strategy based on market conditions
def get_regime():
    btc_vol = get_volatility("BTC")
    vix = get_vix()
    
    if vix > 30:
        return "HIGH_VOLATILITY"  # Reduce size, wider stops
    elif btc_vol > 0.03:
        return "CRYPTO_BULL"  # Long crypto
    else:
        return "NORMAL"  # Standard trading
```

---

## Risk Management by Market

### Crypto
- Max position size: 5% of capital
- Stop loss: 2-3% (50-60% with leverage)
- Take profit: 5-10% (100-200% with leverage)

### Forex
- Max position size: 2% of capital
- Stop loss: 20-50 pips
- Take profit: 40-100 pips

### Commodities
- Max position size: 1 contract per $10k
- Stop loss: ATR-based
- Take profit: 2x stop loss

### Stocks
- Max position size: 10% per stock
- Stop loss: 3-5%
- Take profit: 6-10%

---

## Data Sources

| Market | Primary Source | Fallback |
|--------|---------------|----------|
| Crypto | Hyperliquid API | Binance, Coinbase |
| Polymarket | Gamma API | Manual scraping |
| Forex | OANDA, FXCM | Yahoo Finance |
| Commodities | CME, Yahoo Finance | Investing.com |
| Stocks | Alpaca, Yahoo Finance | IEX Cloud |

---

## Quick Start Examples

### 1. Crypto Sniping
```bash
cd agent/paper_trading
python sniper_launcher.py  # W token momentum sniper
```

### 2. Polymarket Trading
```bash
cd agent/paper_trading
python polymarket_v1.py  # Auto-trades prediction markets
```

### 3. Forex Scalping
```bash
cd agent/paper_trading
python forex_trader.py  # EUR/USD scalper
```

### 4. Multi-Market Portfolio
```bash
cd agent/paper_trading
python portfolio_manager.py  # Diversified across all markets
```

---

## Performance Benchmarks

| Market | Avg Trades/Day | Avg PnL/Trade | Win Rate | Sharpe |
|--------|---------------|---------------|----------|--------|
| Crypto (Hyperliquid) | 50-100 | 0.5-2.0% | 45-55% | 1.2-1.8 |
| Polymarket | 5-10 | 2-5% | 60-70% | 1.5-2.5 |
| Forex | 20-40 | 0.3-1.0% | 50-60% | 1.0-1.5 |
| Commodities | 10-20 | 0.5-1.5% | 45-55% | 1.1-1.6 |
| Stocks | 10-30 | 0.5-2.0% | 50-60% | 1.2-1.8 |

---

*Last updated: June 21, 2026*
