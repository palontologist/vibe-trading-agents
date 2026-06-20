"""Read-only wallet connector for EVM-compatible chains.

Fetches balances and token positions without requiring transaction signing.
Uses public RPC endpoints (no private keys needed).

Supported chains: Base, Polygon, Arbitrum, Optimism, Ethereum, BSC, Avalanche.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ERC20 ABI for balanceOf
ERC20_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}]'
)

# Chain presets: (RPC URLs, native symbol, USDC contract, WETH contract)
CHAIN_CONFIGS: Dict[str, Tuple[List[str], str, str, str]] = {
    "base": (
        [
            "https://mainnet.base.org",
            "https://base-rpc.publicnode.com",
            "https://base.llamarpc.com",
        ],
        "ETH",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "0x4200000000000000000000000000000000000006",
    ),
    "polygon": (
        [
            "https://1rpc.io/matic",
            "https://polygon.drpc.org",
            "https://polygon-rpc.com",
        ],
        "MATIC",
        "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
    ),
    "arbitrum": (
        [
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.llamarpc.com",
        ],
        "ETH",
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    ),
    "optimism": (
        [
            "https://mainnet.optimism.io",
            "https://optimism.llamarpc.com",
        ],
        "ETH",
        "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        "0x4200000000000000000000000000000000000006",
    ),
    "ethereum": (
        [
            "https://eth.llamarpc.com",
            "https://rpc.ankr.com/eth",
        ],
        "ETH",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    ),
    "bsc": (
        [
            "https://bsc-dataseed.binance.org",
            "https://bsc-rpc.publicnode.com",
        ],
        "BNB",
        "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    ),
    "avalanche": (
        [
            "https://api.avax.network/ext/bc/C/rpc",
            "https://avalanche-c-chain-rpc.publicnode.com",
        ],
        "AVAX",
        "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
    ),
}

# Map native token symbol -> decimals
NATIVE_DECIMALS: Dict[str, int] = {
    "ETH": 18,
    "MATIC": 18,
    "BNB": 18,
    "AVAX": 18,
}


class WalletConnector:
    """Read-only wallet state connector for EVM-compatible chains.

    No private keys required. Only reads balances and token positions.
    """

    def __init__(
        self,
        wallet_address: str,
        chain: str = "base",
        rpc_url: Optional[str] = None,
    ):
        self.wallet_address = self._validate_address(wallet_address)
        self.chain = chain.lower()
        self.rpc_url = rpc_url
        self._native_symbol: str = ""
        self._usdc_contract: str = ""
        self._weth_contract: str = ""
        self._native_decimals: int = 18
        self._balances: Dict[str, float] = {}
        self._resolve_chain_config()

    @staticmethod
    def _validate_address(addr: str) -> str:
        if not addr.startswith("0x") or len(addr) != 42:
            raise ValueError(f"Invalid Ethereum address: {addr}")
        return addr.lower()

    def _resolve_chain_config(self):
        """Resolve RPC URL and contract addresses for the configured chain."""
        if self.rpc_url:
            if self.chain in CHAIN_CONFIGS:
                _, self._native_symbol, self._usdc_contract, self._weth_contract = CHAIN_CONFIGS[self.chain]
            else:
                self._native_symbol = "NATIVE"
                self._usdc_contract = ""
                self._weth_contract = ""
        elif self.chain in CHAIN_CONFIGS:
            rpc_urls, self._native_symbol, self._usdc_contract, self._weth_contract = CHAIN_CONFIGS[self.chain]
            env_var = f"{self.chain.upper()}_RPC_URL"
            env_rpc = os.getenv(env_var)
            if env_rpc:
                self.rpc_url = env_rpc
            else:
                self.rpc_url = self._pick_working_rpc(rpc_urls)
        else:
            raise ValueError(
                f"Unknown chain: {self.chain}. "
                f"Supported: {', '.join(CHAIN_CONFIGS.keys())}. "
                f"Or provide a custom rpc_url."
            )

        self._native_decimals = NATIVE_DECIMALS.get(self._native_symbol, 18)

    @staticmethod
    def _pick_working_rpc(urls: List[str]) -> str:
        for url in urls:
            try:
                requests.post(
                    url,
                    json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
                    timeout=5,
                )
                return url
            except Exception:
                continue
        return urls[0]

    def _rpc_call(self, method: str, params: List) -> dict:
        assert self.rpc_url is not None, f"No RPC URL configured for chain: {self.chain}"
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
        resp = requests.post(self.rpc_url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data

    def get_native_balance(self) -> float:
        """Get native token balance (ETH on Base, MATIC on Polygon, etc.)."""
        try:
            result = self._rpc_call("eth_getBalance", [self.wallet_address, "latest"])
            raw = int(result["result"], 16)
            return raw / (10**self._native_decimals)
        except Exception as exc:
            logger.error("Failed to get %s balance on %s: %s", self._native_symbol, self.chain, exc)
            return 0.0

    def get_erc20_balance(self, token_address: str, decimals: int = 6) -> float:
        """Get ERC20 token balance.

        Args:
            token_address: Contract address of the token.
            decimals: Token decimals (USDC=6, WETH=18).

        Returns:
            Balance in token units.
        """
        try:
            # Encode balanceOf(address)
            selector = "0x70a08231"
            padded_addr = self.wallet_address[2:].zfill(64)
            data = selector + padded_addr

            result = self._rpc_call(
                "eth_call",
                [
                    {
                        "to": token_address,
                        "data": data,
                    },
                    "latest",
                ],
            )

            raw_balance = int(result["result"], 16)
            return raw_balance / (10**decimals)
        except Exception as exc:
            logger.error("Failed to get ERC20 balance for %s: %s", token_address, exc)
            return 0.0

    def get_all_balances(self) -> Dict[str, float]:
        """Fetch all relevant token balances for the wallet.

        Returns:
            Dict mapping token symbol -> balance.
        """
        self._balances = {
            self._native_symbol: self.get_native_balance(),
        }
        if self._usdc_contract:
            self._balances["USDC"] = self.get_erc20_balance(self._usdc_contract, decimals=6)
        if self._weth_contract:
            self._balances["WETH"] = self.get_erc20_balance(self._weth_contract, decimals=18)
        return self._balances

    def get_portfolio_value_usd(
        self,
        native_price: Optional[float] = None,
        weth_price: Optional[float] = None,
    ) -> float:
        """Estimate total portfolio value in USD.

        Args:
            native_price: Current native token price in USD (MATIC, ETH, etc.).
            weth_price: Current WETH price in USD. Defaults to native_price.

        Returns:
            Approximate USD value.
        """
        if not self._balances:
            self.get_all_balances()

        total = self._balances.get("USDC", 0.0)
        if native_price is not None:
            total += self._balances.get(self._native_symbol, 0.0) * native_price
            wp = weth_price if weth_price is not None else native_price
            assert wp is not None
            total += self._balances.get("WETH", 0.0) * wp
        return total

    def get_wallet_summary(self) -> str:
        """Get human-readable wallet summary."""
        balances = self.get_all_balances()
        lines = [f"Wallet: {self.wallet_address}", f"Chain: {self.chain}", "Balances:"]
        for token, balance in balances.items():
            lines.append(f"  {token}: {balance:.6f}")
        return "\n".join(lines)
