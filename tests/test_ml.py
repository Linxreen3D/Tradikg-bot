from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from tradikg_bot.features import FEATURE_COLUMNS
from tradikg_bot.ml.champion import decide
from tradikg_bot.ml.dataset import build_dataset, chronological_splits, label_signals
from tradikg_bot.ml.models import (
    BaseRateModel,
    LogisticModel,
    brier,
    evaluate,
    expected_calibration_error,
)
from tradikg_bot.ml.registry import ModelRegistry
from tradikg_bot.ml.train import train_and_evaluate
from tradikg_bot.strategies.base import empty_signals
from tradikg_bot.strategies.families import strategy_registry
from tradikg_bot.trading.probability import ModelProbability, ProbabilityEstimate

from edge_data import momentum_ohlcv
from helpers import make_bars

pytest.importorskip("sklearn")


def test_triple_barrier_labels():
    rows = [(100, 100, 100, 100)] * 2 + [
        (100, 106, 99, 105),  # TP (5) vor SL
        (100, 101, 97, 98),  # SL
        (100, 106, 97, 100),  # beide -> konservativ SL
        (100, 100.5, 99.5, 100),  # nichts erreicht
        (100, 100.5, 99.5, 100),
    ]
    bars = make_bars(rows, freq="1h")
    sig = empty_signals(bars.index)
    sig["stop_distance"], sig["take_profit_distance"] = 2.0, 5.0
    for i in (1, 2, 3, 4):
        sig.iloc[i, 0] = True
    labels = label_signals(bars, sig, horizon=1)
    assert labels["label"].tolist() == [1, 0, 0]  # Signal bei i=4: kein Ergebnis -> verworfen
    assert (labels["reward_risk"] == 2.5).all()


def synthetic_dataset(n=2000, informative=True, seed=0):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    df["reward_risk"] = 2.0
    df["strategy"] = rng.choice(["a", "b"], n)
    logit = 2.0 * df["rsi"] - 0.7 if informative else np.full(n, -0.7)
    df["label"] = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    df["signal_ts"] = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    df["symbol"] = "SYN"
    return df


def test_splits_are_chronological_with_embargo():
    df = synthetic_dataset(500)
    tr, va, te = chronological_splits(df, embargo=pd.Timedelta(hours=10))
    assert tr["signal_ts"].max() < va["signal_ts"].min() < te["signal_ts"].min()
    assert va["signal_ts"].min() - tr["signal_ts"].max() > pd.Timedelta(hours=10)


def test_metrics_and_calibration():
    y = np.array([0, 0, 1, 1])
    assert brier(np.array([0, 0, 1, 1.0]), y) == 0
    assert expected_calibration_error(np.full(4, 0.5), y) == pytest.approx(0.0)
    m = evaluate(np.full(4, 0.9), y)
    assert m["ece"] == pytest.approx(0.4) and m["reliability"][0]["n"] == 4


def test_models_roundtrip_as_json_without_pickle():
    df = synthetic_dataset(400)
    for model in (BaseRateModel().fit(df), LogisticModel().fit(df)):
        params = json.loads(json.dumps(model.to_params()))
        clone = type(model).from_params(params)
        np.testing.assert_allclose(clone.predict(df), model.predict(df))


def test_challenger_with_real_signal_is_promoted(tmp_path):
    reg = ModelRegistry(tmp_path / "m.sqlite")
    res = train_and_evaluate(synthetic_dataset(3000, informative=True), reg)
    assert res.decision.promote
    assert reg.champion().kind == "logistic"
    assert reg.get(res.baseline_version).status == "retired"


def test_challenger_without_signal_is_rejected_and_baseline_stays(tmp_path):
    reg = ModelRegistry(tmp_path / "m.sqlite")
    res = train_and_evaluate(synthetic_dataset(3000, informative=False, seed=3), reg)
    assert not res.decision.promote
    assert reg.champion().kind == "base_rate"  # einfacheres Modell bleibt
    assert reg.get(res.challenger_version).status == "retired"


def test_live_mode_never_auto_promotes(tmp_path):
    reg = ModelRegistry(tmp_path / "m.sqlite")
    res = train_and_evaluate(synthetic_dataset(3000, informative=True), reg, live_mode=True)
    assert not res.decision.promote and res.decision.requires_manual_approval
    assert reg.champion().kind == "base_rate"
    assert reg.get(res.challenger_version).status == "challenger"  # wartet auf Freigabe


def test_decide_requires_enough_samples():
    y = np.array([0, 1] * 20)
    d = decide(y, np.full(40, 0.5), y.astype(float))
    assert not d.promote and "zu wenige" in d.reasons[0]


def test_registry_records_predictions_and_outcomes(tmp_path):
    reg = ModelRegistry(tmp_path / "m.sqlite")
    v = reg.register(BaseRateModel(rates={"a": 0.4}), {"brier": 0.2}, {}, ["strategy"])
    reg.record_prediction(v, "SYN", "a", "2026-01-01T00:00:00+00:00", 0.4)
    reg.record_prediction(v, "SYN", "a", "2026-01-01T00:00:00+00:00", 0.9)  # doppelt -> ignoriert
    reg.record_outcome(v, "SYN", "a", "2026-01-01T00:00:00+00:00", True, "2026-01-01T03:00")
    preds = reg.predictions(v)
    assert len(preds) == 1 and preds.iloc[0]["outcome"] == 1 and preds.iloc[0]["p"] == 0.4
    assert not reg.events().empty


def test_model_probability_uses_ml_champion_and_falls_back(tmp_path):
    class Fallback:
        def estimate(self, strategy, symbol, regime, reward_risk, features=None):
            return ProbabilityEstimate(0.33, "oos[*/*]", 50)

    reg = ModelRegistry(tmp_path / "m.sqlite")
    assert ModelProbability(reg, Fallback()).estimate("a", "SYN", "range", 2.0).p_win == 0.33
    train_and_evaluate(synthetic_dataset(3000, informative=True), reg)
    mp = ModelProbability(reg, Fallback())
    feats = pd.Series(
        np.zeros(len(FEATURE_COLUMNS)),
        index=FEATURE_COLUMNS,
        name=pd.Timestamp("2026-01-01", tz="UTC"),
    )
    est = mp.estimate("a", "SYN", "range", 2.0, feats)
    assert est.source.startswith("ml[") and est.model_version
    assert len(reg.predictions(est.model_version)) == 1
    assert mp.estimate("a", "SYN", "range", 2.0, None).p_win == 0.33  # ohne Features


def test_dataset_from_real_strategies():
    bars = momentum_ohlcv(1500, sigma=0.02)
    spec = strategy_registry()["momentum_breakout"]
    ds = build_dataset({"SYN": bars}, {"SYN": [spec.build(spec.grid[0])]}, horizon=48)
    assert len(ds) > 10 and set(ds["label"].unique()) <= {0, 1}
    assert not ds[FEATURE_COLUMNS].isna().any().any()
    assert ds["signal_ts"].is_monotonic_increasing
