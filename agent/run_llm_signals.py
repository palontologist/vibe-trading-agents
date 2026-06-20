"""Generate LLM perp signals for BTC, ETH, SOL on Hyperliquid."""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from backtest.loaders.ccxt_loader import DataLoader as CCXTLoader
from paper_trading.llm_signal_engine import LLMSignalEngine
from paper_trading.hyperliquid_executor import HyperliquidExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
HYPER_COINS = {"BTC-USDT": "BTC", "ETH-USDT": "ETH", "SOL-USDT": "SOL"}


def main():
    import os
    from dotenv import load_dotenv

    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY", "")
    if not private_key:
        print("Error: PRIVATE_KEY not set in .env")
        sys.exit(1)

    # 1. Fetch recent OHLCV data
    loader = CCXTLoader()
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)

    print("\n=== Fetching OHLCV data for", SYMBOLS, "===")
    data_map = loader.fetch(
        SYMBOLS,
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        interval="1H",
    )

    if not data_map:
        print("Error: No data fetched")
        sys.exit(1)

    for sym, df in data_map.items():
        print(f"  {sym}: {len(df)} bars, last close = {df['close'].iloc[-1]:.2f}")

    # 2. Generate LLM signals
    print("\n=== Generating LLM signals ===")
    engine = LLMSignalEngine(
        min_confidence=60,
        max_risk_score=60,
    )

    signals = engine.generate_signals(
        data_map=data_map,
        equity=23.74,
        positions={},
    )

    print(json.dumps(signals, indent=2))

    # 3. Filter actionable signals
    actionable = {}
    for sym, sig in signals.get("signals", {}).items():
        confidence = sig.get("confidence", 0)
        risk = sig.get("risk_score", 100)
        action = sig.get("action", "CLOSE")
        weight = sig.get("target_weight", 0)

        if confidence >= 60 and risk <= 60 and action != "CLOSE" and abs(weight) > 0:
            actionable[sym] = sig

    print(f"\n=== Actionable signals: {len(actionable)} ===")
    for sym, sig in actionable.items():
        print(
            f"  {sym}: {sig['action']} (conf={sig['confidence']}, weight={sig['target_weight']:.3f}, risk={sig['risk_score']})"
        )
        print(f"    Reason: {sig.get('reasoning', 'N/A')}")

    if not actionable:
        print("\nNo actionable signals from LLM. Trying technical fallback...")
        fallback = engine._fallback_tech_signals(data_map)
        for sym, sig in fallback.get("signals", {}).items():
            if sig.get("action") != "CLOSE":
                actionable[sym] = sig
                print(f"  {sym}: {sig['action']} (conf={sig['confidence']}, weight={sig['target_weight']:.3f})")
                print(f"    Reason: {sig.get('reasoning', 'N/A')}")

    # 4. Save signals for backtest
    signal_file = Path("./paper_runs/llm_signals.json")
    with open(signal_file, "w") as f:
        json.dump(signals, f, indent=2, default=str)
    print(f"\nSignals saved to {signal_file}")

    # 5. Backtest signals
    print("\n=== Running backtest with LLM signals ===")
    backtest(data_map, signals)

    # 6. Execute on Hyperliquid
    if actionable:
        print("\n=== Opening positions on Hyperliquid ===")
        execute_signals(actionable, private_key)


def backtest(data_map: Dict[str, pd.DataFrame], signals: Dict[str, Any]):
    """Simple forward backtest: apply signals at each bar and track equity."""
    signal_map = signals.get("signals", {})

    # Build a unified date index
    all_dates = set()
    for df in data_map.values():
        all_dates.update(df.index)
    dates = pd.DatetimeIndex(sorted(all_dates))

    # Close prices matrix
    close = pd.DataFrame(index=dates)
    for sym, df in data_map.items():
        close[sym] = df["close"].reindex(dates).ffill()

    # Backtest parameters
    initial_equity = 1000.0  # Simulated $1000 for backtest
    equity = initial_equity
    equity_curve = []
    trades = []

    # Apply the LLM signal as a static position (rebalance daily)
    for sym, sig in signal_map.items():
        if sym not in close.columns:
            continue
        weight = sig.get("target_weight", 0)
        if abs(weight) < 0.01:
            continue

        direction = 1 if weight > 0 else -1
        leverage = 3.0

        # Simulate: enter at signal time, hold for N bars
        entry_idx = len(dates) - 1  # Last bar = signal time
        if entry_idx < 0:
            continue

        entry_price = close[sym].iloc[entry_idx]
        position_value = abs(weight) * equity * leverage
        size = position_value / entry_price

        # Walk forward from entry
        for i in range(entry_idx, min(entry_idx + 50, len(dates))):
            current_price = close[sym].iloc[i]
            if pd.isna(current_price):
                continue
            pnl = direction * size * (current_price - entry_price)
            fee = size * entry_price * 0.0005  # taker fee
            net_pnl = pnl - fee

            # Stop loss at -5%, take profit at +10%
            pnl_pct = pnl / position_value * 100 if position_value > 0 else 0
            if abs(pnl_pct) > 5:
                trades.append(
                    {
                        "symbol": sym,
                        "direction": "LONG" if direction > 0 else "SHORT",
                        "entry": entry_price,
                        "exit": current_price,
                        "pnl": net_pnl,
                        "pnl_pct": pnl_pct,
                        "bars_held": i - entry_idx,
                    }
                )
                break

        else:
            # Exit at end
            exit_price = close[sym].iloc[min(entry_idx + 49, len(dates) - 1)]
            pnl = direction * size * (exit_price - entry_price)
            fee = size * entry_price * 0.0005
            net_pnl = pnl - fee
            trades.append(
                {
                    "symbol": sym,
                    "direction": "LONG" if direction > 0 else "SHORT",
                    "entry": entry_price,
                    "exit": exit_price,
                    "pnl": net_pnl,
                    "pnl_pct": pnl / (abs(weight) * equity * leverage) * 100,
                    "bars_held": min(49, len(dates) - 1 - entry_idx),
                }
            )

    # Print backtest results
    print(f"\n  Initial equity: ${initial_equity:.2f}")
    print(f"  Trades: {len(trades)}")
    for t in trades:
        emoji = "+" if t["pnl"] > 0 else "-"
        print(
            f"    {t['symbol']} {t['direction']}: {t['entry']:.2f} -> {t['exit']:.2f} | PnL: ${t['pnl']:.2f} ({t['pnl_pct']:+.2f}%) | {t['bars_held']} bars"
        )

    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100 if trades else 0
    print(f"\n  Total PnL: ${total_pnl:.2f}")
    print(f"  Win rate: {win_rate:.1f}%")

    # Save backtest trades
    bt_file = Path("./paper_runs/backtest_trades.json")
    with open(bt_file, "w") as f:
        json.dump(trades, f, indent=2, default=str)
    print(f"  Backtest saved to {bt_file}")


def execute_signals(actionable: Dict[str, Any], private_key: str):
    """Execute signals on Hyperliquid."""
    from dotenv import load_dotenv

    load_dotenv()

    ex = HyperliquidExecutor(private_key, run_dir="./paper_runs")
    if not ex.is_ready:
        print("Error: Hyperliquid executor not ready")
        return

    balance = ex.get_balance()
    print(f"  Account: {ex._exchange_address}")
    print(f"  Balance: ${balance['total']:.2f} (free: ${balance['free']:.2f})")

    # Check existing positions
    existing = ex.get_all_positions()
    if existing:
        print(f"\n  Existing positions ({len(existing)}):")
        for p in existing:
            print(
                f"    {p['coin']}: {p['side']} {p['size']} @ ${p['entry_price']:.2f} (PnL: ${p['unrealized_pnl']:.2f})"
            )

    free = balance["free"]
    if free < 5:
        print(f"\n  Insufficient balance (${free:.2f}) to open positions")
        return

    # Size positions conservatively: 25% of balance per position
    num_signals = len(actionable)
    size_per_signal = free / num_signals * 0.5  # Use 50% of allocated per signal
    leverage = 3

    for sym, sig in actionable.items():
        coin = HYPER_COINS.get(sym, sym.replace("-USDT", ""))
        action = sig.get("action", "CLOSE")
        weight = sig.get("target_weight", 0)

        direction = "Buy" if (action == "LONG" or weight > 0) else "Sell"
        pos_size = min(size_per_signal, free * 0.4)

        price = ex.get_price(coin)
        if not price:
            print(f"  Skipping {coin}: no price available")
            continue

        print(f"\n  Opening {coin}: {direction} ${pos_size:.2f} @ ${price:.2f} ({leverage}x)")
        result = ex.open_position(
            coin=coin,
            direction=direction,
            size_usd=pos_size,
            price=price,
            leverage=leverage,
        )

        if result and result.get("success"):
            print(f"    OK: {result.get('status', 'filled')} @ ${result.get('price', price):.2f}")
        else:
            print(f"    FAILED: {result.get('error', 'unknown')}")

    # Show final state
    print("\n  === Final positions ===")
    final_positions = ex.get_all_positions()
    final_balance = ex.get_balance()
    print(f"  Balance: ${final_balance['total']:.2f}")
    for p in final_positions:
        print(f"    {p['coin']}: {p['side']} {p['size']} @ ${p['entry_price']:.2f} (PnL: ${p['unrealized_pnl']:.2f})")


if __name__ == "__main__":
    main()
