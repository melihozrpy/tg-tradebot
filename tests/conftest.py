from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("MARKET_DATA_PROVIDER", "mock")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy.orm import sessionmaker

from app.config.settings import get_strategy_config
from app.data.mock_provider import MockMarketDataProvider
from app.models.database import Base, build_engine


@pytest.fixture()
def strategy_config():
    return get_strategy_config()


@pytest.fixture()
def mock_provider():
    return MockMarketDataProvider()


@pytest.fixture()
def db_session():
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()
