from __future__ import annotations

import pytest

from tradikg_bot.live.journal import ExecutionJournal
from tradikg_bot.live.paper import PaperExchange


@pytest.fixture
def journal(tmp_path):
    j = ExecutionJournal(tmp_path / "journal.sqlite")
    yield j
    j.close()


@pytest.fixture
def paper():
    ex = PaperExchange(native_bracket=True)
    ex.set_book("XBTEUR", 60000.0, 60010.0)
    return ex
