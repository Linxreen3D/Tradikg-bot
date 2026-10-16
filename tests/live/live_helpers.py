from __future__ import annotations

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.executor import LiveExecutor

# Offizielles Beispiel aus der Kraken-Dokumentation (kein echter Schlüssel)
DOC_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
)


# Erwartete Signatur aus demselben offiziellen Dokumentationsbeispiel
DOC_SIGNATURE = (
    "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def make_executor(gateway, journal, **cfg) -> LiveExecutor:
    clock = FakeClock()
    config = LiveRiskConfig(**{"confirm_timeout_s": 5.0, "poll_interval_s": 1.0, **cfg})
    return LiveExecutor(gateway, journal, config, clock=clock, sleep=clock.sleep)
