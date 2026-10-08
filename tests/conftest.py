import asyncio
from typing import AsyncGenerator
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from src.core.database import Base
from src.models import *
from tests.mock_redis import MockRedis

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=False,
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
async def init_test_db(monkeypatch):
    # Patch Redis globally for tests
    mock_r = MockRedis()
    monkeypatch.setattr("src.core.redis.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.queue_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.worker.recovery.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.web.routes.system_routes.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.setting_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.system_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.worker.processor.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.worker.telegram_factory.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.ytdlp_service.get_redis_client", lambda: mock_r)

    # Patch database engine across modules
    monkeypatch.setattr("src.core.database.engine", test_engine)
    monkeypatch.setattr("src.core.database.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("src.web.app.engine", test_engine)
    monkeypatch.setattr("src.services.setting_service.engine", test_engine)
    monkeypatch.setattr("src.services.setting_service.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("src.services.auth_service.engine", test_engine)
    monkeypatch.setattr("src.services.auth_service.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("src.bot.handlers.base.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("src.worker.processor.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("src.worker.main.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("src.bot.handlers.must_join_handler.AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr("src.bot.handlers.url_handler.AsyncSessionLocal", TestSessionLocal)

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()
