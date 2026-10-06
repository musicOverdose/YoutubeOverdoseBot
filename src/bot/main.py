import asyncio
from pathlib import Path
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from src.bot.handlers.audio_handler import audio_router
from src.bot.handlers.base import base_router
from src.bot.handlers.codec_handler import codec_router
from src.bot.handlers.must_join_handler import must_join_router
from src.bot.handlers.quality_handler import quality_router
from src.bot.handlers.queue_handler import queue_router
from src.bot.handlers.subtitle_handler import subtitle_router
from src.bot.handlers.url_handler import url_router
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.setting import Setting
from src.services.setting_service import SettingService

logger = setup_logger("bot_main")


def setup_handlers(dp: Dispatcher) -> None:
    dp.include_router(base_router)
    dp.include_router(url_router)
    dp.include_router(quality_router)
    dp.include_router(codec_router)
    dp.include_router(audio_router)
    dp.include_router(subtitle_router)
    dp.include_router(queue_router)
    dp.include_router(must_join_router)


def read_runtime_token() -> Optional[str]:
    """
    Read bot token strictly from /config/runtime/bot-token.
    Zero fallback to settings.BOT_TOKEN or .env credentials.
    """
    token_path = Path(settings.RUNTIME_BOT_TOKEN_FILE)
    if token_path.is_file():
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception as e:
            logger.warning("Failed to read %s: %s", token_path, e)
    return None


async def read_authoritative_config() -> Tuple[str, str]:
    """
    Read mode and version directly from PostgreSQL (authoritative source).
    Falls back to Redis derived cache if PostgreSQL is temporarily unavailable.
    """
    default_mode = getattr(settings, "TELEGRAM_API_MODE", "cloud")
    try:
        async with AsyncSessionLocal() as session:
            stmt = select(Setting).where(Setting.status == "ACTIVE")
            res = await session.execute(stmt)
            items = {s.key: s.value for s in res.scalars().all()}
            mode = items.get("telegram_api_mode") or default_mode
            version = items.get("telegram_config_version") or "1"
            return mode, version
    except Exception as e:
        logger.warning("Could not read config directly from PostgreSQL: %s. Falling back to Redis mirror.", e)
        try:
            r = get_redis_client()
            m = await r.get("telegram:active:mode")
            v = await r.get("telegram:active:config_version")
            mode = m.decode() if isinstance(m, bytes) else (str(m) if m else default_mode)
            version = v.decode() if isinstance(v, bytes) else (str(v) if v else "1")
            return mode, version
        except Exception:
            return default_mode, "1"


async def wait_for_readiness() -> None:
    """
    Block until /config/state/READY exists on disk.
    READY indicates runtime artifacts correspond to PostgreSQL ACTIVE.
    """
    ready_file = Path(settings.RUNTIME_READY_FILE)
    while not ready_file.is_file():
        logger.info("Waiting for runtime state reconciliation (%s)...", ready_file)
        await asyncio.sleep(1.0)
    logger.info("Runtime readiness confirmed (%s exists).", ready_file)


async def check_bot_connectivity(bot: Bot, mode: str, endpoint: str):
    """
    Perform an explicit getMe check before starting polling.
    Logs sanitized bot details and returns the User object.
    Never exposes the bot token in logs.
    """
    try:
        me = await bot.get_me()
        logger.info(
            "Bot authenticated successfully: @%s (ID: %s, mode: %s, endpoint: %s)",
            me.username,
            me.id,
            mode,
            endpoint,
        )
        return me
    except Exception as e:
        sanitized_err = str(e).replace(bot.token or "", "<REDACTED>")
        logger.error(
            "Bot authentication check (getMe) failed [mode=%s, endpoint=%s]: %s",
            mode,
            endpoint,
            sanitized_err,
        )
        raise


async def main() -> None:
    logger.info("Initializing Telegram Bot Service...")

    dp = Dispatcher()
    setup_handlers(dp)

    # Load persistent application settings from PostgreSQL on bot startup
    try:
        await SettingService.load_public_settings_to_runtime()
    except Exception as e:
        logger.warning("Could not load application settings from DB on bot startup: %s", e)

    reload_event = asyncio.Event()
    current_version: Optional[str] = None

    # Resilient Redis Pub/Sub listener with auto-reconnect
    async def listen_for_reloads():
        while True:
            try:
                r = get_redis_client()
                pubsub = r.pubsub()
                await pubsub.subscribe("telegram:config:reload", "app:config:reload")
                async for message in pubsub.listen():
                    if message and message.get("type") == "message":
                        channel = message.get("channel")
                        if isinstance(channel, bytes):
                            channel = channel.decode("utf-8")
                        if channel == "app:config:reload":
                            logger.info("Application settings reload event received. Updating runtime...")
                            try:
                                await SettingService.load_public_settings_to_runtime()
                            except Exception as e:
                                logger.warning("Failed reloading application settings: %s", e)
                        else:
                            logger.info("Configuration reload event received via Redis. Cycling Bot session...")
                            reload_event.set()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Redis pubsub listener error: %s. Reconnecting in 2s...", e)
                await asyncio.sleep(2.0)

    # Readiness monitor: signals reload immediately when READY is unlinked
    async def monitor_readiness():
        ready_file = Path(settings.RUNTIME_READY_FILE)
        while True:
            try:
                await asyncio.sleep(1.0)
                if not ready_file.is_file():
                    # READY unlinked: halt polling immediately
                    reload_event.set()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # 30-second background PostgreSQL drift heartbeat
    async def drift_heartbeat():
        while True:
            try:
                await asyncio.sleep(30.0)
                ready_file = Path(settings.RUNTIME_READY_FILE)
                if not ready_file.is_file():
                    reload_event.set()
                    continue

                async with AsyncSessionLocal() as session:
                    stmt = select(Setting.value).where(
                        Setting.key == "telegram_config_version", Setting.status == "ACTIVE"
                    )
                    res = await session.execute(stmt)
                    db_version = res.scalar_one_or_none()
                    if db_version and current_version and str(db_version) != str(current_version):
                        logger.info(
                            "Heartbeat drift detected (DB version: %s, local: %s). Cycling bot...",
                            db_version,
                            current_version,
                        )
                        reload_event.set()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Heartbeat check warning: %s", e)

    reload_task = asyncio.create_task(listen_for_reloads())
    readiness_task = asyncio.create_task(monitor_readiness())
    heartbeat_task = asyncio.create_task(drift_heartbeat())

    backoff_delay = 1.0

    try:
        while True:
            reload_event.clear()

            # Gate on READY existence before starting any session
            await wait_for_readiness()

            token = read_runtime_token()
            if not token:
                logger.info("Bot token not configured in %s. Waiting...", settings.RUNTIME_BOT_TOKEN_FILE)
                while not token and not reload_event.is_set():
                    await asyncio.sleep(1.0)
                    token = read_runtime_token()
                if not token:
                    continue

            mode, current_version = await read_authoritative_config()
            endpoint = SettingService.derive_endpoint(mode)
            is_local = (mode == "local")
            logger.info(
                "Configuring Bot with mode: %s, config_version: %s, endpoint: %s",
                mode,
                current_version,
                endpoint,
            )

            session = AiohttpSession(
                api=TelegramAPIServer.from_base(endpoint, is_local=False)
            )

            bot = Bot(
                token=token,
                session=session,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )

            # Explicit safe getMe connectivity check before polling
            try:
                me = await check_bot_connectivity(bot, mode, endpoint)
                backoff_delay = 1.0  # Reset backoff on successful authentication
            except Exception:
                await bot.session.close()
                await asyncio.sleep(backoff_delay)
                backoff_delay = min(backoff_delay * 2, 30.0)
                continue

            polling_start_time = asyncio.get_running_loop().time()
            polling_task = asyncio.create_task(
                dp.start_polling(bot, allowed_updates=["message", "callback_query"])
            )

            waiter = asyncio.create_task(reload_event.wait())
            done, pending = await asyncio.wait(
                [polling_task, waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if polling_task in done:
                exc = polling_task.exception() if not polling_task.cancelled() else None
                if exc:
                    sanitized_exc = str(exc).replace(token, "<REDACTED>")
                    if isinstance(exc, TelegramAPIError):
                        logger.error(
                            "Bot polling stopped with Telegram API error (%s): %s",
                            type(exc).__name__,
                            sanitized_exc,
                            exc_info=True,
                        )
                    else:
                        logger.error(
                            "Bot polling stopped with connection/network error (%s): %s",
                            type(exc).__name__,
                            sanitized_exc,
                            exc_info=True,
                        )
                    waiter.cancel()
                    await bot.session.close()
                    await asyncio.sleep(backoff_delay)
                    backoff_delay = min(backoff_delay * 2, 30.0)
                    continue
                else:
                    logger.warning("Bot polling task finished unexpectedly without exception.")
                    waiter.cancel()
            else:
                logger.info("Halting current polling instance (reload or unreadiness signaled)...")
                polling_task.cancel()
                try:
                    await polling_task
                except (asyncio.CancelledError, Exception):
                    pass

            if asyncio.get_running_loop().time() - polling_start_time > 30.0:
                backoff_delay = 1.0

            await bot.session.close()
            await asyncio.sleep(0.5)

    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Bot service shutdown requested.")
    finally:
        reload_task.cancel()
        readiness_task.cancel()
        heartbeat_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
