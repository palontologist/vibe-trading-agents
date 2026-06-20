"""Growth Framework: $10 -> $10,000 in 10 Days.

Multi-signal confluence engine combining:
  - Sentiment-driven contrarian (Fear & Greed extremes)
  - Momentum (MA crossover + RSI confirmation)
  - Breakout (Bollinger squeeze + volume surge)
  - Rotation (weakest performer out, high-beta in)
  - Factor alpha (cross-sectional ranking)

Designed for maximum aggression: 10x leverage, 60-80% position sizing,
5+ concurrent positions, multi-asset (crypto + forex + commodities).
"""

from paper_trading.growth.strategy import GrowthStrategy
from paper_trading.growth.portfolio import GrowthPortfolio
from paper_trading.growth.risk import GrowthRiskManager
from paper_trading.growth.orchestrator import GrowthOrchestrator

__all__ = [
    "GrowthStrategy",
    "GrowthPortfolio",
    "GrowthRiskManager",
    "GrowthOrchestrator",
]
