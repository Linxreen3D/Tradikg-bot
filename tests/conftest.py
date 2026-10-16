from __future__ import annotations

import pandas as pd
import pytest

from tradikg_bot.data import generate_synthetic_ohlcv
from tradikg_bot.domain import AssetClass, Instrument


@pytest.fixture
def crypto_instrument() -> Instrument:
    return Instrument("TEST-EUR", AssetClass.CRYPTO, qty_step=1e-6, min_notional=0.0)


@pytest.fixture
def synthetic_bars() -> pd.DataFrame:
    return generate_synthetic_ohlcv(n_bars=600, seed=7, annual_vol=0.8)
