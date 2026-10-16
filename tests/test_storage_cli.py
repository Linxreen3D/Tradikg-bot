import pandas as pd
import pytest

from tradikg_bot.backtest import BacktestEngine
from tradikg_bot.cli import main
from tradikg_bot.config import LiveTradingNotAllowedError
from tradikg_bot.costs import CRYPTO_SPOT_DEFAULT
from tradikg_bot.data import generate_synthetic_ohlcv
from tradikg_bot.domain import AssetClass, Instrument
from tradikg_bot.storage import SQLiteStore
from tradikg_bot.strategies import SmaCrossoverStrategy


@pytest.fixture
def store(tmp_path):
    with SQLiteStore(tmp_path / "db.sqlite") as s:
        yield s


def test_bars_roundtrip_and_idempotent_upsert(store):
    bars = generate_synthetic_ohlcv(50)
    assert store.save_bars("X", "1d", bars, source="synthetic") == 50
    store.save_bars("X", "1d", bars.iloc[-10:] * 1.0, source="synthetic")
    loaded = store.load_bars("X", "1d")
    pd.testing.assert_frame_equal(loaded, bars, check_freq=False)
    series = store.list_series()
    assert series.iloc[0]["bars"] == 50


def test_load_bars_with_date_filter(store):
    store.save_bars("X", "1d", generate_synthetic_ohlcv(30, start="2024-01-01"))
    part = store.load_bars("X", "1d", start="2024-01-10", end="2024-01-20")
    assert part.index[0] == pd.Timestamp("2024-01-10", tz="UTC")
    assert part.index[-1] == pd.Timestamp("2024-01-19", tz="UTC")


def test_save_bars_requires_tz_aware_index(store):
    bars = generate_synthetic_ohlcv(5)
    bars.index = bars.index.tz_localize(None)
    with pytest.raises(ValueError):
        store.save_bars("X", "1d", bars)


def test_instrument_roundtrip(store):
    inst = Instrument("AAPL", AssetClass.STOCK, 1.0, 1.0, "USD")
    store.upsert_instrument(inst)
    assert store.get_instrument("AAPL") == inst
    assert store.get_instrument("MISSING") is None


def test_backtest_persistence(store):
    bars = generate_synthetic_ohlcv(600, seed=7, annual_vol=0.8)
    inst = Instrument.default_for("SYN", "crypto")
    res = BacktestEngine(inst, CRYPTO_SPOT_DEFAULT).run(bars, SmaCrossoverStrategy())
    run_id = store.save_backtest(res, timeframe="1d")
    trades = store.load_trades(run_id)
    assert len(trades) == len(res.trades) > 0
    assert trades["net_pnl"].sum() == pytest.approx(sum(t.net_pnl for t in res.trades))
    metrics = store.load_run_metrics(run_id)
    assert metrics["num_trades"] == len(res.trades)
    assert len(store.load_runs()) == 1


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "cli.sqlite")
    assert main(["--db", db, "sample-data", "--symbol", "SYN", "--bars", "400"]) == 0
    assert main(["--db", db, "list-data"]) == 0
    export = tmp_path / "trades.csv"
    assert main(["--db", db, "backtest", "--symbol", "SYN", "--export-trades", str(export)]) == 0
    out = capsys.readouterr().out
    assert "Endkapital" in out and "Analyse-Benchmark" in out
    assert export.exists()
    assert main(["--db", db, "runs"]) == 0


def test_cli_import_csv_and_missing_data(tmp_path, capsys):
    db = str(tmp_path / "cli.sqlite")
    csv = tmp_path / "bars.csv"
    generate_synthetic_ohlcv(200).to_csv(csv)
    assert main(["--db", db, "import-csv", str(csv), "--symbol", "CSV"]) == 0
    assert "Validierung" in capsys.readouterr().out
    assert main(["--db", db, "backtest", "--symbol", "NOPE", "--no-save"]) == 1


def test_cli_refuses_live_config(tmp_path):
    cfg = tmp_path / "live.toml"
    cfg.write_text('[app]\nmode = "live"\n')
    with pytest.raises(LiveTradingNotAllowedError):
        main(["--config", str(cfg), "list-data"])
