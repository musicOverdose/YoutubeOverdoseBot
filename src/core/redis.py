from typing import Optional
import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from src.core.config import settings

_redis_client: Optional[aioredis.Redis] = None


def get_redis_client() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
            socket_timeout=20.0,
            socket_connect_timeout=10.0,
            retry_on_timeout=True,
            retry=Retry(ExponentialBackoff(cap=3, base=0.2), 3),
        )
    return _redis_client


async def close_redis_client() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None

