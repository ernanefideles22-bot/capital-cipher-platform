"""Ephemeral TESTNET credentials that are never serialised or persisted."""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.core.errors import ConfigurationError
from app.schemas.common import Exchange


@dataclass(frozen=True)
class TestnetCredentials:
    key_id: str
    signing_secret: str

    def __post_init__(self) -> None:
        if len(self.key_id.strip()) < 8 or len(self.signing_secret.strip()) < 16:
            raise ConfigurationError("Invalid TESTNET credential material")

    def __repr__(self) -> str:
        return "TestnetCredentials(key_id='***', signing_secret='***')"


class EnvironmentTestnetCredentialProvider:
    """Read credentials only when TESTNET is explicitly enabled."""

    _VARIABLES = {
        Exchange.BINANCE: (
            "CAPITAL_CIPHER_BINANCE_TESTNET_KEY_ID",
            "CAPITAL_CIPHER_BINANCE_TESTNET_SIGNING_SECRET",
        ),
        Exchange.BYBIT: (
            "CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID",
            "CAPITAL_CIPHER_BYBIT_TESTNET_SIGNING_SECRET",
        ),
    }

    def load(self, exchange: Exchange) -> TestnetCredentials:
        key_name, secret_name = self._VARIABLES[exchange]
        key_id = os.environ.get(key_name, "")
        signing_secret = os.environ.get(secret_name, "")
        if not key_id or not signing_secret:
            raise ConfigurationError(
                f"TESTNET credentials are missing for {exchange.value}"
            )
        return TestnetCredentials(
            key_id=key_id,
            signing_secret=signing_secret,
        )
