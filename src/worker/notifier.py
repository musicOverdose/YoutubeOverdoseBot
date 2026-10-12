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
        self.min_update_interval: float = 1.2  # Seconds between Telegram message edits
        self.last_text: str = ""
        self.current_pct: float = 5.0
        self.target_pct: float = 5.0
        self.current_speed: str = ""
        self.current_eta: str = ""
        self.current_stage: str = "download"  # "download", "process", "upload"
        self._ticker_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def start_ticker(self, initial_pct: float = 5.0) -> None:
        """Starts a background ticker task for smooth, continuous progress animation."""
        if self._ticker_task and not self._ticker_task.done():
            return
        self.current_pct = max(1.0, initial_pct)
        self.target_pct = self.current_pct
        self._ticker_task = asyncio.create_task(self._run_ticker())

    async def stop_ticker(self) -> None:
        """Cancels and cleanly stops the background progress ticker."""
        if self._ticker_task and not self._ticker_task.done():
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._ticker_task = None

    def set_stage(self, stage: str) -> None:
        """
        Transitions the ticker stage:
        - 'download': spans 5% -> 65% (speed & ETA shown)
        - 'process': spans 65% -> 88% (speed & ETA cleared)
        - 'upload': spans 88% -> 97% (speed & ETA cleared)
        """
        self.current_stage = stage
        if stage in ("process", "upload"):
            self.current_speed = ""
            self.current_eta = ""
        if stage == "process":
            self.target_pct = max(self.target_pct, 88.0)
            self.current_pct = max(self.current_pct, 65.0)
        elif stage == "upload":
            self.target_pct = max(self.target_pct, 97.0)
            self.current_pct = max(self.current_pct, 90.0)

    def on_download_progress(
        self, dl_pct: float, speed: str = "", eta: str = ""
    ) -> None:
        """Hook called during download to update download stats."""
        # Download stage smoothly scales up to 65%
        self.target_pct = max(self.target_pct, min(65.0, 5.0 + (dl_pct * 0.60)))
        if speed:
            self.current_speed = speed
        if eta:
            self.current_eta = eta

    async def _run_ticker(self) -> None:
        try:
            # Immediate initial display
            await self.update_unified_progress(self.current_pct, force=True)
            while True:
                await asyncio.sleep(1.2)
                async with self._lock:
                    if self.current_stage == "download":
                        if self.target_pct > self.current_pct:
                            step = max(1.5, (self.target_pct - self.current_pct) * 0.45)
                            self.current_pct = min(self.target_pct, self.current_pct + step)
                        elif self.current_pct < 62.0:
                            # Gently advance so progress never appears stuck
                            self.current_pct += 1.5
                    elif self.current_stage == "process":
                        if self.current_pct < 88.0:
                            step = max(1.0, (88.0 - self.current_pct) * 0.3)
                            self.current_pct = min(88.0, self.current_pct + step)
                    elif self.current_stage == "upload":
                        if self.current_pct < 97.0:
                            step = max(0.8, (97.0 - self.current_pct) * 0.35)
                            self.current_pct = min(97.0, self.current_pct + step)

                    pct = self.current_pct
                    spd = self.current_speed
                    eta = self.current_eta

                await self.update_unified_progress(
                    pct=pct,
                    speed=spd,
                    eta=eta,
                    force=False,
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Progress ticker encountered error: %s", e)

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
        if force:
            self.current_pct = pct
            self.target_pct = max(self.target_pct, pct)

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
