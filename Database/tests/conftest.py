from __future__ import annotations

import pytest

from market_data_db import DBConfig, MarketDataDB


@pytest.fixture
def db(tmp_path):
    config = DBConfig(root=tmp_path / "lake")
    database = MarketDataDB(config)
    yield database
    database.close()
