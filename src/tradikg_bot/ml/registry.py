"""Modellregister (SQLite): Versionen, Trainingsdaten, Metriken, Prognosen und Ergebnisse.

Zu jedem Modell werden Version, Trainingszeitpunkt, Datenbereiche, Features,
Parameter und Testmetriken gespeichert. Jede verwendete Prognose wird mit dem
späteren tatsächlichen Ergebnis verknüpft, damit die Kalibrierung im Betrieb
überprüfbar bleibt.

Statuswerte: ``challenger`` -> ``champion`` (nur über ``champion.decide``) ->
``retired``. Es gibt immer höchstens einen Champion.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from tradikg_bot.ml.models import BaseRateModel, LogisticModel, model_from_record

_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    version TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL,
    train_start TEXT, train_end TEXT, test_start TEXT, test_end TEXT,
    features_json TEXT NOT NULL, params_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
    status TEXT NOT NULL, notes TEXT
);
CREATE TABLE IF NOT EXISTS model_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, version TEXT NOT NULL,
    event TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, model_version TEXT NOT NULL, symbol TEXT NOT NULL,
    strategy TEXT NOT NULL, signal_ts TEXT NOT NULL, p REAL NOT NULL, outcome INTEGER,
    outcome_ts TEXT, UNIQUE(model_version, symbol, strategy, signal_ts)
);
"""


@dataclass(frozen=True)
class ModelRecord:
    version: str
    kind: str
    created_at: str
    train_start: str | None
    train_end: str | None
    test_start: str | None
    test_end: str | None
    features: list[str]
    params: dict[str, Any]
    metrics: dict[str, Any]
    status: str
    notes: str | None

    def load(self) -> BaseRateModel | LogisticModel:
        return model_from_record(self.kind, self.params)


class ModelRegistry:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _event(self, version: str, event: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO model_events(ts, version, event, detail) VALUES (?,?,?,?)",
            (pd.Timestamp.now(tz="UTC").isoformat(), version, event, detail),
        )

    def register(
        self,
        model: BaseRateModel | LogisticModel,
        metrics: dict[str, Any],
        ranges: dict[str, str | None],
        features: list[str],
        notes: str = "",
    ) -> str:
        version = f"{model.kind}-{pd.Timestamp.now(tz='UTC'):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
        self._conn.execute(
            "INSERT INTO models VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version,
                model.kind,
                pd.Timestamp.now(tz="UTC").isoformat(),
                ranges.get("train_start"),
                ranges.get("train_end"),
                ranges.get("test_start"),
                ranges.get("test_end"),
                json.dumps(features),
                json.dumps(model.to_params()),
                json.dumps(metrics, default=str),
                "challenger",
                notes,
            ),
        )
        self._event(version, "registered", notes)
        return version

    def get(self, version: str) -> ModelRecord:
        row = self._conn.execute("SELECT * FROM models WHERE version = ?", (version,)).fetchone()
        if row is None:
            raise KeyError(version)
        return self._record(row)

    @staticmethod
    def _record(row: tuple) -> ModelRecord:
        return ModelRecord(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            json.loads(row[7]),
            json.loads(row[8]),
            json.loads(row[9]),
            row[10],
            row[11],
        )

    def champion(self) -> ModelRecord | None:
        row = self._conn.execute("SELECT * FROM models WHERE status = 'champion'").fetchone()
        return self._record(row) if row else None

    def models(self) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT version, kind, created_at, train_start, train_end, test_start, test_end, "
            "status, metrics_json FROM models ORDER BY created_at DESC",
            self._conn,
        )

    def events(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM model_events ORDER BY id DESC", self._conn)

    def set_champion(self, version: str, reason: str) -> None:
        self.get(version)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("UPDATE models SET status = 'retired' WHERE status = 'champion'")
            self._conn.execute(
                "UPDATE models SET status = 'champion' WHERE version = ?", (version,)
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._event(version, "promoted", reason)

    def reject(self, version: str, reason: str) -> None:
        self._conn.execute(
            "UPDATE models SET status = 'retired' WHERE version = ? AND status = 'challenger'",
            (version,),
        )
        self._event(version, "rejected", reason)

    # -- Prognosen & Ergebnisse --------------------------------------------------------------
    def record_prediction(
        self, version: str, symbol: str, strategy: str, signal_ts: str, p: float
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO predictions(model_version, symbol, strategy, signal_ts, p) "
            "VALUES (?,?,?,?,?)",
            (version, symbol, strategy, signal_ts, p),
        )

    def record_outcome(
        self,
        version: str,
        symbol: str,
        strategy: str,
        signal_ts: str,
        outcome: bool,
        outcome_ts: str,
    ) -> None:
        self._conn.execute(
            "UPDATE predictions SET outcome = ?, outcome_ts = ? WHERE model_version = ? AND "
            "symbol = ? AND strategy = ? AND signal_ts = ?",
            (int(outcome), outcome_ts, version, symbol, strategy, signal_ts),
        )

    def predictions(self, version: str | None = None) -> pd.DataFrame:
        q, params = "SELECT * FROM predictions", ()
        if version:
            q, params = q + " WHERE model_version = ?", (version,)
        return pd.read_sql_query(q, self._conn, params=params)
