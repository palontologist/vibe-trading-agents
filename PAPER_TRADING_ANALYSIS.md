# Paper Trading Analysis — June 20, 2026 (20:55 UTC)

## Market Snapshot

| Indicator | Value | Change | Interpretation |
|-----------|-------|--------|----------------|
| **Fear & Greed Index** | 23 | — | Extreme Fear — contrarian buy signal |
| **BTC** | $63,805.50 | -$294 | Down from $64,100 earlier |
| **ETH** | $1,725.25 | -$15 | Weak, continuing decline |
| **SOL** | $71.44 | -$0.58 | Pullback from $72 bounce |
| **Gold (XAU)** | $4,153.24 | — | Holding safe-haven bid |
| **Silver (XAG)** | $65.01 | -$0.12 | Tracking gold |
| **Natural Gas** | $3.254 | -$0.001 | Range-bound |

---

## System Status

| System | Status | Ticks | Runtime |
|--------|--------|-------|---------|
| **Growth Framework** | RUNNING | 5 | 3 min |
| **Aggressive Forex** | STOPPED | 55 | 35 min |
| **Commodity Paper** | STOPPED | 7 | 7 min |
| **Original Crypto** | IDLE | 5 | — |

**Critical Issue**: Aggressive Forex and Commodity Paper were killed by system (SIGTERM). Only Growth Framework is active.

---

## 1. Growth Framework ($10 → $10K Target)

**Status**: RUNNING — Day 1 of 10

| Metric | Value |
|--------|-------|
| Equity | $9.99 |
| Cash | $8.50 |
| Target | $10,000 |
| Progress | -0.0001% |
| Trades | 2 |
| Max Drawdown | 0.10% |
| F&G | 23 |

### Open Positions

| Asset | Side | Entry | Current | PnL | Age |
|-------|------|-------|---------|-----|-----|
| XAG | LONG | $65.03 | $65.01 | -$0.002 (-0.03%) | 1.4 min |
| NATGAS | LONG | $3.26 | $3.25 | -$0.002 (-0.05%) | 1.4 min |

**Signal Reasoning**: F&G=23 Extreme Fear contrarian + MA10>MA20 + Factor ranking

**Assessment**: Positions are 1.4 minutes old, within normal spread noise. Both signals triggered at 60% confidence. The system is working correctly — fetching live prices, generating signals, and executing paper trades. Needs more time (at least 1 hour) to evaluate signal quality.

---

## 2. Aggressive Forex ($10, 10x Leverage)

**Status**: STOPPED — killed at 18:22 UTC

| Metric | Value |
|--------|-------|
| Equity | $9.99 |
| Open Positions | 1 |
| Ticks | 55 |
| Runtime | 35 min |

### Open Positions

| Pair | Side | Entry | Current | Notional | PnL |
|------|------|-------|---------|----------|-----|
| USD/MXN | LONG | 17.337 | 17.337 | $60 | $0.00 |

**Assessment**: USD/MXN is completely flat. The ECB daily rates update once per day, and simulated intraday volatility is ~0.007% per tick. At this rate, TP (0.4%) would take ~57 ticks (~30 min) and SL (0.25%) even longer. The position is essentially dormant. The system ran for 35 minutes without a single exit signal.

**Root Cause**: Frankfurter API provides daily ECB rates only. The simulated intraday volatility (geometric Brownian motion) produces price changes too small to trigger TP/SL thresholds.

---

## 3. Commodity Paper ($10, 3x Leverage)

**Status**: STOPPED — killed at 17:38 UTC

| Metric | Value |
|--------|-------|
| Equity | $10.00 |
| Open Positions | 5 |
| Unrealized PnL | -$0.006 |

### Open Positions

| Asset | Side | Entry | Current | PnL | Signal |
|-------|------|-------|---------|-----|--------|
| XAU | LONG | $4,153 | $4,153 | $0.00 | RSI oversold 87% conf |
| XAG | SHORT | $64.82 | $65.01 | -$0.007 (-0.29%) | Below MA20 |
| NATGAS | SHORT | $3.25 | $3.25 | $0.00 | RSI bullish |
| PAXG | LONG | $4,147 | $4,150 | +$0.002 (+0.06%) | RSI bearish |
| SPX | LONG | $0.36 | $0.36 | $0.00 | RSI bearish |

**Assessment**: Mixed performance. Gold long (PAXG) is slightly profitable. Silver short is losing as XAG rallies with gold. All 5 positions opened simultaneously on tick 1 — no staggering. 100% of equity deployed across 5 positions ($2 each).

**Critical Bug**: All 5 positions opened on the very first tick. No cooldown or staggered entry logic.

---

## 4. Original Crypto Paper ($10K, 3x Leverage)

**Status**: IDLE — no positions since May 16

| Metric | Value |
|--------|-------|
| Equity | $9,961.54 |
| Total PnL | -$25.86 |
| Win Rate | 0% |

**Historical**: All 3 SHORT positions (BTC, ETH, SOL) entered simultaneously, closed at loss within 1-2 minutes. Risk assessor had exposure calculation bug.

---

## 5. Live Hyperliquid (Real Money)

**Status**: Wallet depleted

| Metric | Value |
|--------|-------|
| Total Orders | 74 |
| Total Volume | $420.88 |
| Avg Trade Size | $9.35 |
| Wallet Balance | $0.003 |

---

## Cross-Portfolio Analysis

### Position Correlation Risk

| Asset | Growth | Commodity | Net Exposure |
|-------|--------|-----------|--------------|
| XAG | LONG | SHORT | **HEDGED** (cancel out) |
| NATGAS | LONG | SHORT | **HEDGED** (cancel out) |
| XAU | — | LONG | Net LONG |
| PAXG | — | LONG | Net LONG |

**Problem**: Growth framework and Commodity paper have **opposite positions** on XAG and NATGAS, creating near-zero net exposure on those assets. This defeats the purpose of having both systems.

### Signal Quality Assessment

| Signal Type | Quality | Confidence | Notes |
|-------------|---------|------------|-------|
| F&G Contrarian | **HIGH** | 60% | F&G=23 is genuinely extreme fear |
| Momentum | **MEDIUM** | 60% | MA10>MA20 on both assets |
| Breakout | **LOW** | — | No squeeze/breakout detected |
| Rotation | **N/A** | — | No positions to rotate yet |
| Factor | **MEDIUM** | — | Commodity preference in ranking |

### Risk Status

| System | Daily Loss | Drawdown | Status |
|--------|------------|----------|--------|
| Growth Framework | 0% used | 0.1% used | OK |
| Aggressive Forex | 0% used | 0.1% used | STOPPED |
| Commodity Paper | 0% used | 0% used | STOPPED |

---

## Key Observations

1. **Growth framework is the only active system** — others were killed by system
2. **Positions are too new** — 1.4 minutes old, need at least 1 hour to evaluate
3. **F&G=23 is genuinely bullish** — extreme fear historically precedes rallies
4. **Commodity hedging issue** — XAG/NATGAS positions cancel out between systems
5. **Aggressive forex is broken** — ECB daily rates + simulated vol = no movement
6. **No crypto exposure in growth** — only commodities, missing BTC/ETH/SOL volatility
7. **Commodity paper opened all positions at once** — no staggering logic

## Recommendations

1. **Let growth framework run for 1+ hour** — too early to evaluate
2. **Add crypto to growth framework** — BTC, ETH, SOL for higher volatility and faster compounding
3. **Fix commodity hedging** — close opposing positions or coordinate between systems
4. **Improve forex data** — switch from Frankfurter to a real-time API (OANDA, Alpha Vantage)
5. **Add position staggering** — don't open all positions on tick 1
6. **Monitor F&G closely** — if <15, increase sizes; if >35, take profits

## Next Steps

- [ ] Restart Aggressive Forex with real-time data source
- [ ] Restart Commodity Paper with position staggering
- [ ] Add BTC/ETH/SOL to Growth Framework asset universe
- [ ] Fix the hedging conflict between Growth and Commodity systems
- [ ] Run for 24 hours and re-evaluate
