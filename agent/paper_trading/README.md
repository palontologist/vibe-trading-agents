# Paper Trading Module

Autonomous paper trading system for Vibe-Trading that simulates perp trades with real-time market data while tracking your Base wallet.

## Features

- **Paper Trading Engine**: Simulates real perp trades with slippage, fees, and leverage
- **Wallet Connector**: Read-only Base wallet integration (no private keys needed)
- **Risk Manager**: Position limits, daily loss limits, leverage caps
- **Autonomous Loop**: Continuous trading with configurable intervals
- **Real-time Data**: Fetches live prices via CCXT (100+ exchanges)
- **Trade Logging**: JSONL logs of all simulated trades
- **Signal Integration**: Plug in your own strategy functions

## Architecture

```
paper_trading/
├── __init__.py           # Package exports (lazy loading)
├── wallet_connector.py   # Read-only Base wallet reader
├── paper_engine.py       # Paper trading engine (extends CryptoEngine)
├── autonomous_trader.py  # Main trading loop
├── risk_manager.py       # Risk controls
├── runner.py             # CLI entrypoint
└── README.md             # This file
```

## Quick Start

### 1. Install Dependencies

```bash
pip install requests ccxt pandas numpy
```

### 2. Run Paper Trading

**Single cycle (test mode):**
```bash
export PYTHONPATH=/path/to/Vibe-Trading/agent:$PYTHONPATH
python -m paper_trading.runner \
  --once \
  --wallet 0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920 \
  --symbols BTC-USDT,ETH-USDT \
  --leverage 3.0 \
  --initial-cash 10000
```

**Continuous trading:**
```bash
python -m paper_trading.runner \
  --wallet 0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920 \
  --symbols BTC-USDT,ETH-USDT \
  --interval 5m \
  --interval-seconds 300 \
  --leverage 3.0
```

### 3. Check Wallet Status

```bash
python -m paper_trading.runner --status --wallet 0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920
```

## Configuration

Create a `paper_config.json`:

```json
{
  "wallet_address": "0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920",
  "symbols": ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
  "interval": "5m",
  "initial_cash": 10000,
  "leverage": 3.0,
  "risk_config": {
    "max_position_size": 0.3,
    "max_total_exposure": 0.8,
    "max_leverage": 5.0,
    "daily_loss_limit": 5.0,
    "max_trades_per_day": 20,
    "min_trade_size": 10.0
  },
  "run_dir": "./paper_runs"
}
```

Run with config:
```bash
python -m paper_trading.runner --config paper_config.json
```

## Custom Signals

The default signal function is a simple momentum strategy (buy above 20-period MA, sell below). To use your own strategy:

```python
from paper_trading.autonomous_trader import AutonomousPaperTrader
import pandas as pd

def my_strategy(data_map):
    """Custom signal function.
    
    Args:
        data_map: Dict[str, pd.DataFrame] - symbol -> OHLCV data
    
    Returns:
        Dict[str, float] - symbol -> target weight (-1.0 to 1.0)
    """
    signals = {}
    for symbol, df in data_map.items():
        # Your strategy logic here
        rsi = compute_rsi(df)
        if rsi < 30:
            signals[symbol] = 0.5  # 50% long
        elif rsi > 70:
            signals[symbol] = -0.5  # 50% short
        else:
            signals[symbol] = 0.0
    return signals

config = {
    "wallet_address": "0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920",
    "symbols": ["BTC-USDT"],
    "signal_fn": my_strategy,
    "initial_cash": 10000,
    "leverage": 3.0,
}

trader = AutonomousPaperTrader(config)
trader.run_once()  # Test
```

## Output Files

All files are written to `run_dir` (default: `./paper_runs`):

- **`trades.jsonl`**: Every simulated trade with price, size, PnL, fees
- **`equity.jsonl`**: Equity curve snapshots
- **`cycles.jsonl`**: Complete cycle results including signals and prices

Example trade log:
```json
{"timestamp": "2025-01-07T12:34:56", "symbol": "BTC-USDT", "side": "buy", "price": 43250.50, "size": 0.231, "notional": 10000, "direction": 1, "leverage": 3, "margin": 3333.33, "commission": 12.50, "action": "open", "wallet": "0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920"}
```

## Risk Management

Default risk limits (all configurable):

- **Max position size**: 30% of equity per position
- **Max total exposure**: 80% of equity across all positions
- **Max leverage**: 5x (configurable per trade)
- **Daily loss limit**: 5% of equity
- **Max trades/day**: 20 trades
- **Min trade size**: $10 USD

## Wallet Integration

The wallet connector is **read-only** - it fetches:
- Native ETH balance
- USDC balance
- WETH balance

No private keys or signing required. Uses public Base RPC endpoints.

## Safety Features

- **Paper mode only**: No real transactions executed
- **No private keys**: Wallet connector is read-only
- **Daily loss limits**: Auto-stops trading after configured loss
- **Position size limits**: Prevents oversized trades
- **Graceful shutdown**: Ctrl+C safely stops the trading loop
- **Trade confirmation logging**: Every simulated trade is logged

## Advanced Usage

### Using with Your Strategy

```python
from paper_trading.autonomous_trader import AutonomousPaperTrader
from paper_trading.wallet_connector import WalletConnector

# Connect wallet (read-only)
wallet = WalletConnector("0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920")
balances = wallet.get_all_balances()
print(f"ETH: {balances['ETH']:.4f}, USDC: {balances['USDC']:.2f}")

# Define strategy
def momentum_strategy(data):
    signals = {}
    for sym, df in data.items():
        if len(df) < 50:
            continue
        sma20 = df['close'].rolling(20).mean().iloc[-1]
        sma50 = df['close'].rolling(50).mean().iloc[-1]
        if sma20 > sma50 * 1.02:
            signals[sym] = 0.33  # 33% allocation
        elif sma20 < sma50 * 0.98:
            signals[sym] = -0.33
    return signals

# Run
config = {
    "wallet_address": "0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920",
    "symbols": ["BTC-USDT", "ETH-USDT"],
    "interval": "15m",
    "initial_cash": 5000,
    "leverage": 2.0,
    "signal_fn": momentum_strategy,
    "risk_config": {
        "max_position_size": 0.25,
        "max_total_exposure": 0.5,
        "daily_loss_limit": 3.0,
    },
}

trader = AutonomousPaperTrader(config)
# Run once for testing
result = trader.run_once()
print(f"Trades: {result['trades_executed']}")
print(f"Summary: {result['summary']}")
```

### Integration with Backtest Engine

You can backtest your strategy first, then run it in paper trading:

```python
from backtest.runner import main as backtest_main
from paper_trading.autonomous_trader import AutonomousPaperTrader

# 1. Backtest
backtest_main(Path("./my_strategy"))

# 2. Paper trade with same signals
def signal_fn(data_map):
    # Load your backtest signal engine
    # ...
    return signals

trader = AutonomousPaperTrader({
    "wallet_address": "0x1EA37E2Fb76Aa396072204C90fcEF88093CEb920",
    "symbols": ["BTC-USDT"],
    "signal_fn": signal_fn,
})
trader.run()
```

## Troubleshooting

### Import errors
Make sure PYTHONPATH includes the agent directory:
```bash
export PYTHONPATH=/path/to/Vibe-Trading/agent:$PYTHONPATH
```

### CCXT exchange errors
Set exchange via environment variable:
```bash
export CCXT_EXCHANGE=binance  # or okx, bybit, etc.
```

### Wallet connection errors
The wallet connector uses public RPC endpoints. If one fails, it auto-falls back to others. You can also set a custom RPC:
```bash
export BASE_RPC_URL=https://your-custom-rpc.com
```

## Next Steps

1. **Paper trade for 1-2 weeks** to validate your strategy
2. **Compare paper vs backtest** results to check consistency
3. **Adjust risk parameters** based on observed performance
4. **Monitor daily logs** in `paper_runs/trades.jsonl`
5. **Only after consistent paper profits**, consider live trading (not implemented in this module)

## Disclaimer

This is a **simulation/paper trading** system. No real trades are executed. The wallet connector is read-only. Always validate strategies with paper trading before considering any live trading.
