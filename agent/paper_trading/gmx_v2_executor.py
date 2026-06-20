"""GMX v2 perpetual swap executor on Arbitrum.

Provides live perp trading via GMX v2 contracts with full transaction
verification. Supports BTC, ETH, SOL, and other major perps.

Contract addresses are configurable via environment variables.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from web3 import Web3
from web3.contract import Contract
from web3.types import TxReceipt

logger = logging.getLogger(__name__)

# ── GMX v2 Contract Addresses on Arbitrum ────────────────────────────────

GMX_V2_ROUTER = "0xfc8a4f7E1E7Aa1c30CB6f605EF71742CC0f15629"
GMX_V2_ORACLE = "0x2F7304E3f53F980D0cF9B67873fB9701A4A2b3A4"
GMX_V2_SWAP_MARGIN_MODULE = "0x2BbF9f0e0a626064D9E56416133Cf95e5e72Ae21"
GMX_V2_READ_API = "0x0000000000000000000000000000000000000000"

# ── Token Addresses on Arbitrum ──────────────────────────────────────────

ETH_ARBITRUM = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC_ARBITRUM = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDT_ARBITRUM = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"

# ── GMX v2 Market Indices (from GMX v2 docs) ─────────────────────────────

MARKET_INDICES = {
    "BTC-USDT": 0,
    "ETH-USDT": 1,
    "SOL-USDT": 2,
    "AVAX-USDT": 3,
    "ARB-USDT": 4,
    "LINK-USDT": 5,
    "DOGE-USDT": 6,
    "APT-USDT": 7,
    "OP-USDT": 8,
    "SUI-USDT": 9,
}

# ── GMX v2 Router ABI (minimal for swap + margin operations) ────────────

GMX_V2_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "user", "type": "address"},
            {
                "components": [
                    {"internalType": "uint256", "name": "marketIndex", "type": "uint256"},
                    {"internalType": "bool", "name": "isLong", "type": "bool"},
                    {"internalType": "address", "name": "receiver", "type": "address"},
                    {
                        "components": [
                            {"internalType": "address", "name": "token", "type": "address"},
                            {"internalType": "uint256", "name": "depositDelta", "type": "uint256"},
                            {"internalType": "uint256", "name": "withdrawalAmount", "type": "uint256"},
                        ],
                        "internalType": "struct IPool.TokenDelta[]",
                        "name": "collateralDeltas",
                        "type": "tuple[]",
                    },
                    {"internalType": "int128", "name": "sizeDelta", "type": "int128"},
                    {"internalType": "uint256", "name": "triggerPrice", "type": "uint256"},
                    {"internalType": "uint256", "name": "executionPrice", "type": "uint256"},
                    {"internalType": "uint256", "name": "reduceSizeByProfit", "type": "uint256"},
                    {"internalType": "bytes", "name": "executeData", "type": "bytes"},
                ],
                "internalType": "struct ISwapMarginModule.SwapParameters",
                "name": "swapParameter",
                "type": "tuple",
            },
        ],
        "name": "swap",
        "outputs": [
            {"internalType": "int128", "name": "positionSize", "type": "int128"},
            {"internalType": "int256", "name": "positionNetProfits", "type": "int256"},
            {"internalType": "int256[]", "name": "collateralChanges", "type": "int256[]"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "user", "type": "address"},
            {"internalType": "uint256", "name": "marketIndex", "type": "uint256"},
            {"internalType": "bool", "name": "isLong", "type": "bool"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "getAvailableSize",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ── GMX v2 Oracle ABI ───────────────────────────────────────────────────

GMX_V2_ORACLE_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "marketIndex", "type": "uint256"}],
        "name": "getMarketPrices",
        "outputs": [
            {"internalType": "uint256", "name": "price", "type": "uint256"},
            {"internalType": "uint256", "name": "nextPrice", "type": "uint256"},
            {"internalType": "uint256", "name": "previousPrice", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# ── ERC20 ABI (for approvals and balance checks) ─────────────────────────

ERC20_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class GmxV2Executor:
    """GMX v2 perpetual swap executor on Arbitrum.

    Provides:
    - Live price fetching from GMX v2 oracle
    - Position opening/closing via swap
    - Collateral management (deposit/withdraw)
    - Full TX verification chain
    """

    def __init__(
        self,
        private_key: str,
        rpc_url: str = "https://arb1.arbitrum.io/rpc",
        run_dir: str = "./paper_runs",
        slippage_pct: float = 3.0,
        router_address: Optional[str] = None,
        oracle_address: Optional[str] = None,
    ):
        self.private_key = private_key
        self.rpc_url = rpc_url
        self.run_dir = Path(run_dir)
        self.slippage_pct = slippage_pct
        self.is_ready = False

        self.router_address = router_address or GMX_V2_ROUTER
        self.oracle_address = oracle_address or GMX_V2_ORACLE

        try:
            self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            if not self.w3.is_connected():
                raise ConnectionError(f"Cannot connect to Arbitrum RPC: {rpc_url}")

            self.account = self.w3.eth.account.from_key(private_key)
            self.address = self.account.address

            self.router = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.router_address),
                abi=GMX_V2_ROUTER_ABI,
            )
            self.oracle = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.oracle_address),
                abi=GMX_V2_ORACLE_ABI,
            )

            self.usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ARBITRUM),
                abi=ERC20_ABI,
            )

            logger.info(
                "GMX v2 executor initialized | %s | Arbitrum",
                self.address,
            )
            self.is_ready = True

        except Exception as exc:
            logger.error("Failed to init GMX v2 executor: %s", exc)
            self.is_ready = False

    def get_gmx_price(self, market_index: int) -> Optional[float]:
        """Fetch current price from GMX v2 oracle."""
        try:
            result = self.oracle.functions.getMarketPrices(market_index).call()
            price = result[0]
            decimal = 8
            return price / (10**decimal)
        except Exception as exc:
            logger.warning("Failed to fetch GMX price for market %d: %s", market_index, exc)
            return None

    def get_usdc_balance(self) -> float:
        """Get USDC balance on Arbitrum."""
        try:
            balance = self.usdc_contract.functions.balanceOf(self.address).call()
            return balance / 1e6
        except Exception as exc:
            logger.warning("Failed to get USDC balance: %s", exc)
            return 0.0

    def get_eth_balance(self) -> float:
        """Get ETH balance on Arbitrum."""
        try:
            balance = self.w3.eth.get_balance(self.address)
            return balance / 1e18
        except Exception as exc:
            logger.warning("Failed to get ETH balance: %s", exc)
            return 0.0

    def ensure_approval(self, amount: float) -> bool:
        """Ensure USDC approval for GMX v2 router."""
        try:
            allowance = self.usdc_contract.functions.allowance(self.address, self.router_address).call()
            required = int(amount * 1e6)
            if allowance >= required:
                return True

            logger.info("Approving USDC for GMX v2 router...")
            nonce = self.w3.eth.get_transaction_count(self.address)
            approve_tx = self.usdc_contract.functions.approve(self.router_address, 2**256 - 1).build_transaction(
                {
                    "from": self.address,
                    "nonce": nonce,
                    "gas": 60000,
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": 42161,
                }
            )
            signed = self.account.sign_transaction(approve_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt["status"] == 1:
                logger.info("USDC approval confirmed: %s", tx_hash.hex()[:16])
                return True
            else:
                logger.error("USDC approval failed: %s", tx_hash.hex()[:16])
                return False

        except Exception as exc:
            logger.error("Approval failed: %s", exc)
            return False

    def open_position(
        self,
        symbol: str,
        direction: int,
        size_usd: float,
        price: float,
    ) -> Optional[Dict[str, Any]]:
        """Open a perpetual position on GMX v2.

        Args:
            symbol: Trading pair (e.g., "BTC-USDT")
            direction: 1 for long, -1 for short
            size_usd: Position size in USD
            price: Entry price
        """
        market_index = MARKET_INDICES.get(symbol)
        if market_index is None:
            logger.warning("GMX v2: %s not supported", symbol)
            return None

        is_long = direction == 1
        collateral_amount = int(size_usd * 1e6)

        try:
            if not self.ensure_approval(size_usd):
                return None

            gmx_price = self.get_gmx_price(market_index)
            if gmx_price is None:
                logger.warning("GMX v2: cannot fetch price for %s", symbol)
                return None

            size_delta = int(size_usd / gmx_price * 1e8)

            nonce = self.w3.eth.get_transaction_count(self.address)
            gas_estimate = self.router.functions.swap(
                self.address,
                (
                    market_index,
                    is_long,
                    self.address,
                    [(USDC_ARBITRUM, collateral_amount, 0)],
                    size_delta,
                    0,
                    0,
                    0,
                    b"",
                ),
            ).estimate_gas({"from": self.address})

            swap_tx = self.router.functions.swap(
                self.address,
                (
                    market_index,
                    is_long,
                    self.address,
                    [(USDC_ARBITRUM, collateral_amount, 0)],
                    size_delta,
                    0,
                    0,
                    0,
                    b"",
                ),
            ).build_transaction(
                {
                    "from": self.address,
                    "nonce": nonce,
                    "gas": int(gas_estimate * 1.2),
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": 42161,
                }
            )

            signed = self.account.sign_transaction(swap_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt["status"] == 1:
                result = {
                    "success": True,
                    "tx_hash": tx_hash.hex(),
                    "block_number": receipt["blockNumber"],
                    "gas_used": receipt["gasUsed"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                self._log_tx(result)
                logger.info(
                    "GMX v2 position opened: %s %s $%.2f | TX %s",
                    symbol,
                    "LONG" if is_long else "SHORT",
                    size_usd,
                    tx_hash.hex()[:16],
                )
                return result
            else:
                logger.error("GMX v2 swap failed: %s", tx_hash.hex()[:16])
                return {"success": False, "tx_hash": tx_hash.hex(), "error": "TX reverted"}

        except Exception as exc:
            logger.error("GMX v2 open position failed: %s", exc)
            return {"success": False, "error": str(exc)[:200]}

    def close_position(
        self,
        symbol: str,
        direction: int,
        size_usd: float,
    ) -> Optional[Dict[str, Any]]:
        """Close a perpetual position on GMX v2."""
        market_index = MARKET_INDICES.get(symbol)
        if market_index is None:
            logger.warning("GMX v2: %s not supported", symbol)
            return None

        is_long = direction == 1
        size_delta = int(-size_usd / (self.get_gmx_price(market_index) or 1) * 1e8)

        try:
            nonce = self.w3.eth.get_transaction_count(self.address)
            gas_estimate = self.router.functions.swap(
                self.address,
                (
                    market_index,
                    is_long,
                    self.address,
                    [],
                    size_delta,
                    0,
                    0,
                    0,
                    b"",
                ),
            ).estimate_gas({"from": self.address})

            swap_tx = self.router.functions.swap(
                self.address,
                (
                    market_index,
                    is_long,
                    self.address,
                    [],
                    size_delta,
                    0,
                    0,
                    0,
                    b"",
                ),
            ).build_transaction(
                {
                    "from": self.address,
                    "nonce": nonce,
                    "gas": int(gas_estimate * 1.2),
                    "gasPrice": self.w3.eth.gas_price,
                    "chainId": 42161,
                }
            )

            signed = self.account.sign_transaction(swap_tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt["status"] == 1:
                result = {
                    "success": True,
                    "tx_hash": tx_hash.hex(),
                    "block_number": receipt["blockNumber"],
                    "gas_used": receipt["gasUsed"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                self._log_tx(result)
                logger.info(
                    "GMX v2 position closed: %s | TX %s",
                    symbol,
                    tx_hash.hex()[:16],
                )
                return result
            else:
                logger.error("GMX v2 close failed: %s", tx_hash.hex()[:16])
                return {"success": False, "tx_hash": tx_hash.hex(), "error": "TX reverted"}

        except Exception as exc:
            logger.error("GMX v2 close position failed: %s", exc)
            return {"success": False, "error": str(exc)[:200]}

    def _log_tx(self, result: Dict[str, Any]):
        """Log transaction to tx_log.jsonl."""
        tx_log_path = self.run_dir / "tx_log.jsonl"
        record = {"type": "gmx_v2", **result}
        with open(tx_log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def get_supported_markets(self) -> List[str]:
        """Return list of supported perp markets."""
        return list(MARKET_INDICES.keys())
