"""Interpretierbare Modelle für P(TP vor SL) und ihre Kalibrierungsmetriken.

* ``BaseRateModel``: Trefferquote je Strategie aus den Trainingsdaten (geschrumpft
  auf die Vorannahme ohne Vorteil). Das ist die einfache Baseline.
* ``LogisticModel``: Logistische Regression auf standardisierten Features
  (Training mit scikit-learn, optional). Gespeichert werden nur Koeffizienten als
  JSON – kein Pickle, keine Code-Ausführung beim Laden. Die Vorhersage braucht
  nur numpy.

Ein ML-Modell wird nur verwendet, wenn es die Baseline auf unabhängigen Daten
belastbar schlägt (siehe ``champion.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.features import FEATURE_COLUMNS

MODEL_FEATURES = [*FEATURE_COLUMNS, "reward_risk"]


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2)) if len(y) else float("nan")


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))) if len(y) else float("nan")


def reliability(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    """Kalibrierungstabelle: vorhergesagte vs. beobachtete Trefferquote je Bereich."""
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for a, b in zip(edges[:-1], edges[1:], strict=True):
        mask = (p >= a) & (p < b if b < 1 else p <= b)
        if mask.sum():
            out.append(
                {
                    "bin": f"{a:.1f}-{b:.1f}",
                    "n": int(mask.sum()),
                    "predicted": float(p[mask].mean()),
                    "observed": float(y[mask].mean()),
                }
            )
    return out


def expected_calibration_error(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    table = reliability(p, y, bins)
    n = len(y)
    return float(sum(r["n"] / n * abs(r["predicted"] - r["observed"]) for r in table)) if n else 0.0


def evaluate(p: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    return {
        "n": int(len(y)),
        "brier": brier(p, y),
        "log_loss": log_loss(p, y),
        "ece": expected_calibration_error(p, y),
        "base_rate": float(y.mean()) if len(y) else None,
        "mean_prediction": float(p.mean()) if len(p) else None,
        "reliability": reliability(p, y),
    }


@dataclass
class BaseRateModel:
    kind: str = "base_rate"
    rates: dict[str, float] = field(default_factory=dict)
    prior_weight: float = 20.0

    def fit(self, df: pd.DataFrame) -> BaseRateModel:
        for strat, g in df.groupby("strategy"):
            p0 = float((1 / (1 + g["reward_risk"])).mean())
            self.rates[str(strat)] = float(
                (g["label"].sum() + self.prior_weight * p0) / (len(g) + self.prior_weight)
            )
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        p0 = 1 / (1 + df["reward_risk"].to_numpy(float))
        return np.array([self.rates.get(s, q) for s, q in zip(df["strategy"], p0, strict=True)])

    def to_params(self) -> dict[str, Any]:
        return {"rates": self.rates, "prior_weight": self.prior_weight}

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> BaseRateModel:
        return cls(rates=dict(params["rates"]), prior_weight=float(params["prior_weight"]))


@dataclass
class LogisticModel:
    kind: str = "logistic"
    features: list[str] = field(default_factory=lambda: list(MODEL_FEATURES))
    strategies: list[str] = field(default_factory=list)
    coef: list[float] = field(default_factory=list)
    intercept: float = 0.0
    mean: list[float] = field(default_factory=list)
    scale: list[float] = field(default_factory=list)
    C: float = 0.5

    def _design(self, df: pd.DataFrame) -> np.ndarray:
        x = df[self.features].to_numpy(float)
        onehot = (
            np.column_stack([(df["strategy"] == s).to_numpy(float) for s in self.strategies])
            if self.strategies
            else np.empty((len(df), 0))
        )
        return np.column_stack([x, onehot])

    def fit(self, df: pd.DataFrame) -> LogisticModel:
        from sklearn.linear_model import LogisticRegression  # optionale Abhängigkeit

        self.strategies = sorted(df["strategy"].unique())
        x = self._design(df)
        self.mean = x.mean(axis=0).tolist()
        self.scale = np.where(x.std(axis=0) > 0, x.std(axis=0), 1.0).tolist()
        z = (x - np.array(self.mean)) / np.array(self.scale)
        clf = LogisticRegression(C=self.C, max_iter=2000)
        clf.fit(z, df["label"].to_numpy(int))
        self.coef, self.intercept = clf.coef_[0].tolist(), float(clf.intercept_[0])
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        z = (self._design(df) - np.array(self.mean)) / np.array(self.scale)
        return 1 / (1 + np.exp(-(z @ np.array(self.coef) + self.intercept)))

    def to_params(self) -> dict[str, Any]:
        return {
            "features": self.features,
            "strategies": self.strategies,
            "coef": self.coef,
            "intercept": self.intercept,
            "mean": self.mean,
            "scale": self.scale,
            "C": self.C,
        }

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> LogisticModel:
        return cls(
            features=list(params["features"]),
            strategies=list(params["strategies"]),
            coef=list(params["coef"]),
            intercept=float(params["intercept"]),
            mean=list(params["mean"]),
            scale=list(params["scale"]),
            C=float(params["C"]),
        )


def model_from_record(kind: str, params: dict[str, Any]) -> BaseRateModel | LogisticModel:
    if kind == "base_rate":
        return BaseRateModel.from_params(params)
    if kind == "logistic":
        return LogisticModel.from_params(params)
    raise ValueError(f"Unbekannter Modelltyp {kind}")
