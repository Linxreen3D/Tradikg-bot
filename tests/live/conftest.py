from __future__ import annotations

import pytest

from tradikg_bot.live.journal import ExecutionJournal
from tradikg_bot.live.paper import PaperExchange

from helpers import MECHANICS_FEES


@pytest.fixture
def journal(tmp_path):
    j = ExecutionJournal(tmp_path / "journal.sqlite")
    yield j
    j.close()


@pytest.fixture
def paper():
    ex = PaperExchange(native_bracket=True, fees=MECHANICS_FEES)
    ex.set_book("XBTEUR", 60000.0, 60010.0)
    return ex
