import asyncio
import html
import time
from typing import Dict, List, Optional
from aiogram import Bot
from src.core.logger import setup_logger

logger = setup_logger("worker_notifier")


def render_progress_bar(pct: float, width: int = 16) -> str:
    """
    Renders a clean modern progress bar:
    ██████████░░░░░░ 62%
    """
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"{bar} {clamped:.0f}%"


class StatusNotifier:
    render_progress_bar = staticmethod(render_progress_bar)

    def __init__(self, bot: Bot, requests: List[dict]):
        self.bot = bot
        self.requests = requests  # list of {"chat_id": int, "message_id": int}
        self.last_update_time: float = 0.0
        self.min_update_interval: float = 1.5  # Seconds between Telegram message edits
        self.last_text: str = ""

    async def update_download_progress(
        self,
        stage_icon: str,
        stage_name: str,
        pct: float,
        speed: str = "",
        eta: str = "",
        extra: str = "",
        force: bool = False,
    ) -> None:
        """
        Renders real download progress with percentage bar, speed, and ETA:
        ⬇️ <b>Downloading video</b>
        ████████████░░░░ 75%
        12.4 MB/s • ETA 00:08
        """
        bar_line = render_progress_bar(pct)
        detail_lines = [bar_line]
        stats = []
        if speed:
            stats.append(speed)
        if eta:
            stats.append(f"ETA {eta}")
        if stats:
            detail_lines.append(" • ".join(stats))
        if extra:
            detail_lines.append(extra)

        await self.update(
            stage_icon=stage_icon,
            stage_name=stage_name,
            detail="\n".join(detail_lines),
            force=force,
        )

    async def update_unified_progress(
        self,
        pct: float,
        speed: str = "",
        eta: str = "",
        subtitle: str = "",
        force: bool = False,
    ) -> None:
        """
        Renders a clean unified progress bar without exposing internal pipeline steps:
        ⚡️ <b>Please wait...</b>
        ██████████░░░░░░ 65%
        12.4 MB/s • ETA 00:05
        """
        bar_line = render_progress_bar(pct)
        lines = [bar_line]
        stats = []
        if speed:
            stats.append(speed)
        if eta:
            stats.append(f"ETA {eta}")
        if stats:
            lines.append(" • ".join(stats))
        elif subtitle:
            lines.append(subtitle)

        detail_text = "\n".join(lines)
        await self.update(
            stage_icon="⚡️",
            stage_name="Please wait...",
            detail=detail_text,
            force=force,
        )

    async def update(
        self,
        stage_icon: str,
        stage_name: str,
        detail: str = "",
        force: bool = False,
    ) -> None:
        """
        Throttled edit of subscriber status messages.
        Avoids Telegram flood limits while keeping UX responsive.
        """
        now = time.time()
        if not force and (now - self.last_update_time < self.min_update_interval):
            return

        text = f"{stage_icon} <b>{stage_name}</b>\n"
        if detail:
            text += f"{html.escape(detail)}\n"

        if text == self.last_text and not force:
            return

        self.last_update_time = now
        self.last_text = text

        for req in self.requests:
            chat_id = req.get("chat_id")
            message_id = req.get("message_id")
            if not chat_id or not message_id:
                continue

            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                )
            except Exception:
                # Common when text hasn't changed or message was deleted
                pass
