"""Ethereal perpetual futures client.

REST API + EIP-712 signed order placement on Ethereal L2 (chainId 5064014).
Uses the official API at https://api.ethereal.trade/v1 with market/limit orders.
"""

import json
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from web3 import Web3

logger = logging.getLogger(__name__)

API_BASE = "https://api.ethereal.trade/v1"
ETHEREAL_RPC = "https://rpc.ethereal.trade"


class EtherealClient:
    """Ethereal perp trading client with EIP-712 order signing."""

    def __init__(self, private_key: str):
        self.private_key = private_key
        self.account = Web3().eth.account.from_key(private_key)
        self.address = self.account.address
        self.domain: Optional[Dict[str, Any]] = None
        self.signature_types: Optional[Dict[str, str]] = None
        self.products: List[Dict[str, Any]] = []
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

    def initialize(self) -> bool:
        """Fetch EIP-712 domain config and product list."""
        try:
            resp = self._session.get(f"{API_BASE}/rpc/config", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self.domain = data["domain"]
            self.signature_types = data["signatureTypes"]
            logger.info(
                "Ethereal domain: %s v%s chainId=%s",
                self.domain["name"],
                self.domain["version"],
                self.domain["chainId"],
            )
        except Exception as exc:
            logger.error("Failed to fetch Ethereal config: %s", exc)
            return False

        try:
            resp = self._session.get(f"{API_BASE}/product", timeout=10)
            resp.raise_for_status()
            self.products = resp.json().get("data", [])
            logger.info("Ethereal products: %d loaded", len(self.products))
        except Exception as exc:
            logger.error("Failed to fetch products: %s", exc)

        return self.domain is not None

    def get_product(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Find product by ticker (e.g., 'BTCUSD')."""
        for p in self.products:
            if p["ticker"] == ticker or p.get("displayTicker") == ticker:
                return p
        return None

    def get_product_by_onchain_id(self, onchain_id: int) -> Optional[Dict[str, Any]]:
        """Find product by onchainId."""
        for p in self.products:
            if p.get("onchainId") == onchain_id:
                return p
        return None

    def get_subaccount_balance(self, subaccount: str = "primary") -> Optional[Dict[str, Any]]:
        """Get subaccount balance."""
        try:
            subaccount_id = self._encode_subaccount(subaccount)
            resp = self._session.get(
                f"{API_BASE}/subaccount/balance",
                params={"subaccountId": subaccount_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("data")
        except Exception as exc:
            logger.warning("Failed to get subaccount balance: %s", exc)
            return None

    def get_positions(self, subaccount: str = "primary") -> List[Dict[str, Any]]:
        """Get open positions."""
        try:
            subaccount_id = self._encode_subaccount(subaccount)
            resp = self._session.get(
                f"{API_BASE}/position",
                params={"subaccountId": subaccount_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except Exception as exc:
            logger.warning("Failed to get positions: %s", exc)
            return []

    def place_market_order(
        self,
        ticker: str,
        side: str,
        quantity: str,
        subaccount: str = "primary",
        reduce_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Place a market order with EIP-712 signature.

        Args:
            ticker: Product ticker (e.g., 'BTCUSD')
            side: 'BUY' or 'SELL'
            quantity: Human-readable quantity string (e.g., '5.5')
            subaccount: Subaccount name (default 'primary')
            reduce_only: Whether this order should only reduce position
        """
        product = self.get_product(ticker)
        if not product:
            logger.error("Unknown product: %s", ticker)
            return None

        onchain_id = product["onchainId"]
        side_num = 0 if side == "BUY" else 1

        nonce = self._get_nonce()
        signed_at = int(time.time())

        try:
            signature = self._sign_trade_order(
                subaccount=subaccount,
                quantity=quantity,
                price="0",
                reduce_only=reduce_only,
                side=side_num,
                product_id=onchain_id,
                nonce=nonce,
                signed_at=signed_at,
            )

            body = {
                "data": {
                    "sender": self.address,
                    "subaccount": self._encode_subaccount(subaccount),
                    "quantity": quantity,
                    "reduceOnly": reduce_only,
                    "side": side_num,
                    "engineType": 0,
                    "onchainId": onchain_id,
                    "type": "MARKET",
                    "nonce": nonce,
                    "signedAt": signed_at,
                },
                "signature": signature,
            }

            resp = self._session.post(f"{API_BASE}/order", json=body, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            logger.info("Market order placed: %s %s %s @ %s", side, quantity, ticker, result.get("orderId", "unknown"))
            return result

        except Exception as exc:
            logger.error("Market order failed: %s", exc)
            return {"error": str(exc)}

    def place_limit_order(
        self,
        ticker: str,
        side: str,
        quantity: str,
        price: str,
        subaccount: str = "primary",
        reduce_only: bool = False,
        time_in_force: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """Place a limit order with EIP-712 signature."""
        product = self.get_product(ticker)
        if not product:
            logger.error("Unknown product: %s", ticker)
            return None

        onchain_id = product["onchainId"]
        side_num = 0 if side == "BUY" else 1

        nonce = self._get_nonce()
        signed_at = int(time.time())

        try:
            price_bigint = str(int(float(price) * 1e9))
            quantity_bigint = str(int(float(quantity) * 1e9))

            signature = self._sign_trade_order(
                subaccount=subaccount,
                quantity=quantity_bigint,
                price=price_bigint,
                reduce_only=reduce_only,
                side=side_num,
                product_id=onchain_id,
                nonce=nonce,
                signed_at=signed_at,
            )

            body = {
                "data": {
                    "sender": self.address,
                    "subaccount": self._encode_subaccount(subaccount),
                    "quantity": quantity,
                    "price": price,
                    "reduceOnly": reduce_only,
                    "side": side_num,
                    "engineType": 0,
                    "onchainId": onchain_id,
                    "type": "LIMIT",
                    "timeInForce": time_in_force,
                    "postOnly": False,
                    "nonce": nonce,
                    "signedAt": signed_at,
                },
                "signature": signature,
            }

            resp = self._session.post(f"{API_BASE}/order", json=body, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            logger.info("Limit order placed: %s %s %s @ %s", side, quantity, ticker, price)
            return result

        except Exception as exc:
            logger.error("Limit order failed: %s", exc)
            return {"error": str(exc)}

    def cancel_orders(
        self,
        order_ids: List[str],
        subaccount: str = "primary",
    ) -> Optional[Dict[str, Any]]:
        """Cancel orders by ID."""
        nonce = self._get_nonce()

        try:
            signature = self._sign_cancel_order(
                subaccount=subaccount,
                nonce=nonce,
            )

            body = {
                "data": {
                    "sender": self.address,
                    "subaccount": self._encode_subaccount(subaccount),
                    "nonce": nonce,
                    "orderIds": order_ids,
                },
                "signature": signature,
            }

            resp = self._session.post(f"{API_BASE}/order/cancel", json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()

        except Exception as exc:
            logger.error("Cancel orders failed: %s", exc)
            return {"error": str(exc)}

    # ── EIP-712 Signing ──

    def _sign_trade_order(
        self,
        subaccount: str,
        quantity: str,
        price: str,
        reduce_only: bool,
        side: int,
        product_id: int,
        nonce: str,
        signed_at: int,
    ) -> str:
        """Sign a TradeOrder EIP-712 message."""
        quantity_bigint = int(quantity) if quantity else 0
        price_bigint = int(price) if price else 0
        nonce_bigint = int(nonce)

        message = {
            "sender": self.address,
            "subaccount": self._encode_subaccount(subaccount),
            "quantity": quantity_bigint,
            "price": price_bigint,
            "reduceOnly": reduce_only,
            "side": side,
            "engineType": 0,
            "productId": product_id,
            "nonce": nonce_bigint,
            "signedAt": signed_at,
        }

        types = {
            "TradeOrder": [
                {"name": "sender", "type": "address"},
                {"name": "subaccount", "type": "bytes32"},
                {"name": "quantity", "type": "uint256"},
                {"name": "price", "type": "uint256"},
                {"name": "reduceOnly", "type": "bool"},
                {"name": "side", "type": "uint8"},
                {"name": "engineType", "type": "uint8"},
                {"name": "productId", "type": "uint32"},
                {"name": "nonce", "type": "uint64"},
                {"name": "signedAt", "type": "uint64"},
            ],
        }

        domain = {
            "name": self.domain["name"],
            "version": self.domain["version"],
            "chainId": self.domain["chainId"],
            "verifyingContract": self.domain["verifyingContract"],
        }

        structured_data = {
            "domain": domain,
            "primaryType": "TradeOrder",
            "types": types,
            "message": message,
        }

        signed = self.account.sign_typed_data(structured_data=structured_data)
        return signed.signature.hex()

    def _sign_cancel_order(
        self,
        subaccount: str,
        nonce: str,
    ) -> str:
        """Sign a CancelOrder EIP-712 message."""
        message = {
            "sender": self.address,
            "subaccount": self._encode_subaccount(subaccount),
            "nonce": int(nonce),
        }

        types = {
            "CancelOrder": [
                {"name": "sender", "type": "address"},
                {"name": "subaccount", "type": "bytes32"},
                {"name": "nonce", "type": "uint64"},
            ],
        }

        domain = {
            "name": self.domain["name"],
            "version": self.domain["version"],
            "chainId": self.domain["chainId"],
            "verifyingContract": self.domain["verifyingContract"],
        }

        structured_data = {
            "domain": domain,
            "primaryType": "CancelOrder",
            "types": types,
            "message": message,
        }

        signed = self.account.sign_typed_data(structured_data=structured_data)
        return signed.signature.hex()

    # ── Helpers ──

    @staticmethod
    def _encode_subaccount(name: str) -> str:
        """Encode subaccount name as bytes32 hex string."""
        padded = name.encode().ljust(32, b"\x00")
        return "0x" + padded.hex()

    @staticmethod
    def _get_nonce() -> str:
        """Generate nanosecond nonce."""
        return str(int(time.time() * 1e9))

    def get_supported_tickers(self) -> List[str]:
        """Return list of supported tickers."""
        return [p["ticker"] for p in self.products]

    def get_market_info(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get market info for a ticker."""
        return self.get_product(ticker)
