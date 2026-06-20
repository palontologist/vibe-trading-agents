"""Signing wallet for on-chain execution.

Uses web3.py to derive address, sign transactions, and send on-chain trades.
Reads PRIVATE_KEY from environment only — never hardcoded.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Polygon mainnet defaults
POLYGON_RPC = "https://polygon-rpc.com"
POLYGON_CHAIN_ID = 137

# ERC20 ABI fragments
ERC20_ABI = [
    {
        "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class SigningWallet:
    """EVM wallet that can sign and send transactions."""

    def __init__(
        self,
        private_key: Optional[str] = None,
        rpc_url: Optional[str] = None,
        chain_id: Optional[int] = None,
    ):
        self._private_key = private_key or os.environ.get("PRIVATE_KEY", "")
        self.rpc_url = rpc_url or os.environ.get("POLYGON_RPC_URL", POLYGON_RPC)
        self._chain_id = chain_id
        self._web3: Any = None
        self._account: Any = None
        self.address: str = ""
        self._init_web3()

    def _init_web3(self):
        try:
            from web3 import Web3

            self._web3 = Web3(Web3.HTTPProvider(self.rpc_url))
            if not self._web3.is_connected():
                raise RuntimeError(f"Cannot connect to RPC: {self.rpc_url}")

            pk = self._private_key
            if not pk.startswith("0x"):
                pk = "0x" + pk
            self._account = self._web3.eth.account.from_key(pk)
            self.address = self._account.address
            logger.info("Wallet initialized: %s on chain %s", self.address, self._web3.eth.chain_id)
        except ImportError:
            logger.warning("web3 not installed — SigningWallet will be non-functional")
        except Exception as exc:
            logger.error("Failed to init wallet: %s", exc)

    @property
    def is_ready(self) -> bool:
        return self._web3 is not None and self._account is not None

    def get_balance_wei(self) -> int:
        if not self.is_ready:
            return 0
        return self._web3.eth.get_balance(self.address)

    def get_balance_eth(self) -> float:
        if not self.is_ready:
            return 0.0
        return self._web3.from_wei(self.get_balance_wei(), "ether")

    def get_erc20_balance(self, token_address: str, decimals: int = 18) -> float:
        if not self.is_ready:
            return 0.0
        try:
            contract = self._web3.eth.contract(address=token_address, abi=ERC20_ABI)
            raw = contract.functions.balanceOf(self.address).call()
            return raw / (10**decimals)
        except Exception as exc:
            logger.error("Failed to get ERC20 balance: %s", exc)
            return 0.0

    def build_and_send_tx(
        self,
        to: str,
        value_wei: int = 0,
        data: bytes = b"",
        gas_limit: Optional[int] = None,
        max_fee_per_gas: Optional[int] = None,
    ) -> Optional[str]:
        """Build, sign, and send a transaction.

        Returns tx hash on success, None on failure.
        """
        if not self.is_ready:
            logger.error("Wallet not ready")
            return None

        try:
            nonce = self._web3.eth.get_transaction_count(self.address)
            chain_id = self._chain_id or self._web3.eth.chain_id

            tx: Dict[str, Any] = {
                "from": self.address,
                "to": to,
                "value": value_wei,
                "data": data,
                "nonce": nonce,
                "chainId": chain_id,
                "type": 2,
            }

            if gas_limit:
                tx["gas"] = gas_limit
            else:
                estimate = self._web3.eth.estimate_gas(tx)
                tx["gas"] = int(estimate * 1.2)

            if max_fee_per_gas:
                tx["maxFeePerGas"] = max_fee_per_gas
            else:
                tx["maxFeePerGas"] = self._web3.eth.gas_price * 2
            tx["maxPriorityFeePerGas"] = self._web3.to_wei(2, "gwei")

            signed = self._account.sign_transaction(tx)
            tx_hash = self._web3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("TX sent: %s", tx_hash.hex())
            return tx_hash.hex()

        except Exception as exc:
            logger.error("Transaction failed: %s", exc, exc_info=True)
            return None

    def wait_for_receipt(self, tx_hash: str, timeout: int = 120) -> Optional[Dict[str, Any]]:
        if not self.is_ready:
            return None
        try:
            receipt = self._web3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
            return dict(receipt)
        except Exception as exc:
            logger.error("Receipt wait failed: %s", exc)
            return None

    def approve_erc20(
        self,
        token_address: str,
        spender: str,
        amount: float,
        decimals: int = 18,
    ) -> Optional[str]:
        """Approve a spender for ERC20 token spending."""
        if not self.is_ready:
            return None
        try:
            contract = self._web3.eth.contract(address=token_address, abi=ERC20_ABI)
            amount_wei = int(amount * (10**decimals))
            tx = contract.functions.approve(spender, amount_wei).build_transaction(
                {
                    "from": self.address,
                    "nonce": self._web3.eth.get_transaction_count(self.address),
                    "gasPrice": self._web3.eth.gas_price,
                }
            )
            signed = self._account.sign_transaction(tx)
            tx_hash = self._web3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("Approval TX sent: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            logger.error("Approval failed: %s", exc)
            return None

    def check_allowance(
        self,
        token_address: str,
        spender: str,
        decimals: int = 18,
    ) -> float:
        """Check current allowance for a spender."""
        if not self.is_ready:
            return 0.0
        try:
            contract = self._web3.eth.contract(address=token_address, abi=ERC20_ABI)
            raw = contract.functions.allowance(self.address, spender).call()
            return raw / (10**decimals)
        except Exception:
            return 0.0
