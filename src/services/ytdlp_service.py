import asyncio
import errno
import json
import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar
from src.core.config import settings
from src.core.constants import REDIS_KEY_METADATA_PREFIX
from src.core.logger import setup_logger
from src.core.redis import get_redis_client

logger = setup_logger("ytdlp_service")


def _is_readonly_or_permission_error(exc: Exception) -> bool:
    """Checks if an exception is due to a read-only filesystem or restricted file permissions."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
            return True
        msg = str(exc).lower()
        if "read-only" in msg or "permission denied" in msg or "operation not permitted" in msg:
            return True
    return False


# Monkey-patch yt-dlp cookie saving to gracefully handle read-only cookie files/filesystems
# (e.g., in Docker containers where cookie secrets are mounted :ro to protect credentials from mutation).
_orig_cookiejar_save = YoutubeDLCookieJar.save


def _safe_cookiejar_save(self, filename=None, *args, **kwargs):
    try:
        return _orig_cookiejar_save(self, filename, *args, **kwargs)
    except OSError as e:
        if _is_readonly_or_permission_error(e):
            logger.debug(
                "Suppressed cookiejar save on read-only cookiefile (%s): %s",
                filename or getattr(self, "filename", None),
                e,
            )
            return None
        logger.warning(
            "Suppressed unexpected cookiejar save error on cookiefile (%s): %s",
            filename or getattr(self, "filename", None),
            e,
        )
        return None
    except Exception as e:
        logger.warning("Suppressed unexpected error during cookiejar save: %s", e)
        return None


YoutubeDLCookieJar.save = _safe_cookiejar_save

_orig_ydl_save_cookies = yt_dlp.YoutubeDL.save_cookies


def _safe_ydl_save_cookies(self):
    try:
        return _orig_ydl_save_cookies(self)
    except OSError as e:
        if _is_readonly_or_permission_error(e):
            logger.debug("Suppressed save_cookies on read-only cookiefile: %s", e)
            return None
        logger.warning("Suppressed unexpected save_cookies error on cookiefile: %s", e)
        return None
    except Exception as e:
        logger.warning("Suppressed unexpected error during save_cookies: %s", e)
        return None


yt_dlp.YoutubeDL.save_cookies = _safe_ydl_save_cookies


class YouTubeSubtitleRateLimitError(Exception):
    """Raised when YouTube HTTP 429 rate limit persists across all retries."""
    pass


def is_http_429_error(exc: Exception) -> bool:
    """Checks whether the exception is specifically an HTTP 429 Too Many Requests error."""
    msg = str(exc).lower()
    return "429" in msg and (
        "too many requests" in msg
        or "http error 429" in msg
        or "http error: 429" in msg
        or "status 429" in msg
    )


SUBTITLE_COOLDOWN_KEY = "ytdlp:subtitles:cooldown"
_in_memory_subtitle_cooldown_until: float = 0.0


async def get_subtitle_cooldown_remaining() -> float:
    """Returns remaining seconds of global subtitle cooldown, or 0.0 if not active."""
    global _in_memory_subtitle_cooldown_until
    try:
        r = get_redis_client()
        ttl = await r.ttl(SUBTITLE_COOLDOWN_KEY)
        if ttl and ttl > 0:
            return float(ttl)
    except Exception:
        pass
    now = time.monotonic()
    rem = _in_memory_subtitle_cooldown_until - now
    return max(0.0, rem)


async def set_subtitle_cooldown(cooldown_seconds: int = 30) -> None:
    """Sets a short global cooldown after encountering HTTP 429 to protect the IP."""
    global _in_memory_subtitle_cooldown_until
    try:
        r = get_redis_client()
        await r.set(SUBTITLE_COOLDOWN_KEY, "1", ex=cooldown_seconds)
    except Exception:
        pass
    now = time.monotonic()
    _in_memory_subtitle_cooldown_until = max(_in_memory_subtitle_cooldown_until, now + cooldown_seconds)


# Standard YouTube URL regex patterns
YOUTUBE_URL_REGEX = re.compile(
    r"^(https?://)?(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)([\w-]{11})([^\s]*)?$",
    re.IGNORECASE,
)


def extract_youtube_id(url: str) -> Optional[str]:
    match = YOUTUBE_URL_REGEX.match(url.strip())
    if match:
        return match.group(5)
    return None


def get_canonical_url(source_id: str) -> str:
    return f"https://www.youtube.com/watch?v={source_id}"


_metadata_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_METADATA_CACHE_TTL: float = 300.0  # 5 minutes


class YtDlpService:
    @staticmethod
    def get_base_opts(cookies_file: Optional[str] = None) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "no_color": True,
            "ignoreerrors": False,
            "noplaylist": not settings.PLAYLISTS_ENABLED,
            "socket_timeout": 30,
        }

        active_cookie_file = cookies_file or settings.YTDLP_COOKIES_FILE
        if settings.YTDLP_COOKIES_ENABLED and active_cookie_file and os.path.exists(active_cookie_file):
            opts["cookiefile"] = active_cookie_file

        if settings.YTDLP_PROXY:
            opts["proxy"] = settings.YTDLP_PROXY

        return opts

    @classmethod
    async def extract_metadata(
        cls, url: str, cookies_file: Optional[str] = None, use_cache: bool = True
    ) -> Dict[str, Any]:
        """Extract full metadata without downloading, using L1 in-memory and L2 Redis cache when available."""
        source_id = extract_youtube_id(url)
        now = time.monotonic()

        # 1. Check L1 in-memory cache
        if use_cache and source_id and source_id in _metadata_cache:
            ts, cached_info = _metadata_cache[source_id]
            if now - ts < _METADATA_CACHE_TTL:
                return cached_info

        # 2. Check L2 Redis cache (cross-process cache between bot and worker)
        if use_cache and source_id:
            try:
                r = get_redis_client()
                redis_key = f"{REDIS_KEY_METADATA_PREFIX}{source_id}"
                cached_json = await r.get(redis_key)
                if cached_json:
                    info = json.loads(cached_json)
                    _metadata_cache[source_id] = (now, info)
                    logger.debug("L2 Redis metadata cache hit for %s", source_id)
                    return info
            except Exception as e:
                logger.debug("Error checking Redis metadata cache for %s: %s", source_id, e)

        # 3. Extract via yt-dlp in executor (non-blocking)
        loop = asyncio.get_running_loop()
        opts = cls.get_base_opts(cookies_file)

        def _extract() -> Dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await loop.run_in_executor(None, _extract)

        if source_id and info:
            # Update L1 in-memory cache
            if len(_metadata_cache) > 50:
                expired = [k for k, (t, _) in _metadata_cache.items() if now - t >= _METADATA_CACHE_TTL]
                for k in expired:
                    _metadata_cache.pop(k, None)
            _metadata_cache[source_id] = (now, info)

            # Update L2 Redis cache with size safety protection (max 512 KB payload)
            try:
                r = get_redis_client()
                redis_key = f"{REDIS_KEY_METADATA_PREFIX}{source_id}"

                serialized = json.dumps(info)
                if len(serialized) > 512 * 1024:
                    # Prune verbose format dump to essential fields to protect VPS RAM
                    pruned_info = dict(info)
                    if "formats" in pruned_info:
                        essential_fields = (
                            "format_id", "vcodec", "acodec", "height", "width",
                            "ext", "filesize", "filesize_approx", "fps", "format_note", "url", "tbr", "abr", "vbr"
                        )
                        pruned_info["formats"] = [
                            {k: f.get(k) for k in essential_fields if k in f}
                            for f in pruned_info["formats"]
                        ]
                    serialized = json.dumps(pruned_info)

                if len(serialized) <= 512 * 1024:
                    await r.set(redis_key, serialized, ex=300)
                    logger.debug("Stored metadata in L2 Redis cache for %s (%d bytes)", source_id, len(serialized))
                else:
                    logger.debug("Pruned metadata for %s still exceeds 512 KB (%d bytes), skipping Redis cache", source_id, len(serialized))
            except Exception as e:
                logger.debug("Failed to store metadata in Redis cache: %s", e)

        return info


    @classmethod
    def get_available_resolutions(cls, info: Dict[str, Any]) -> List[int]:
        """
        Inspect yt-dlp format metadata and extract actual unique available video heights.
        Sorts descending: e.g. [2160, 1440, 1080, 720, 480, 360, 240, 144].
        """
        formats = info.get("formats", [])
        heights = set()

        for f in formats:
            # Must have a video stream
            vcodec = f.get("vcodec")
            if not vcodec or vcodec == "none":
                continue

            height = f.get("height")
            if height and isinstance(height, int) and height > 0:
                heights.add(height)

        sorted_heights = sorted(list(heights), reverse=True)
        return sorted_heights

    @classmethod
    def get_available_codecs_for_height(cls, info: Dict[str, Any], target_height: int) -> List[str]:
        """
        Inspects yt-dlp format metadata for the exact target_height.
        Returns a list of available codecs from ["H264", "H265"] that actually exist at that height.
        Recognizes:
        - H.264: avc1*, h264*
        - H.265/HEVC: hev1*, hvc1*, hevc*, h265*
        Formats with vcodec == 'none' or non-matching heights are ignored.
        """
        formats = info.get("formats", [])
        codecs = set()

        for f in formats:
            h = f.get("height")
            if h != target_height:
                continue

            vcodec = f.get("vcodec")
            if not vcodec or vcodec == "none":
                continue

            vcodec_lower = str(vcodec).lower()
            if vcodec_lower.startswith("avc1") or vcodec_lower.startswith("h264"):
                codecs.add("H264")
            elif (
                vcodec_lower.startswith("hev1")
                or vcodec_lower.startswith("hvc1")
                or vcodec_lower.startswith("hevc")
                or vcodec_lower.startswith("h265")
            ):
                codecs.add("H265")

        res = []
        if "H264" in codecs:
            res.append("H264")
        if "H265" in codecs:
            res.append("H265")
        return res

    @classmethod
    def auto_select_codec(cls, info: Dict[str, Any], target_height: int) -> str:
        """
        Automatically selects the optimal available video codec for target_height.
        Prioritizes H.264 (AVC) for instant stream-copying and universal compatibility.
        Falls back to H.265 (HEVC) if available, or default H264.
        """
        codecs = cls.get_available_codecs_for_height(info, target_height)
        if "H264" in codecs:
            return "H264"
        if "H265" in codecs:
            return "H265"
        return "H264"

    @classmethod
    def get_resolution_labels(cls, info: Dict[str, Any]) -> Dict[int, str]:
        """
        Maps each available format height to a friendly display label.
        For vertical formats (height > width): uses width as the resolution label (e.g. 1920 -> '1080p').
        For horizontal formats (width >= height): uses height (e.g. 1080 -> '1080p').
        """
        formats = info.get("formats", [])
        labels: Dict[int, str] = {}
        for f in formats:
            vcodec = f.get("vcodec")
            if not vcodec or vcodec == "none":
                continue
            h = f.get("height")
            w = f.get("width")
            if h and isinstance(h, int) and h > 0:
                if w and isinstance(w, int) and w > 0 and h > w:
                    labels[h] = f"{w}p"
                elif h not in labels:
                    labels[h] = f"{h}p"
        return labels

    @classmethod
    def check_english_subtitles(cls, info: Dict[str, Any]) -> Tuple[bool, Optional[str], bool]:
        """
        Returns (has_english, track_key, is_auto_generated).
        Normalizes en, en-US, en-GB, en-orig, etc.
        """
        # 1. Check manual subtitles first
        subtitles = info.get("subtitles") or {}
        for key in subtitles:
            if key == "en" or key.startswith("en-") or key.startswith("en_"):
                return True, key, False

        # 2. Check automatic captions
        auto_caps = info.get("automatic_captions") or {}
        for key in auto_caps:
            if key == "en" or key.startswith("en-") or key.startswith("en_"):
                return True, key, True

        return False, None, False

    @classmethod
    def find_persian_subtitles(cls, info: Dict[str, Any]) -> Tuple[bool, Optional[str], bool]:
        """
        Returns (has_persian, track_key, is_auto_generated).
        Priority:
        1. Check manual subtitles first (fa, fa-*, per, fas)
        2. Check automatic captions (fa, fa-*, per, fas)
        """
        def _is_persian(k: str) -> bool:
            k_lower = k.lower()
            return (
                k_lower in ("fa", "per", "fas")
                or k_lower.startswith(("fa-", "fa_", "per-", "per_"))
            )

        # 1. Check manual subtitles first
        subtitles = info.get("subtitles") or {}
        for key in subtitles:
            if _is_persian(key):
                return True, key, False

        # 2. Check automatic captions
        auto_caps = info.get("automatic_captions") or {}
        for key in auto_caps:
            if _is_persian(key):
                return True, key, True

        return False, None, False

    @classmethod
    def validate_duration(
        cls, info: Dict[str, Any], max_duration_seconds: int = 0, allow_unknown: bool = True
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Duration validation is retired in favor of pre-download file size limit.
        Always returns is_valid=True so long videos are never rejected based on duration.
        """
        duration = info.get("duration")
        dur_int = int(duration) if duration is not None and duration > 0 else None
        return True, dur_int, None

    @staticmethod
    def format_duration(seconds: int) -> str:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def format_file_size(num_bytes: int) -> str:
        """Formats byte count to human-readable string (e.g. 684 MB, 2.14 GB)."""
        if num_bytes >= 1024 * 1024 * 1024:
            val = num_bytes / (1024 ** 3)
            return f"{val:.2f} GB"
        elif num_bytes >= 1024 * 1024:
            val = num_bytes / (1024 ** 2)
            if val >= 10:
                return f"{int(round(val))} MB"
            return f"{val:.1f} MB"
        elif num_bytes >= 1024:
            return f"{int(round(num_bytes / 1024))} KB"
        return f"{num_bytes} B"

    @staticmethod
    def format_mb(mb: int) -> str:
        """Formats megabyte count to human-readable string (e.g. 1900 -> 1.9 GB, 48 -> 48 MB)."""
        if mb >= 1000:
            val = mb / 1000.0
            return f"{val:g} GB"
        return f"{mb} MB"

    @classmethod
    def calculate_expected_video_size(
        cls,
        info: Dict[str, Any],
        target_height: int,
        target_codec: str,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        Simulates yt-dlp's exact format selector to determine the exact video and audio
        formats that will be selected during real download, and calculates the expected
        final output file size (in bytes).

        Returns: (expected_final_bytes, size_source, details)
        size_source is one of: "exact", "approximate", "estimated".
        """
        format_spec = cls.build_video_format_spec(target_height, target_codec)
        ydl_opts = cls.get_base_opts()
        ydl_opts["format"] = format_spec

        # Perform no-download format selection using yt-dlp
        clean_info = dict(info)
        clean_info.setdefault("id", "video")
        clean_info.setdefault("title", "video")
        clean_info.setdefault("extractor", "youtube")
        clean_info.setdefault("extractor_key", "Youtube")
        clean_info["formats"] = [
            dict(f, url=f.get("url", "https://example.com/stream"))
            for f in info.get("formats", [])
        ]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                selected = ydl.process_video_result(clean_info, download=False)
            except Exception as e:
                logger.error(f"yt-dlp format simulation failed for {target_height}p {target_codec}: {e}")
                selected = None

        if not selected:
            return 0, "estimated", {"error": "Format selection failed"}

        # Extract selected video and audio formats
        requested_formats = selected.get("requested_formats")
        if requested_formats and len(requested_formats) >= 2:
            v_fmt = next(
                (f for f in requested_formats if f.get("vcodec") and f.get("vcodec") != "none"),
                requested_formats[0],
            )
            a_fmt = next(
                (f for f in requested_formats if f.get("acodec") and f.get("acodec") != "none"),
                requested_formats[1],
            )
        elif requested_formats and len(requested_formats) == 1:
            v_fmt = requested_formats[0]
            a_fmt = (
                requested_formats[0]
                if (requested_formats[0].get("acodec") and requested_formats[0].get("acodec") != "none")
                else None
            )
        else:
            v_fmt = selected
            a_fmt = (
                selected
                if (selected.get("acodec") and selected.get("acodec") != "none")
                else None
            )

        orig_formats = {str(f.get("format_id")): f for f in info.get("formats", [])}
        orig_v = orig_formats.get(str(v_fmt.get("format_id")), v_fmt) if v_fmt else {}
        orig_a = orig_formats.get(str(a_fmt.get("format_id")), a_fmt) if a_fmt else {}

        duration = float(info.get("duration") or selected.get("duration") or 0.0)

        # 1. Video Size Calculation
        v_size = 0
        v_source = "estimated"
        if orig_v.get("filesize") and orig_v["filesize"] > 0:
            v_size = int(orig_v["filesize"])
            v_source = "exact"
        elif orig_v.get("filesize_approx") and orig_v["filesize_approx"] > 0:
            v_size = int(orig_v["filesize_approx"])
            v_source = "approximate"
        elif v_fmt:
            if v_fmt.get("filesize") and v_fmt["filesize"] > 0:
                v_size = int(v_fmt["filesize"])
                v_source = "exact"
            elif v_fmt.get("filesize_approx") and v_fmt["filesize_approx"] > 0:
                v_size = int(v_fmt["filesize_approx"])
                v_source = "estimated"
            else:
                v_bitrate = v_fmt.get("vbr") or v_fmt.get("tbr") or 0.0
                if v_bitrate > 0 and duration > 0:
                    v_size = int((float(v_bitrate) * 1000 / 8) * duration)
                else:
                    v_size = 0
                v_source = "estimated"

        # 2. Audio Size Calculation
        a_size = 0
        a_source = "exact"
        audio_copied = True
        a_codec_name = (a_fmt.get("acodec") or "").lower() if a_fmt else "none"

        # Check if audio is AAC (or combined format where v_fmt == a_fmt)
        if a_fmt is None or (v_fmt is a_fmt and a_fmt.get("acodec") != "none"):
            # Combined format: entire stream size accounted for in v_size
            a_size = 0
            a_source = v_source
            audio_copied = True
        else:
            is_aac = (
                a_codec_name.startswith("mp4a")
                or a_codec_name.startswith("aac")
                or (a_fmt.get("ext") == "m4a" and "opus" not in a_codec_name)
            )
            if is_aac:
                # AAC: stream-copied (-c:a copy)
                audio_copied = True
                if orig_a.get("filesize") and orig_a["filesize"] > 0:
                    a_size = int(orig_a["filesize"])
                    a_source = "exact"
                elif orig_a.get("filesize_approx") and orig_a["filesize_approx"] > 0:
                    a_size = int(orig_a["filesize_approx"])
                    a_source = "approximate"
                elif a_fmt.get("filesize") and a_fmt["filesize"] > 0:
                    a_size = int(a_fmt["filesize"])
                    a_source = "exact"
                elif a_fmt.get("filesize_approx") and a_fmt["filesize_approx"] > 0:
                    a_size = int(a_fmt["filesize_approx"])
                    a_source = "estimated"
                else:
                    a_bitrate = a_fmt.get("abr") or a_fmt.get("tbr") or 128.0
                    if a_bitrate > 0 and duration > 0:
                        a_size = int((float(a_bitrate) * 1000 / 8) * duration)
                    else:
                        a_size = int((128 * 1000 / 8) * duration)
                    a_source = "estimated"
            else:
                # Non-AAC: transcoded to AAC 192 kbps (-c:a aac -b:a 192k)
                audio_copied = False
                if duration > 0:
                    a_size = int((192000 / 8) * duration)
                else:
                    a_size = int(a_fmt.get("filesize") or a_fmt.get("filesize_approx") or 0)
                a_source = "estimated"

        # 3. Container & metadata overhead (~0.5%)
        overhead_bytes = int((v_size + a_size) * 0.005)
        expected_final_size = v_size + a_size + overhead_bytes

        # 4. Overall size_source resolution
        if v_source == "exact" and a_source == "exact":
            overall_source = "exact"
        elif v_source == "estimated" or a_source == "estimated":
            overall_source = "estimated"
        else:
            overall_source = "approximate"

        details = {
            "video_format_id": v_fmt.get("format_id") if v_fmt else None,
            "audio_format_id": a_fmt.get("format_id") if a_fmt else None,
            "video_codec": v_fmt.get("vcodec") if v_fmt else None,
            "audio_codec": a_fmt.get("acodec") if a_fmt else None,
            "video_size": v_size,
            "audio_final_size": a_size,
            "overhead_bytes": overhead_bytes,
            "expected_final_size": expected_final_size,
            "size_source": overall_source,
            "audio_copied": audio_copied,
            "duration": duration,
        }

        return expected_final_size, overall_source, details

    @classmethod
    def build_video_format_spec(cls, target_height: int, target_codec: Optional[str] = None) -> str:
        """
        STRICT EXACT QUALITY & CODEC:
        Ensures height == target_height AND video codec matches target_codec.
        - H264: vcodec~='(?i)^(avc1|h264)'
        - H265: vcodec~='(?i)^(hev1|hvc1|hevc|h265)'
        Prefers compatible AAC audio (ext=m4a) for zero-conversion muxing,
        falling back to best audio.
        NEVER falls back to a different video codec or resolution!
        """
        if not target_codec:
            return f"bestvideo[height={target_height}]+bestaudio/best[height={target_height}]"

        codec_upper = target_codec.upper()
        if codec_upper == "H264":
            vfilter = "vcodec~='(?i)^(avc1|h264)'"
        elif codec_upper == "H265":
            vfilter = "vcodec~='(?i)^(hev1|hvc1|hevc|h265)'"
        else:
            raise ValueError(f"Unsupported target codec: {target_codec}")

        return (
            f"bestvideo[height={target_height}][{vfilter}]+bestaudio[ext=m4a]/"
            f"bestvideo[height={target_height}][{vfilter}]+bestaudio/"
            f"best[height={target_height}][{vfilter}]"
        )

    @classmethod
    async def download_subtitles(
        cls,
        url: str,
        output_template: str,
        cookies_file: Optional[str] = None,
        on_retry: Optional[Callable[[int, float, Exception], Any]] = None,
        langs: Optional[List[str]] = None,
    ) -> None:
        """
        Downloads subtitles with conservative backoff specifically for HTTP 429:
        - Attempt 1: wait ~10s (+jitter)
        - Attempt 2: wait ~30s (+jitter)
        - Attempt 3: wait ~60s (+jitter)
        Only retries on HTTP 429. Unrelated failures raise immediately.
        Respects global subtitle cooldown across concurrent worker jobs.
        """
        # 1. Check & respect global subtitle cooldown if another job recently hit 429
        cd_rem = await get_subtitle_cooldown_remaining()
        if cd_rem > 0:
            logger.info("Global subtitle cooldown active (%0.1fs remaining), waiting before attempt...", cd_rem)
            await asyncio.sleep(cd_rem)

        backoff_delays = [10.0, 30.0, 60.0]
        max_retries = len(backoff_delays)

        opts = cls.get_base_opts(cookies_file)
        opts.update({
            "outtmpl": output_template,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": langs or ["en.*", "en"],
            "subtitlesformat": "srt/vtt/best",
        })

        loop = asyncio.get_running_loop()

        for attempt in range(max_retries + 1):
            try:
                def _dl():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])

                await loop.run_in_executor(None, _dl)
                logger.info("Subtitles downloaded successfully for %s on attempt %d", url, attempt + 1)
                return
            except Exception as e:
                # ONLY retry if error is specifically HTTP 429
                if not is_http_429_error(e):
                    logger.error("Subtitle download failed with non-429 error: %s", e)
                    raise

                logger.warning(
                    "YouTube subtitle download encountered HTTP 429 on attempt %d/%d: %s",
                    attempt + 1,
                    max_retries + 1,
                    e,
                )

                # Set global cooldown so concurrent jobs/workers back off this IP
                await set_subtitle_cooldown(cooldown_seconds=30)

                if attempt >= max_retries:
                    raise YouTubeSubtitleRateLimitError(
                        "YouTube rate-limited subtitle extraction (HTTP 429: Too Many Requests) across all retries. "
                        "Please try again later or configure YouTube Cookies or a Proxy in the Admin Panel."
                    ) from e

                base_delay = backoff_delays[attempt]
                jitter = random.uniform(0.5, 3.0)
                wait_time = base_delay + jitter

                logger.info(
                    "Waiting %0.1fs (base=%0.1fs, jitter=%0.1fs) before subtitle retry %d/%d",
                    wait_time,
                    base_delay,
                    jitter,
                    attempt + 1,
                    max_retries,
                )

                if on_retry:
                    try:
                        res = on_retry(attempt + 1, wait_time, e)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as cb_err:
                        logger.warning("on_retry callback failed: %s", cb_err)

                await asyncio.sleep(wait_time)
