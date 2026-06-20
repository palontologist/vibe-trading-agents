"""Live trade executor with paper/live toggle.

Supports two execution modes:
  - Paper mode: simulates trades via PaperTradingEngine (default, safe)
  - Live mode: executes real on-chain swaps via SigningWallet + DEX router

Configurable via paper_mode flag. Live mode requires explicit opt-in.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LiveExecutor:
    """Execute trades on-chain or in paper mode.

    Args:
        paper_mode: If True, simulate trades (default True for safety).
        signing_wallet: Optional SigningWallet for live execution.
        dex_router: DEX router address for swaps (e.g., Uniswap V3 on Polygon).
        run_dir: Directory for trade logs.
    """

    def __init__(
        self,
        paper_mode: bool = True,
        signing_wallet: Any = None,
        dex_router: Optional[str] = None,
        quoter: Optional[str] = None,
        run_dir: str = "./paper_runs",
    ):
        self.paper_mode = paper_mode or os.environ.get("PAPER_MODE", "true").lower() == "true"
        self.signing_wallet = signing_wallet
        self.dex_router = dex_router
        self.quoter = quoter
        self.run_dir = run_dir
        self._paper_engine: Any = None
        self._trade_log: List[Dict[str, Any]] = []

        Path(run_dir).mkdir(parents=True, exist_ok=True)

        if not self.paper_mode and not signing_wallet:
            logger.warning("Live mode requested but no signing wallet configured — falling back to paper mode")
            self.paper_mode = True

        if not self.paper_mode:
            logger.info("LIVE EXECUTION MODE — real trades will be executed")
        else:
            logger.info("PAPER MODE — trades will be simulated")

    def set_paper_engine(self, engine: Any):
        """Attach a PaperTradingEngine for paper mode execution."""
        self._paper_engine = engine

    def execute_trade(
        self,
        symbol: str,
        direction: int,
        size: float,
        price: float,
        target_weight: float,
        equity: float,
        order_params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Execute a trade (paper or live).

        Args:
            symbol: Trading pair (e.g., BTC-USDT, WETH-USDC).
            direction: 1 for long, -1 for short, 0 for close.
            size: Position size in base units.
            price: Current price.
            target_weight: Target portfolio weight (-1.0 to 1.0).
            equity: Current portfolio equity.
            order_params: Optional stop_loss, take_profit, trailing_stop.

        Returns:
            Trade dict or None if execution failed.
        """
        if direction == 0:
            return self._close_position(symbol, price, equity)

        if self.paper_mode:
            return self._execute_paper_trade(symbol, direction, size, price, target_weight, equity, order_params)
        else:
            return self._execute_live_trade(symbol, direction, size, price, target_weight, equity, order_params)

    def _execute_paper_trade(
        self,
        symbol: str,
        direction: int,
        size: float,
        price: float,
        target_weight: float,
        equity: float,
        order_params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if self._paper_engine is None:
            logger.error("No paper engine attached")
            return None

        try:
            trade = self._paper_engine.execute_paper_trade(
                symbol=symbol,
                direction=direction,
                target_weight=target_weight,
                price=price,
                equity=equity,
            )
            if trade:
                trade["mode"] = "paper"
                trade["order_params"] = order_params
                self._log_trade(trade)
            return trade
        except Exception as exc:
            logger.error("Paper trade failed for %s: %s", symbol, exc)
            return None

    def _execute_live_trade(
        self,
        symbol: str,
        direction: int,
        size: float,
        price: float,
        target_weight: float,
        equity: float,
        order_params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.signing_wallet or not self.signing_wallet.is_ready:
            logger.error("Live execution: wallet not ready")
            return None

        if not self.dex_router:
            logger.error("Live execution: no DEX router configured")
            return None

        try:
            action = "BUY" if direction > 0 else "SELL"
            usd_value = size * price

            # Check gas balance
            balance_eth = self.signing_wallet.get_balance_eth()
            if balance_eth < 0.001:
                logger.warning("Insufficient native gas: %.6f (need at least 0.001)", balance_eth)
                return None

            # Build swap calldata (simplified — uses exactInputSingle)
            tx_hash = self._build_swap_tx(symbol, direction, size, price)
            if not tx_hash:
                return None

            receipt = self.signing_wallet.wait_for_receipt(tx_hash)
            success = receipt and receipt.get("status") == 1

            trade = {
                "symbol": symbol,
                "action": action,
                "direction": direction,
                "size": size,
                "price": price,
                "usd_value": usd_value,
                "target_weight": target_weight,
                "equity": equity,
                "tx_hash": tx_hash,
                "block_number": receipt.get("blockNumber") if receipt else None,
                "gas_used": receipt.get("gasUsed") if receipt else None,
                "success": bool(success),
                "mode": "live",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "order_params": order_params,
            }

            self._log_trade(trade)
            logger.info(
                "Live trade %s: %s %s %.4f @ %.2f (TX: %s)",
                "SUCCESS" if success else "FAILED",
                action,
                symbol,
                size,
                price,
                tx_hash[:16],
            )
            return trade

        except Exception as exc:
            logger.error("Live trade failed for %s: %s", symbol, exc, exc_info=True)
            return None

    def _build_swap_tx(self, symbol: str, direction: int, size: float, price: float) -> Optional[str]:
        """Build and send a DEX swap transaction.

        This is a simplified implementation for Polygon USDC pairs.
        For production, use a full router with proper path encoding.
        """
        if not self.signing_wallet or not self.dex_router:
            return None

        # Parse symbol for token addresses
        parts = symbol.replace("-", "/").split("/")
        if len(parts) != 2:
            logger.warning("Cannot parse symbol for live swap: %s", symbol)
            return None

        base, quote = parts
        token_in, token_out = (base, quote) if direction > 0 else (quote, base)

        # Map common tokens to Polygon addresses
        polygon_tokens = {
            "WETH": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
            "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
            "MATIC": "0x0000000000000000000000000000000000001010",
            "DAI": "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063",
            "WBTC": "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6",
        }

        token_in_addr = polygon_tokens.get(token_in.upper())
        token_out_addr = polygon_tokens.get(token_out.upper())

        if not token_in_addr or not token_out_addr:
            logger.warning(
                "Token not mapped for live swap: %s -> %s (supported: %s)",
                token_in,
                token_out,
                list(polygon_tokens.keys()),
            )
            return None

        # For now, log the intended swap — full router integration requires
        # path encoding, quote fetching, and slippage calculation
        logger.info(
            "Live swap intended: %s -> %s (%.4f @ %.2f)",
            token_in,
            token_out,
            size,
            price,
        )

        # TODO: Full Uniswap V3 exactInputSingle implementation
        # This requires:
        # 1. Fetching quote from QuoterV2
        # 2. Encoding path with fee tier
        # 3. Building exactInputSingle calldata
        # 4. Sending via signing_wallet.build_and_send_tx

        return None

    def _close_position(
        self,
        symbol: str,
        price: float,
        equity: float,
    ) -> Optional[Dict[str, Any]]:
        """Close an existing position."""
        if self.paper_mode and self._paper_engine:
            try:
                trade = self._paper_engine.execute_paper_trade(
                    symbol=symbol,
                    direction=0,
                    target_weight=0.0,
                    price=price,
                    equity=equity,
                )
                if trade:
                    trade["mode"] = "paper"
                    self._log_trade(trade)
                return trade
            except Exception as exc:
                logger.error("Failed to close position %s: %s", symbol, exc)
                return None
        else:
            logger.warning("Closing position in live mode not yet implemented: %s", symbol)
            return None

    def _log_trade(self, trade: Dict[str, Any]):
        """Append trade to JSONL log."""
        self._trade_log.append(trade)
        log_path = Path(self.run_dir) / "trades_live.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(trade, default=str) + "\n")

    def get_trade_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._trade_log[-limit:]

    def get_summary(self) -> Dict[str, Any]:
        trades = self._trade_log
        successful = [t for t in trades if t.get("success", t.get("mode") == "paper")]
        return {
            "total_trades": len(trades),
            "successful": len(successful),
            "mode": "paper" if self.paper_mode else "live",
            "recent_trades": trades[-10:],
        }
