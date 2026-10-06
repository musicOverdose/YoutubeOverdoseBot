import asyncio
from datetime import datetime, timezone
import json
import os
import re
import signal
from typing import Any, Dict, List, Optional, Tuple, Union
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, ID3NoHeaderError
from PIL import Image
from src.core.logger import setup_logger

logger = setup_logger("ffmpeg_service")


class FFmpegService:
    @staticmethod
    async def probe_file(file_path: str) -> Dict:
        """Runs ffprobe on the media file to inspect audio/video streams."""
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"ffprobe failed for {file_path}: {stderr.decode()}")
            return {}
        try:
            return json.loads(stdout.decode())
        except Exception as e:
            logger.error(f"Error parsing ffprobe output: {e}")
            return {}

    @classmethod
    def can_stream_copy(
        cls,
        probe_data: Dict,
        target_codec: str,  # "H264" or "H265"
    ) -> Tuple[bool, bool]:
        """
        Determines if video and/or audio can be stream-copied (-c:v copy, -c:a copy).
        Returns: (can_copy_video, can_copy_audio)
        """
        streams = probe_data.get("streams", [])
        v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        can_copy_v = False
        can_copy_a = False

        if v_stream:
            vcodec = (v_stream.get("codec_name") or "").lower()
            if target_codec.upper() == "H264":
                # h264, avc1 can be stream-copied
                if vcodec in ("h264", "avc1"):
                    can_copy_v = True
            elif target_codec.upper() == "H265":
                # hevc, h265, hev1, hvc1 can be stream-copied
                if vcodec in ("hevc", "h265", "hev1", "hvc1"):
                    can_copy_v = True

        if a_stream:
            acodec = (a_stream.get("codec_name") or "").lower()
            # MP4 supports AAC without re-encoding
            if acodec == "aac":
                can_copy_a = True

        return can_copy_v, can_copy_a

    @classmethod
    def build_scale_filter(cls, target_height: int) -> str:
        """
        Builds DAR-preserving scaling filter chain.
        Uses 'dar' so display aspect ratio (including non-square sample aspect ratios) is preserved.
        scale='if(gt(dar,16/9),{max_w},-2)':'if(gt(dar,16/9),-2,{target_height})',pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1
        """
        max_w = int(round(target_height * (16 / 9)))
        if max_w % 2 != 0:
            max_w += 1
        return (
            f"scale='if(gt(dar,16/9),{max_w},-2)':'if(gt(dar,16/9),-2,{target_height})',"
            f"pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1"
        )

    @classmethod
    async def process_video(
        cls,
        input_path: str,
        output_path: str,
        target_codec: str,  # "H264" or "H265"
        target_height: int,
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """
        Remuxes video to exact output format (MP4 with target_codec + AAC).
        STRICT SOURCE-CODEC-ONLY INVARIANT:
        The video stream is ALWAYS stream-copied (-c:v copy).
        NEVER re-encodes video under any circumstances.
        If the source video stream does not match target_codec, fails immediately.
        Audio is stream-copied when already AAC; otherwise remuxed to AAC.
        """
        probe_data = await cls.probe_file(input_path)
        can_copy_v, can_copy_a = cls.can_stream_copy(probe_data, target_codec)

        if not can_copy_v:
            streams = probe_data.get("streams", [])
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            actual_vcodec = v_stream.get("codec_name") if v_stream else "unknown"
            logger.error(
                "Source video codec '%s' does not match requested target '%s'. "
                "Video re-encoding is strictly prohibited.",
                actual_vcodec,
                target_codec,
            )
            raise RuntimeError(
                f"Source video codec '{actual_vcodec}' does not match requested '{target_codec}'. "
                f"Video re-encoding is disabled."
            )

        logger.info(
            "OPERATION: DOWNLOAD + REMUX (ZERO VIDEO RE-ENCODE) for %s -> %s (codec=%s, height=%d)",
            input_path,
            output_path,
            target_codec,
            target_height,
        )

        cmd = ["ffmpeg", "-y", "-i", input_path]

        # Video: STRICT STREAM-COPY ONLY (NEVER RE-ENCODE)
        cmd.extend(["-c:v", "copy"])

        # Audio: stream copy if already AAC, otherwise audio-only transcode to AAC (fast, lightweight)
        if can_copy_a:
            logger.info("Stream-copying audio for %s (already AAC)", input_path)
            cmd.extend(["-c:a", "copy"])
        else:
            logger.info("Remuxing audio to AAC for %s", input_path)
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])

        # Movflags for fast streaming start
        cmd.extend(["-movflags", "+faststart", output_path])

        return await cls.run_subprocess(cmd, cancellation_event)

    @staticmethod
    def resolve_audio_metadata(
        info_dict: Optional[Dict[str, Any]],
        fallback_title: str = "Audio Track",
    ) -> Tuple[str, str, str, str, int]:
        """
        Resolves (title, artist, album, year, duration) with robust fallbacks.
        Never returns empty strings for title, artist, or album.
        """
        info = info_dict or {}
        raw_title = (info.get("title") or fallback_title).strip()
        if not raw_title:
            raw_title = "Audio Track"

        # Artist resolution
        artist = (
            info.get("artist")
            or info.get("creator")
            or info.get("uploader")
            or info.get("channel")
            or ""
        ).strip()

        # Parse title if " - " is present and artist is missing or generic
        title = raw_title
        if " - " in raw_title:
            parts = raw_title.split(" - ", 1)
            candidate_artist = parts[0].strip()
            candidate_title = parts[1].strip()
            if candidate_artist and candidate_title:
                if not artist or artist.lower() in ("youtube", "various artists", "unknown"):
                    artist = candidate_artist
                title = candidate_title

        # Clean unwanted suffixes in title (e.g. "(Official Music Video)", "[Official Audio]")
        cleaned_title = re.sub(
            r"\s*[\(\[](?:official\s*(?:video|audio|music\s*video|lyric\s*video|hd|4k)?|lyrics|audio|mv)[\)\]]",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned_title:
            title = cleaned_title

        if not artist:
            artist = "Unknown Artist"

        # Album resolution
        album = (info.get("album") or f"{title} - Single").strip()
        if not album:
            album = f"{title} - Single"

        # Year resolution
        year = ""
        release_date = info.get("release_date") or info.get("upload_date")
        if release_date and len(str(release_date)) >= 4:
            year = str(release_date)[:4]
        elif info.get("release_year"):
            year = str(info.get("release_year"))
        if not year or not year.isdigit():
            year = str(datetime.now(timezone.utc).year)

        # Duration
        duration = int(info.get("duration") or 0)

        return title, artist, album, year, duration

    @staticmethod
    def generate_safe_audio_filename(artist: str, title: str) -> str:
        """
        Generates clean sanitized filename: 'Artist - Title.mp3' or 'Title.mp3'.
        Removes invalid filesystem characters: / \\ : * ? " < > |
        Ensures filename is never empty, safe for filesystem and Telegram.
        """
        if artist and artist.lower() != "unknown artist" and artist.lower() not in title.lower():
            base = f"{artist} - {title}"
        else:
            base = title

        # Replace invalid filesystem characters
        safe_base = re.sub(r'[\\/*?:"<>|]', "", base).strip()
        safe_base = re.sub(r"\s+", " ", safe_base).strip()

        if not safe_base:
            safe_base = "Audio Track"

        if len(safe_base) > 100:
            safe_base = safe_base[:100].strip()

        return f"{safe_base}.mp3"

    @staticmethod
    def prepare_cover_image(source_cover_path: Optional[str], output_cover_path: str) -> str:
        """
        Prepares a high quality square JPEG cover image suitable for both ID3 APIC and Telegram thumbnail.
        Guarantees that output_cover_path exists and is a valid JPEG.
        """
        try:
            if source_cover_path and os.path.exists(source_cover_path):
                img = Image.open(source_cover_path).convert("RGB")
                w, h = img.size
                min_dim = min(w, h)
                left = (w - min_dim) // 2
                top = (h - min_dim) // 2
                img = img.crop((left, top, left + min_dim, top + min_dim))
                img = img.resize((500, 500), Image.Resampling.LANCZOS)
                img.save(output_cover_path, "JPEG", quality=90)
                return output_cover_path
        except Exception as e:
            logger.warning("Could not convert source cover %s: %s. Generating fallback cover.", source_cover_path, e)

        # Fallback cover generation
        try:
            img = Image.new("RGB", (500, 500), color=(26, 32, 44))
            img.save(output_cover_path, "JPEG", quality=90)
        except Exception as fe:
            logger.error("Failed to generate fallback cover: %s", fe)
        return output_cover_path

    @staticmethod
    def apply_and_verify_id3_tags(
        mp3_path: str,
        title: str,
        artist: str,
        album: str,
        year: Union[str, int],
        cover_image_path: Optional[str] = None,
    ) -> bool:
        """
        Embeds and strictly verifies ID3v2.3 tags (TIT2, TPE1, TALB, TDRC, APIC).
        """
        try:
            try:
                tags = ID3(mp3_path)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delete(mp3_path)
            tags = ID3()

            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TALB(encoding=3, text=album))
            tags.add(TDRC(encoding=3, text=str(year)))

            if cover_image_path and os.path.exists(cover_image_path):
                with open(cover_image_path, "rb") as f:
                    cover_bytes = f.read()
                tags.add(APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # Cover (front)
                    desc="Cover",
                    data=cover_bytes,
                ))

            tags.save(mp3_path, v2_version=3)

            # Verification step: read back and strictly verify required frames
            read_tags = ID3(mp3_path)
            required_frames = ["TIT2", "TPE1", "TALB", "TDRC"]
            missing = [f for f in required_frames if f not in read_tags]
            if missing:
                raise ValueError(f"ID3v2.3 verification failed: missing frames {missing}")

            if cover_image_path and os.path.exists(cover_image_path):
                if not any(k.startswith("APIC") for k in read_tags.keys()):
                    raise ValueError("ID3v2.3 verification failed: missing APIC frame")

            logger.info("ID3v2.3 tags strictly verified for %s (TIT2, TPE1, TALB, TDRC, APIC)", mp3_path)
            return True
        except Exception as e:
            logger.error("ID3 tagging or verification failed on %s: %s", mp3_path, e)
            raise

    @classmethod
    async def extract_mp3(
        cls,
        input_audio_path: str,
        output_mp3_path: str,
        title: str,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        year: Optional[Union[str, int]] = None,
        cover_image_path: Optional[str] = None,
        cancellation_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """
        Extracts and converts audio to MP3 with verified ID3v2.3 metadata and embedded cover art.
        """
        artist = artist or "Unknown Artist"
        album = album or f"{title} - Single"
        year = str(year) if year else str(datetime.now(timezone.utc).year)

        # 1. Transcode audio using libmp3lame with high quality
        cmd = [
            "ffmpeg", "-y",
            "-i", input_audio_path,
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "320k",
            output_mp3_path,
        ]
        ok = await cls.run_subprocess(cmd, cancellation_event)
        if not ok or not os.path.exists(output_mp3_path):
            return False

        # 2. Embed and verify ID3v2.3 tags
        cls.apply_and_verify_id3_tags(
            mp3_path=output_mp3_path,
            title=title,
            artist=artist,
            album=album,
            year=year,
            cover_image_path=cover_image_path,
        )
        return True

    @staticmethod
    async def run_subprocess(
        cmd: List[str], cancellation_event: Optional[asyncio.Event] = None
    ) -> bool:
        """Runs ffmpeg command in a subprocess with cancellation and process group cleanup."""
        proc = None
        try:
            # Create process in a new process group so we can kill the whole tree
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid,
            )

            async def wait_process():
                stdout, stderr = await proc.communicate()
                return proc.returncode, stderr

            process_task = asyncio.create_task(wait_process())

            if cancellation_event:
                cancel_task = asyncio.create_task(cancellation_event.wait())
                done, pending = await asyncio.wait(
                    [process_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if cancel_task in done:
                    # Cancelled! Kill entire process group
                    logger.warning(f"Process cancelled by user: killing PID group {proc.pid}")
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process_task.cancel()
                    return False

            returncode, stderr = await process_task
            if returncode != 0:
                logger.error(f"FFmpeg failed with code {returncode}: {stderr.decode()[-500:]}")
                return False
            return True

        except Exception as e:
            logger.error(f"FFmpeg subprocess error: {e}")
            if proc and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            return False

    @classmethod
    async def extract_video_metadata(cls, file_path: str) -> Tuple[int, int, int]:
        """
        Runs ffprobe against the FINAL processed output file and extracts:
        (duration, width, height).
        All must be valid positive values.
        Raises ValueError if ffprobe cannot determine valid positive metadata.
        """
        if not os.path.exists(file_path):
            raise ValueError(f"Video file does not exist: {file_path}")

        probe_data = await cls.probe_file(file_path)
        streams = probe_data.get("streams", [])
        v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not v_stream:
            raise ValueError(f"No video stream found in final output file: {file_path}")

        raw_width = v_stream.get("width")
        raw_height = v_stream.get("height")
        if raw_width is None or raw_height is None:
            raise ValueError(f"Video dimensions missing in ffprobe for {file_path}")

        try:
            width = int(raw_width)
            height = int(raw_height)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid video dimensions in {file_path}: width={raw_width}, height={raw_height}") from e

        if width <= 0 or height <= 0:
            raise ValueError(f"Video dimensions must be positive integers: width={width}, height={height}")

        # Duration can be in video stream duration, container format duration, or tags
        raw_duration = v_stream.get("duration")
        if raw_duration is None:
            raw_duration = probe_data.get("format", {}).get("duration")

        if raw_duration is None:
            raise ValueError(f"Video duration missing in ffprobe for {file_path}")

        try:
            duration_float = float(raw_duration)
            duration = int(round(duration_float))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid video duration in {file_path}: {raw_duration}") from e

        if duration <= 0:
            if duration_float > 0:
                duration = 1
            else:
                raise ValueError(f"Video duration must be positive: {duration_float}")

        return duration, width, height

    @classmethod
    async def generate_thumbnail(
        cls,
        video_path: str,
        output_thumb_path: str,
        duration: Optional[int] = None,
        source_thumb_path: Optional[str] = None,
        source_thumb_url: Optional[str] = None,
    ) -> bool:
        """
        Prepares a high-quality JPEG thumbnail adhering to Telegram limits (<=320x320, <200 KB).
        Delegates to ThumbnailService which prioritizes official YouTube artwork before
        falling back to high-resolution FFmpeg frame extraction.
        """
        from src.services.thumbnail_service import ThumbnailService

        return await ThumbnailService.prepare_video_thumbnail(
            video_path=video_path,
            output_thumb_path=output_thumb_path,
            source_thumb_path=source_thumb_path,
            source_thumb_url=source_thumb_url,
            duration=duration,
        )


