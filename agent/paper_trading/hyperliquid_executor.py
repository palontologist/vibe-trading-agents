"""Hyperliquid perpetual swap executor.

Uses the official hyperliquid-python-sdk for spot and perp trading
on Hyperliquid's L1 DEX. No KYC, just a private key.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class HyperliquidExecutor:
    """Hyperliquid perp trading executor using the official SDK."""

    def __init__(
        self,
        private_key: str,
        run_dir: str = "./paper_runs",
        testnet: bool = False,
    ):
        self.private_key = private_key
        self.run_dir = Path(run_dir)
        self.testnet = testnet
        self.is_ready = False
        self._info = None
        self._exchange = None
        self._exchange_address = ""

        try:
            from hyperliquid.info import Info
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
            from eth_account import Account

            api_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

            self._info = Info(api_url, skip_ws=True)

            account = Account.from_key(private_key)
            self._exchange_address = account.address

            self._exchange = Exchange(account, base_url=api_url)

            user_state = self._info.user_state(self._exchange_address)
            account_value = float(user_state.get("marginSummary", {}).get("accountValue", 0))

            logger.info(
                "Hyperliquid executor ready | %s | %s | balance: $%.2f",
                "TESTNET" if testnet else "MAINNET",
                self._exchange_address,
                account_value,
            )
            self.is_ready = True

        except Exception as exc:
            logger.error("Failed to init Hyperliquid executor: %s", exc)
            self.is_ready = False

    def get_balance(self) -> Dict[str, float]:
        """Get account balance."""
        try:
            user_state = self._info.user_state(self._exchange_address)
            margin = user_state.get("marginSummary", {})
            return {
                "total": float(margin.get("accountValue", 0)),
                "free": float(user_state.get("withdrawable", 0)),
                "used": float(margin.get("totalMarginUsed", 0)),
            }
        except Exception as exc:
            logger.warning("Failed to get Hyperliquid balance: %s", exc)
            return {"total": 0, "free": 0, "used": 0}

    def get_price(self, coin: str) -> Optional[float]:
        """Get current mid price from L2 book."""
        try:
            l2 = self._info.l2_snapshot(coin)
            levels = l2.get("levels", [])
            if levels and len(levels) >= 2:
                best_bid = float(levels[0][0]["px"])
                best_ask = float(levels[1][0]["px"])
                return (best_bid + best_ask) / 2
            return None
        except Exception as exc:
            logger.warning("Failed to get price for %s: %s", coin, exc)
            return None

    def open_position(
        self,
        coin: str,
        direction: str,
        size_usd: float,
        price: float,
        leverage: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Open a perpetual position on Hyperliquid.

        Args:
            coin: Coin name (e.g., 'BTC', 'ETH', 'SOL')
            direction: 'Buy' for long, 'Sell' for short
            size_usd: Position size in USD
            price: Entry price
            leverage: Leverage multiplier
        """
        try:
            amount = size_usd / price

            # Round to Hyperliquid's szDecimals
            sz_decimals = {
                "BTC": 5,
                "ETH": 4,
                "SOL": 2,
                "DOGE": 0,
                "XRP": 0,
                "BNB": 3,
                "AVAX": 2,
                "ARB": 1,
                "OP": 1,
                "ATOM": 0,
                "APT": 2,
                "MATIC": 1,
                "FIL": 1,
                "NEAR": 1,
                "INJ": 1,
                "TIA": 1,
                "SUI": 1,
                "FET": 0,
                "WIF": 0,
                "ONDO": 0,
                "JUP": 0,
                "RENDER": 1,
                "ENA": 0,
                "W": 1,
                "STRK": 1,
                "LDO": 1,
                "STX": 1,
                "WLD": 1,
                "GOAT": 0,
                "AIXBT": 0,
                "ZEREBRO": 0,
                "SPX": 1,
                "TRUMP": 1,
                "MELANIA": 1,
                "KAITO": 0,
            }.get(coin, 0)
            amount = round(amount, sz_decimals)

            if amount <= 0:
                logger.warning("Hyperliquid: %s amount %.8f too small after rounding", coin, amount)
                return {"success": False, "error": "amount too small"}

            # Set leverage first (signature: leverage, name, is_cross)
            try:
                self._exchange.update_leverage(leverage, coin, True)
            except Exception:
                pass  # Leverage set best-effort

            order = self._exchange.market_open(
                coin,
                direction == "Buy",
                amount,
                px=price if direction == "Sell" else None,
                slippage=0.01,
            )

            logger.info("Hyperliquid order response for %s: %s", coin, order)

            # Check for errors in response (Hyperliquid returns status:'ok' but inner error)
            if isinstance(order, dict):
                response_data = order.get("response", {}).get("data", {})
                statuses = response_data.get("statuses", [])
                for st in statuses:
                    if isinstance(st, dict) and st.get("error"):
                        logger.error("Hyperliquid order failed for %s: %s", coin, st.get("error"))
                        return {"success": False, "error": st.get("error", str(order))}

            result = {
                "success": True,
                "status": order.get("status", "filled"),
                "coin": coin,
                "side": direction,
                "amount": amount,
                "price": order.get("avgPx", price),
                "order_type": order.get("orderType", "market"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            self._log_trade(result)

            logger.info(
                "Hyperliquid: %s %s %.2f USD @ %.2f",
                coin,
                direction,
                size_usd,
                price,
            )

            return result

        except Exception as exc:
            logger.error("Hyperliquid open position failed: %s", exc)
            return {"success": False, "error": str(exc)[:200]}

    def close_position(
        self,
        coin: str,
        size_usd: float,
        price: float,
    ) -> Optional[Dict[str, Any]]:
        """Close a perpetual position on Hyperliquid."""
        try:
            pos = self.get_position(coin)
            if pos is None:
                logger.info("Hyperliquid: %s no position to close", coin)
                return None

            # Get actual position size (abs value, direction doesn't matter for close)
            actual_size = abs(float(pos["size"]))
            sz_decimals = {
                "BTC": 5,
                "ETH": 4,
                "SOL": 2,
                "DOGE": 0,
                "ARB": 1,
                "OP": 1,
                "ATOM": 0,
                "APT": 2,
                "MATIC": 1,
                "FIL": 1,
                "NEAR": 1,
                "INJ": 1,
                "TIA": 1,
                "SUI": 1,
                "FET": 0,
                "WIF": 0,
                "ONDO": 0,
            }.get(coin, 2)
            amount = round(actual_size, sz_decimals)

            order = self._exchange.market_close(
                coin,
                sz=amount,
            )

            result = {
                "success": True,
                "status": order.get("status", "filled"),
                "coin": coin,
                "amount": amount,
                "price": order.get("avgPx", price),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            self._log_trade(result)

            logger.info(
                "Hyperliquid: closed %s @ %.2f",
                coin,
                price,
            )

            return result

        except Exception as exc:
            logger.error("Hyperliquid close position failed: %s", exc)
            return {"success": False, "error": str(exc)[:200]}

    def get_position(self, coin: str) -> Optional[Dict[str, Any]]:
        """Get current position for a coin."""
        try:
            user_state = self._info.user_state(self._exchange_address)
            positions = user_state.get("assetPositions", [])
            for pos in positions:
                asset = pos.get("position", {})
                if asset.get("coin") == coin:
                    return {
                        "coin": coin,
                        "side": "long" if float(asset.get("szi", 0)) > 0 else "short",
                        "size": float(asset.get("szi", 0)),
                        "entry_price": float(asset.get("entryPx", 0)),
                        "mark_price": float(asset.get("entryPx", 0)),
                        "unrealized_pnl": float(asset.get(".pnl", 0)),
                        "leverage": asset.get("leverage", {}).get("value", 0),
                    }
            return None
        except Exception as exc:
            logger.warning("Failed to get position for %s: %s", coin, exc)
            return None

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions."""
        try:
            user_state = self._info.user_state(self._exchange_address)
            positions = user_state.get("assetPositions", [])
            result = []
            for pos in positions:
                asset = pos.get("position", {})
                szi = float(asset.get("szi", 0))
                if abs(szi) > 0:
                    result.append(
                        {
                            "coin": asset.get("coin"),
                            "side": "long" if szi > 0 else "short",
                            "size": abs(szi),
                            "entry_price": float(asset.get("entryPx", 0)),
                            "unrealized_pnl": float(asset.get("pnl", 0)),
                        }
                    )
            return result
        except Exception as exc:
            logger.warning("Failed to get positions: %s", exc)
            return []

    def get_supported_coins(self) -> List[str]:
        """Get list of supported coins."""
        try:
            meta = self._info.meta()
            universe = meta.get("universe", [])
            return [m.get("name") for m in universe if not m.get("isDelisted", False)]
        except Exception as exc:
            logger.warning("Failed to get coins: %s", exc)
            return []

    def set_leverage(self, coin: str, leverage: int) -> bool:
        """Set leverage for a coin."""
        try:
            result = self._exchange.update_leverage(leverage, coin, True)
            logger.info("Hyperliquid: set %s leverage to %dx", coin, leverage)
            return True
        except Exception as exc:
            # Hyperliquid 422 on leverage is cosmetic — trade still executes
            logger.debug("Leverage set skipped for %s (non-fatal): %s", coin, exc)
            return False

    def _log_trade(self, result: Dict[str, Any]):
        """Log trade to tx_log.jsonl."""
        tx_log_path = self.run_dir / "tx_log.jsonl"
        record = {"type": "hyperliquid", **result}
        with open(tx_log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
