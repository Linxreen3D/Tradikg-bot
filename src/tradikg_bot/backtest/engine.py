"""Ereignisgesteuerte Backtesting-Engine (ein Instrument, long-only).

Zeitlicher Ablauf je Kerze ``i`` (Zeitstempel = Öffnungszeit):

1. **Tageswechsel** (UTC): Tages-Verlustwächter mit der Equity der Vorkerze neu setzen.
2. **Eröffnung**: Ausstehende Orders aus Kerze ``i-1`` werden zum Eröffnungskurs
   ausgeführt. Die Positionsgröße wird mit dem Schlusskurs von ``i-1`` als
   Referenz und dem Kapitalstand *vor* der Eröffnung berechnet – also nur mit
   Informationen, die zum Orderzeitpunkt bekannt sind.
3. **Intrabar**: Stop-Loss (Stop-Market) und Take-Profit (Limit) werden anhand von
   ``high``/``low`` geprüft. Kurslücken werden zum (schlechteren bzw. besseren)
   Eröffnungskurs ausgeführt. Werden beide Marken in derselben Kerze berührt, wird
   konservativ der Stop-Loss angenommen.
4. **Schluss**: Bewertung zum Schlusskurs, Prüfung der 10 %-Tagesverlustschwelle.
   Danach werden die Signale der Kerze ``i`` gelesen und als Orders für ``i+1``
   vorgemerkt.

Jede Position hat zwingend Stop-Loss und Take-Profit; Einstiege ohne gültige
Marken werden verworfen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.backtest.lookahead import check_no_lookahead
from tradikg_bot.config import RiskConfig
from tradikg_bot.costs.fees import FeeModel, Fill, OrderSide
from tradikg_bot.data.validation import OHLCV_COLUMNS
from tradikg_bot.domain import ExitReason, Instrument
from tradikg_bot.risk.limits import DailyLossGuard
from tradikg_bot.risk.sizing import SkipReason, size_position, worst_case_loss
from tradikg_bot.strategies.base import Strategy, validate_signals


@dataclass
class Trade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    qty: float
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    gross_pnl: float
    commission: float
    slippage_cost: float
    net_pnl: float
    planned_risk: float
    exit_reason: ExitReason
    bars_held: int
    max_adverse_excursion: float
    max_favorable_excursion: float

    @property
    def r_multiple(self) -> float:
        return self.net_pnl / self.planned_risk if self.planned_risk > 0 else 0.0

    @property
    def total_cost(self) -> float:
        return self.commission + self.slippage_cost

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exit_reason"] = str(self.exit_reason)
        d["r_multiple"] = self.r_multiple
        return d


@dataclass
class SkippedEntry:
    time: pd.Timestamp
    reason: SkipReason


@dataclass
class _OpenPosition:
    entry_index: int
    entry_time: pd.Timestamp
    qty: float
    entry_fill: Fill
    stop_loss: float
    take_profit: float
    planned_risk: float
    max_high: float
    min_low: float


@dataclass
class _PendingEntry:
    ref_price: float
    stop_distance: float
    take_profit_distance: float


@dataclass
class BacktestResult:
    symbol: str
    strategy_name: str
    strategy_params: dict[str, Any]
    initial_capital: float
    trades: list[Trade]
    fills: pd.DataFrame
    equity: pd.DataFrame
    skipped_entries: list[SkippedEntry]
    halted_days: list[Any]
    fee_model: FeeModel
    risk: RiskConfig
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def final_equity(self) -> float:
        return float(self.equity["equity"].iloc[-1])

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])


class BacktestEngine:
    def __init__(
        self,
        instrument: Instrument,
        fee_model: FeeModel,
        risk: RiskConfig | None = None,
        initial_capital: float = 100.0,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital muss > 0 sein")
        self.instrument = instrument
        self.fees = fee_model
        self.risk = risk or RiskConfig()
        self.initial_capital = float(initial_capital)

    def run(
        self,
        bars: pd.DataFrame,
        strategy: Strategy,
        verify_causality: bool = True,
        benchmark_daily_volume_return: float = 0.10,
    ) -> BacktestResult:
        from tradikg_bot.backtest.metrics import compute_metrics

        if len(bars) < 2:
            raise ValueError("Mindestens zwei Kerzen erforderlich")
        if not bars.index.is_monotonic_increasing or bars.index.has_duplicates:
            raise ValueError("Kerzen müssen streng aufsteigend sein (validate_ohlcv nutzen)")
        bars = bars[OHLCV_COLUMNS]
        if verify_causality:
            check_no_lookahead(strategy, bars)
        signals = validate_signals(strategy.generate_signals(bars), bars)

        idx = bars.index
        o = bars["open"].to_numpy(float)
        h = bars["high"].to_numpy(float)
        lo_ = bars["low"].to_numpy(float)
        c = bars["close"].to_numpy(float)
        sig_entry = signals["entry"].to_numpy(bool)
        sig_exit = signals["exit"].to_numpy(bool)
        sig_sd = signals["stop_distance"].to_numpy(float)
        sig_tp = signals["take_profit_distance"].to_numpy(float)

        cash = self.initial_capital
        equity = self.initial_capital
        position: _OpenPosition | None = None
        pending_entry: _PendingEntry | None = None
        pending_exit: ExitReason | None = None
        guard = DailyLossGuard(limit=self.risk.daily_loss_limit)

        trades: list[Trade] = []
        fills: list[dict[str, Any]] = []
        skipped: list[SkippedEntry] = []
        eq_rows: list[tuple[float, float, float]] = []

        def record_fill(i: int, fill: Fill, purpose: str) -> None:
            fills.append(
                {
                    "time": idx[i],
                    "side": str(fill.side),
                    "qty": fill.qty,
                    "reference_price": fill.reference_price,
                    "price": fill.price,
                    "notional": fill.notional,
                    "commission": fill.commission,
                    "slippage_cost": fill.slippage_cost,
                    "purpose": purpose,
                }
            )

        def close_position(i: int, fill: Fill, reason: ExitReason) -> None:
            nonlocal cash, position
            assert position is not None
            cash += fill.cash_flow
            record_fill(i, fill, f"exit:{reason}")
            entry = position.entry_fill
            gross = position.qty * (fill.reference_price - entry.reference_price)
            commission = entry.commission + fill.commission
            slippage = entry.slippage_cost + fill.slippage_cost
            trades.append(
                Trade(
                    symbol=self.instrument.symbol,
                    entry_time=position.entry_time,
                    exit_time=idx[i],
                    qty=position.qty,
                    entry_price=entry.price,
                    exit_price=fill.price,
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    gross_pnl=gross,
                    commission=commission,
                    slippage_cost=slippage,
                    net_pnl=entry.cash_flow + fill.cash_flow,
                    planned_risk=position.planned_risk,
                    exit_reason=reason,
                    bars_held=i - position.entry_index,
                    max_adverse_excursion=min(0.0, position.min_low / entry.price - 1),
                    max_favorable_excursion=max(0.0, position.max_high / entry.price - 1),
                )
            )
            position = None

        n = len(bars)
        for i in range(n):
            # 1) Tageswechsel
            day = idx[i].date()
            if guard.current_day != day:
                guard.start_day(day, equity)

            # 2) Ausführung zur Eröffnung
            if pending_exit is not None and position is not None:
                close_position(
                    i, self.fees.market_fill(OrderSide.SELL, position.qty, o[i]), pending_exit
                )
            pending_exit = None

            if pending_entry is not None and position is None:
                if guard.halted:
                    skipped.append(SkippedEntry(idx[i], SkipReason.DAILY_LOSS_HALT))
                else:
                    decision = size_position(
                        equity=equity,
                        cash=cash,
                        ref_price=pending_entry.ref_price,
                        stop_distance=pending_entry.stop_distance,
                        take_profit_distance=pending_entry.take_profit_distance,
                        risk=self.risk,
                        instrument=self.instrument,
                        fees=self.fees,
                        remaining_daily_loss_budget=guard.remaining_budget(equity),
                    )
                    if not decision.accepted:
                        skipped.append(SkippedEntry(idx[i], decision.skip_reason))
                    else:
                        qty = decision.qty
                        fill = self.fees.market_fill(OrderSide.BUY, qty, o[i])
                        # Kurslücke kann die Order verteuern -> auf verfügbares Kapital kürzen.
                        while -fill.cash_flow > cash and qty > 0:
                            qty = self.instrument.round_qty(qty - self.instrument.qty_step)
                            fill = self.fees.market_fill(OrderSide.BUY, qty, o[i])
                        if qty <= 0 or fill.notional < self.instrument.min_notional:
                            skipped.append(SkippedEntry(idx[i], SkipReason.BELOW_MIN_SIZE))
                        else:
                            cash += fill.cash_flow
                            record_fill(i, fill, "entry")
                            stop = fill.price - pending_entry.stop_distance
                            take = fill.price + pending_entry.take_profit_distance
                            position = _OpenPosition(
                                entry_index=i,
                                entry_time=idx[i],
                                qty=qty,
                                entry_fill=fill,
                                stop_loss=stop,
                                take_profit=take,
                                planned_risk=worst_case_loss(
                                    qty, o[i], pending_entry.stop_distance, self.fees
                                ),
                                max_high=o[i],
                                min_low=o[i],
                            )
            pending_entry = None

            # 3) Intrabar Stop-Loss / Take-Profit
            if position is not None:
                position.max_high = max(position.max_high, h[i])
                position.min_low = min(position.min_low, lo_[i])
                sl, tp = position.stop_loss, position.take_profit
                if o[i] <= sl:
                    close_position(
                        i,
                        self.fees.market_fill(OrderSide.SELL, position.qty, o[i]),
                        ExitReason.STOP_LOSS,
                    )
                elif o[i] >= tp:
                    close_position(
                        i,
                        self.fees.limit_fill(OrderSide.SELL, position.qty, o[i]),
                        ExitReason.TAKE_PROFIT,
                    )
                elif lo_[i] <= sl:  # konservativ: Stop vor Take-Profit
                    close_position(
                        i,
                        self.fees.market_fill(OrderSide.SELL, position.qty, sl),
                        ExitReason.STOP_LOSS,
                    )
                elif h[i] >= tp:
                    close_position(
                        i,
                        self.fees.limit_fill(OrderSide.SELL, position.qty, tp),
                        ExitReason.TAKE_PROFIT,
                    )

            # 4) Bewertung zum Schluss und Tagesverlustschwelle
            pos_qty = position.qty if position is not None else 0.0
            equity = cash + pos_qty * c[i]
            guard.check(equity)
            eq_rows.append((equity, cash, pos_qty))

            if i == n - 1:
                break

            # 5) Signale der abgeschlossenen Kerze i -> Orders für Kerze i+1
            if position is not None:
                if guard.halted:
                    pending_exit = ExitReason.DAILY_LOSS_LIMIT
                elif sig_exit[i]:
                    pending_exit = ExitReason.SIGNAL
            elif sig_entry[i]:
                sd, tpd = sig_sd[i], sig_tp[i]
                if guard.halted:
                    skipped.append(SkippedEntry(idx[i], SkipReason.DAILY_LOSS_HALT))
                elif np.isfinite(sd) and np.isfinite(tpd) and sd > 0 and tpd > 0:
                    pending_entry = _PendingEntry(c[i], sd, tpd)
                else:
                    skipped.append(SkippedEntry(idx[i], SkipReason.INVALID_LEVELS))

        # Offene Position am Datenende zum letzten Schlusskurs glattstellen
        if position is not None:
            close_position(
                n - 1,
                self.fees.market_fill(OrderSide.SELL, position.qty, c[n - 1]),
                ExitReason.END_OF_DATA,
            )
            final_equity = cash
            eq_rows[-1] = (final_equity, cash, 0.0)
            guard.check(final_equity)

        equity_df = pd.DataFrame(eq_rows, index=idx, columns=["equity", "cash", "position_qty"])
        fills_df = pd.DataFrame(
            fills,
            columns=[
                "time",
                "side",
                "qty",
                "reference_price",
                "price",
                "notional",
                "commission",
                "slippage_cost",
                "purpose",
            ],
        )
        result = BacktestResult(
            symbol=self.instrument.symbol,
            strategy_name=strategy.name,
            strategy_params=strategy.params(),
            initial_capital=self.initial_capital,
            trades=trades,
            fills=fills_df,
            equity=equity_df,
            skipped_entries=skipped,
            halted_days=list(guard.halted_days),
            fee_model=self.fees,
            risk=self.risk,
        )
        result.metrics = compute_metrics(
            result, bars, benchmark_daily_volume_return=benchmark_daily_volume_return
        )
        return result
