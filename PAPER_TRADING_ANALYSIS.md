# Paper Trading Analysis — June 20, 2026 (18:30 UTC)

## Market Snapshot

| Indicator | Value | Interpretation |
|-----------|-------|----------------|
| **Fear & Greed Index** | 23 | Extreme Fear — contrarian buy signal |
| **BTC** | $64,099.50 | Down from $79K (May 16) |
| **ETH** | $1,740.35 | Weak, -1.22% intraday |
| **SOL** | $72.02 | +2.42% bounce (high-beta) |
| **Gold (XAU)** | $4,160.21 | Safe haven bid |
| **Silver (XAG)** | $65.13 | Tracking gold |
| **Natural Gas** | $3.255 | Range-bound |

---

## 1. Growth Framework ($10 → $10K Target)

**Status**: Day 1 of 10 — positions just opened

| Metric | Value |
|--------|-------|
| Equity | $9.99 |
| Cash | $8.43 |
| Target | $10,000 |
| Progress | -0.0001% |
| Trades | 2 |
| Max Drawdown | 0.10% |

### Open Positions

| Asset | Side | Entry | Current | PnL | Age |
|-------|------|-------|---------|-----|-----|
| XAG | LONG | $65.14 | $65.13 | -$0.01 (-0.02%) | 0.1 min |
| NATGAS | LONG | $3.26 | $3.26 | -$0.03 (-0.05%) | 0.1 min |

**Signal Reasoning**: F&G=23 Extreme Fear contrarian + MA alignment + Factor ranking

**Assessment**: Positions just opened, within normal spread noise. Signal quality is moderate — F&G contrarian is valid but commodity markets have low volatility. Needs more time to develop.

---

## 2. Aggressive Forex ($10, 10x Leverage)

**Status**: 1 position open, 55 ticks running

| Metric | Value |
|--------|-------|
| Equity | $9.99 |
| Open Positions | 1 |
| Trades Today | 1 |

### Open Positions

| Pair | Side | Entry | Current | Notional | PnL |
|------|------|-------|---------|----------|-----|
| USD/MXN | LONG | 17.337 | 17.337 | $60 | $0.00 |

**Assessment**: USD/MXN is flat. The ECB daily rates update once per day, and simulated intraday volatility is minimal (~0.007% per tick). TP at 0.4% and SL at 0.25% are unlikely to trigger quickly. The position is essentially dormant.

---

## 3. Commodity Paper ($10, 3x Leverage)

**Status**: 5 positions, all opened on tick 1

| Metric | Value |
|--------|-------|
| Equity | $10.00 |
| Open Positions | 5 |
| Unrealized PnL | -$0.002 |

### Open Positions

| Asset | Side | Entry | Current | PnL | Signal |
|-------|------|-------|---------|-----|--------|
| XAU | LONG | $4,153 | $4,160 | +$0.004 (+0.17%) | RSI oversold 87% conf |
| XAG | SHORT | $64.82 | $65.13 | -$0.012 (-0.48%) | Below MA20 |
| NATGAS | SHORT | $3.25 | $3.26 | -$0.001 (-0.03%) | RSI bullish |
| PAXG | LONG | $4,147 | $4,158 | +$0.006 (+0.25%) | RSI bearish |
| SPX | LONG | $0.36 | $0.36 | $0.00 | RSI bearish |

**Assessment**: Mixed. Gold positions (XAU, PAXG) are profitable with safe-haven bid. Silver SHORT is losing as XAG rallies with gold. Natgas SHORT is flat. All 5 positions opened simultaneously on tick 1 — no staggering. 100% of equity deployed across 5 positions.

**Best Trade**: PAXG LONG (+0.25%) — gold proxy benefiting from F&G=23 fear bid
**Worst Trade**: XAG SHORT (-0.48%) — silver shorted into gold rally

---

## 4. Original Crypto Paper ($10K, 3x Leverage)

**Status**: Closed — all positions exited May 16

| Metric | Value |
|--------|-------|
| Equity | $9,961.54 |
| Total PnL | -$25.86 |
| Trades | 3 (all closed) |
| Win Rate | 0% |

**Historical Context**: All 3 SHORT positions (BTC, ETH, SOL) entered simultaneously during NEUTRAL→BEARISH regime. Risk assessor had exposure calculation bug ($5.5M vs actual ~$8K). All closed at loss within 1-2 minutes.

**Status**: No active positions. System idle.

---

## 5. Live Hyperliquid (Real Money)

**Status**: Wallet depleted

| Metric | Value |
|--------|-------|
| Total Orders | 74 |
| Total Volume | $420.88 |
| Avg Trade Size | $9.35 |
| Assets | BTC, ETH, SOL, WIF, JUP, RENDER, ENA |
| Wallet Balance | $0.003 |
| Short Bias | 98% (44/45 opens = sells) |

**Assessment**: Heavy short bias during a period that saw BTC drop from $79K to $64K. The short bias was directionally correct but positions were too small ($9.35 avg) to generate meaningful returns. Wallet is effectively empty — no capital to deploy.

---

## Cross-Portfolio Analysis

### Position Correlation Risk
- **Gold exposure**: XAU LONG (growth) + PAXG LONG (commodity) = 2 correlated gold positions
- **Silver exposure**: XAG LONG (growth) + XAG SHORT (commodity) = **HEDGED** (opposite directions!)
- **Natgas exposure**: NATGAS LONG (growth) + NATGAS SHORT (commodity) = **HEDGED** (opposite directions!)

This is actually a problem — the growth framework and commodity paper trader have **opposite positions** on XAG and NATGAS, creating a near-zero net exposure on those assets.

### Signal Quality Assessment

| Signal Type | Quality | Notes |
|-------------|---------|-------|
| F&G Contrarian | **HIGH** | F&G=23 is genuinely extreme fear, historically bullish |
| Momentum | **MEDIUM** | Mixed — some assets trending, some ranging |
| Breakout | **LOW** | No clear squeeze/breakout patterns detected |
| Rotation | **N/A** | No open positions to rotate yet |
| Factor | **MEDIUM** | Cross-sectional ranking showing commodity preference |

### Risk Status

| System | Daily Loss Limit | Drawdown Limit | Status |
|--------|------------------|----------------|--------|
| Growth Framework | 20.0% remaining | 30.0% remaining | OK |
| Aggressive Forex | N/A | N/A | OK |
| Commodity Paper | N/A | N/A | OK |

---

## Key Observations

1. **Growth framework just started** — positions are 0.1 minutes old, too early to evaluate
2. **Commodity paper has hedged positions** — XAG LONG vs SHORT and NATGAS LONG vs SHORT cancel out
3. **F&G=23 is genuinely bullish** — extreme fear historically precedes rallies
4. **Gold is the strongest trade** — XAU and PAXG positions are profitable on safe-haven bid
5. **Aggressive forex is dormant** — ECB daily rates + simulated volatility = minimal movement
6. **Original crypto paper is idle** — no positions, -0.26% historical loss
7. **Live wallet is empty** — $0.003 remaining, no more capital to deploy

## Recommendations

1. **Let growth framework run** — positions are too new to evaluate, need at least 1 hour
2. **Fix commodity hedging** — XAG SHORT conflicts with growth XAG LONG, consider closing one
3. **Add more crypto to growth** — currently only has commodity exposure, should diversify into BTC/ETH/SOL for higher volatility
4. **Reduce commodity positions** — 5 simultaneous positions on $10 is over-leveraged
5. **Monitor F&G closely** — if it drops below 15, increase position sizes; if it rises above 35, take profits
