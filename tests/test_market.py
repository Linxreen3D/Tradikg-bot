from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tradikg_bot.domain import AssetClass
from tradikg_bot.market.kraken_data import KrakenMarketData, is_stale, timeframe_minutes
from tradikg_bot.market.resilience import RetryExhaustedError, TokenBucket, retry_call
from tradikg_bot.market.scanner import ScanConfig, scan_markets
from tradikg_bot.market.sources import KRAKEN_SPOT, YAHOO_STOCKS, is_market_open, usable_for_trading
from tradikg_bot.storage import SQLiteStore

from market_fakes import FakeKrakenPublic

NOW = pd.Timestamp("2026-10-16 10:20", tz="UTC")


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def md(fake, store=None, attempts=4):
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, capacity=2.0, clock=clock, sleep=clock.sleep)
    return KrakenMarketData(
        fake, store, bucket, now=lambda: NOW, sleep=clock.sleep, attempts=attempts
    ), clock


def test_ohlc_drops_uncommitted_candle_and_is_validated():
    fake = FakeKrakenPublic(NOW)
    data, _ = md(fake)
    bars = data.fetch_ohlc("XBTEUR", "1h")
    assert len(bars) == len(fake.opens) - 1
    assert bars.index[-1] + pd.Timedelta(hours=1) <= NOW  # nur abgeschlossene Kerzen
    assert str(bars.index.tz) == "UTC" and bars.index.is_monotonic_increasing
    assert fake.calls[-1][1]["interval"] == 60


def test_cache_is_incremental(tmp_path):
    fake = FakeKrakenPublic(NOW)
    with SQLiteStore(tmp_path / "c.sqlite") as store:
        data, _ = md(fake, store)
        first = data.update_cache("XBTEUR", "1h")
        assert first == len(fake.opens) - 1
        assert data.update_cache("XBTEUR", "1h") == 0  # nichts Neues
        assert "since" in fake.calls[-1][1]
        assert len(data.history("XBTEUR", "1h", bars=50)) == 50


def test_retry_with_backoff_on_timeouts():
    fake = FakeKrakenPublic(NOW)
    fake.fail_next = 2
    data, clock = md(fake)
    assert not data.fetch_ohlc("XBTEUR", "1h").empty
    backoffs = [s for s in clock.slept if s >= 1.0]
    assert len(backoffs) == 2 and backoffs[1] > backoffs[0]
    assert data.last_error is None and data.last_success == NOW


def test_retry_gives_up_and_records_error():
    fake = FakeKrakenPublic(NOW)
    fake.fail_next = 10
    data, _ = md(fake, attempts=3)
    with pytest.raises(RetryExhaustedError):
        data.fetch_ohlc("XBTEUR", "1h")
    assert "OHLC" in data.last_error


def test_rate_limiter_spaces_requests():
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, capacity=2.0, clock=clock, sleep=clock.sleep)
    waits = [bucket.acquire() for _ in range(5)]
    assert waits[:2] == [0.0, 0.0]  # Burst
    assert all(w == pytest.approx(1.0) for w in waits[2:])


def test_retry_call_validates_attempts():
    with pytest.raises(ValueError):
        retry_call(lambda: 1, retry_on=(RuntimeError,), attempts=0)


def test_stale_detection():
    last = pd.Timestamp("2026-10-16 08:00", tz="UTC")
    assert not is_stale(last, "1h", pd.Timestamp("2026-10-16 10:10", tz="UTC"))
    assert is_stale(last, "1h", pd.Timestamp("2026-10-16 10:31", tz="UTC"))
    assert is_stale(None, "1h", NOW)
    with pytest.raises(ValueError):
        timeframe_minutes("2h")


def test_scanner_selects_only_tradable_affordable_liquid_pairs():
    data, _ = md(FakeKrakenPublic(NOW))
    result = scan_markets(data, ScanConfig(capital_eur=10.0))
    names = [c.rules.altname for c in result.candidates]
    assert "XBTEUR" in names and "SOLEUR" in names
    assert "Mindestorder" in result.rejected["ETHEUR"]  # 0.01 ETH ≈ 30 EUR > 10 EUR
    assert "Spread" in result.rejected["ILLIQEUR"]
    assert "USDTEUR" not in names and "USDTEUR" not in result.rejected  # Stablecoin-Basis
    assert "XBTUSD" not in names and "DELISTEUR" not in names  # andere Quote / nicht online
    assert result.candidates == sorted(result.candidates, key=lambda c: -c.score)


def test_scanner_with_more_capital_includes_eth():
    data, _ = md(FakeKrakenPublic(NOW))
    names = [c.rules.altname for c in scan_markets(data, ScanConfig(capital_eur=100.0)).candidates]
    assert "ETHEUR" in names


def test_sources_and_trading_hours():
    assert usable_for_trading(KRAKEN_SPOT) and not usable_for_trading(YAHOO_STOCKS)
    sat = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.UTC)
    wed_morning = dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.UTC)  # 10:00 Berlin
    wed_night = dt.datetime(2026, 10, 14, 20, 0, tzinfo=dt.UTC)
    assert is_market_open(AssetClass.CRYPTO, sat)
    assert not is_market_open(AssetClass.STOCK, sat)
    assert is_market_open(AssetClass.STOCK, wed_morning)
    assert not is_market_open(AssetClass.STOCK, wed_night)
