"""CEX perpetual swap executor via ccxt.

Supports Binance, Bybit, OKX, Gate, Bitget USDT perpetual futures.
Unified interface — same code works across all exchanges.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CexPerpExecutor:
    """CEX perp trading executor using ccxt unified API."""

    SUPPORTED_EXCHANGES = ["binance", "bybit", "okx", "gate", "bitget"]

    def __init__(
        self,
        exchange_name: str,
        api_key: str,
        secret: str,
        password: str = "",
        run_dir: str = "./paper_runs",
        sandbox: bool = False,
    ):
        self.exchange_name = exchange_name.lower()
        self.api_key = api_key
        self.secret = secret
        self.password = password
        self.run_dir = Path(run_dir)
        self.sandbox = sandbox
        self.is_ready = False
        self._exchange = None

        if self.exchange_name not in self.SUPPORTED_EXCHANGES:
            raise ValueError(f"Unsupported exchange: {self.exchange_name}. Supported: {self.SUPPORTED_EXCHANGES}")

        self._init_exchange()

    def _init_exchange(self):
        """Initialize ccxt exchange instance."""
        import ccxt

        try:
            ExchangeClass = getattr(ccxt, self.exchange_name)

            config = {
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }

            if self.sandbox:
                config["sandbox"] = True

            if self.exchange_name == "binance":
                config["options"]["defaultSubType"] = "linear"

            self._exchange = ExchangeClass(
                {
                    "apiKey": self.api_key,
                    "secret": self.secret,
                    **config,
                }
            )

            if self.password:
                self._exchange.options["password"] = self.password

            # Test connection
            markets = self._exchange.load_markets()
            logger.info(
                "CEX executor ready | %s | %d markets loaded",
                self.exchange_name.upper(),
                len(markets),
            )
            self.is_ready = True

        except Exception as exc:
            logger.error("Failed to init CEX executor: %s", exc)
            self.is_ready = False

    def get_balance(self) -> Dict[str, float]:
        """Get USDT balance."""
        try:
            balance = self._exchange.fetch_balance()
            usdt = balance.get("total", {}).get("USDT", 0)
            usdt_free = balance.get("free", {}).get("USDT", 0)
            return {"total": usdt, "free": usdt_free}
        except Exception as exc:
            logger.warning("Failed to get balance: %s", exc)
            return {"total": 0, "free": 0}

    def get_price(self, symbol: str) -> Optional[float]:
        """Get current mark price."""
        try:
            ticker = self._exchange.fetch_ticker(symbol)
            return ticker.get("last") or ticker.get("mark")
        except Exception as exc:
            logger.warning("Failed to get price for %s: %s", symbol, exc)
            return None

    def open_position(
        self,
        symbol: str,
        direction: str,
        size_usd: float,
        price: float,
        leverage: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Open a perpetual position.

        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            direction: 'buy' for long, 'sell' for short
            size_usd: Position size in USDT
            price: Entry price
            leverage: Leverage multiplier
        """
        try:
            # Set leverage
            self._exchange.set_leverage(leverage, symbol)

            # Calculate amount
            amount = size_usd / price

            # Place market order
            order = self._exchange.create_market_order(symbol, direction, amount)

            result = {
                "success": True,
                "order_id": order["id"],
                "symbol": symbol,
                "side": direction,
                "amount": order.get("amount", amount),
                "price": order.get("average", price),
                "cost": order.get("cost", size_usd),
                "fee": order.get("fee", {}).get("cost", 0),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            self._log_trade(result)

            logger.info(
                "CEX %s: %s %s %.2f USDT @ %.2f | order %s",
                self.exchange_name.upper(),
                symbol,
                direction,
                size_usd,
                price,
                order["id"],
            )

            return result

        except Exception as exc:
            logger.error("CEX open position failed: %s", exc)
            return {"success": False, "error": str(exc)[:200]}

    def close_position(
        self,
        symbol: str,
        direction: str,
        size_usd: float,
        price: float,
    ) -> Optional[Dict[str, Any]]:
        """Close a perpetual position."""
        try:
            # Place market order in opposite direction
            close_side = "sell" if direction == "buy" else "buy"
            amount = size_usd / price

            order = self._exchange.create_market_order(symbol, close_side, amount)

            result = {
                "success": True,
                "order_id": order["id"],
                "symbol": symbol,
                "side": close_side,
                "amount": order.get("amount", amount),
                "price": order.get("average", price),
                "cost": order.get("cost", size_usd),
                "fee": order.get("fee", {}).get("cost", 0),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            self._log_trade(result)

            logger.info(
                "CEX %s: closed %s @ %.2f | order %s",
                self.exchange_name.upper(),
                symbol,
                price,
                order["id"],
            )

            return result

        except Exception as exc:
            logger.error("CEX close position failed: %s", exc)
            return {"success": False, "error": str(exc)[:200]}

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current position for a symbol."""
        try:
            positions = self._exchange.fetch_positions([symbol])
            if positions:
                pos = positions[0]
                return {
                    "symbol": pos["symbol"],
                    "side": pos.get("side"),
                    "size": pos.get("contracts", 0),
                    "entry_price": pos.get("entryPrice", 0),
                    "mark_price": pos.get("markPrice", 0),
                    "unrealized_pnl": pos.get("unrealizedPnl", 0),
                    "leverage": pos.get("leverage", 0),
                }
            return None
        except Exception as exc:
            logger.warning("Failed to get position for %s: %s", symbol, exc)
            return None

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions."""
        try:
            positions = self._exchange.fetch_positions()
            return [
                {
                    "symbol": p["symbol"],
                    "side": p.get("side"),
                    "size": p.get("contracts", 0),
                    "entry_price": p.get("entryPrice", 0),
                    "mark_price": p.get("markPrice", 0),
                    "unrealized_pnl": p.get("unrealizedPnl", 0),
                }
                for p in positions
                if p.get("contracts", 0) > 0
            ]
        except Exception as exc:
            logger.warning("Failed to get positions: %s", exc)
            return []

    def set_sl_tp(
        self,
        symbol: str,
        stop_price: float,
        take_profit_price: float,
    ) -> Optional[Dict[str, Any]]:
        """Set stop loss and take profit for a position."""
        try:
            result = {}

            # Create stop loss order
            if stop_price:
                sl_order = self._exchange.create_order(
                    symbol,
                    "stop_market",
                    "sell" if self._get_position_side(symbol) == "long" else "buy",
                    0,
                    0,
                    {"stopPrice": stop_price, "reduceOnly": True},
                )
                result["sl_order_id"] = sl_order["id"]

            # Create take profit order
            if take_profit_price:
                tp_order = self._exchange.create_order(
                    symbol,
                    "take_profit_market",
                    "sell" if self._get_position_side(symbol) == "long" else "buy",
                    0,
                    0,
                    {"stopPrice": take_profit_price, "reduceOnly": True},
                )
                result["tp_order_id"] = tp_order["id"]

            return result

        except Exception as exc:
            logger.warning("Failed to set SL/TP: %s", exc)
            return None

    def _get_position_side(self, symbol: str) -> Optional[str]:
        """Get the side of a position."""
        pos = self.get_position(symbol)
        return pos.get("side") if pos else None

    def get_supported_symbols(self) -> List[str]:
        """Get list of supported perp symbols."""
        if not self._exchange or not self._exchange.markets:
            return []
        return [s for s, m in self._exchange.markets.items() if m.get("linear") and m.get("swap")]

    def _log_trade(self, result: Dict[str, Any]):
        """Log trade to tx_log.jsonl."""
        tx_log_path = self.run_dir / "tx_log.jsonl"
        record = {"type": self.exchange_name, **result}
        with open(tx_log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
