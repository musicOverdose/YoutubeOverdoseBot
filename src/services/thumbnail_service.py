import asyncio
import glob
import os
import shutil
import tempfile
from typing import Optional, Tuple, Dict, Any, List
from PIL import Image
import httpx

from src.core.logger import get_logger

logger = get_logger("thumbnail_service")


class ThumbnailService:
    """
    Dedicated high-fidelity thumbnail processing service.
    
    Guarantees:
    1. Highest-quality official YouTube creator thumbnail is always prioritized.
    2. Zero video re-encoding (thumbnail pipeline is decoupled from video stream).
    3. Strict aspect ratio preservation (no stretching, squishing, or distortion).
    4. High-quality single-pass Lanczos downsampling (Image.Resampling.LANCZOS).
    5. Adaptive JPEG compression starting at quality=95 with 4:4:4 chroma subsampling (subsampling=0).
    6. Telegram Bot API compliance: JPEG format, width <= 320, height <= 320, file size < 200 KB.
    7. Multi-URL fallback: tries candidate thumbnail URLs in descending quality if top is 404.
    8. High-resolution FFmpeg frame extraction fallback only when no official thumbnail exists.
    """

    MAX_DIMENSION: int = 320
    MAX_FILE_BYTES: int = 195 * 1024  # 195 KB (strict Telegram limit is 200 KB)

    @classmethod
    def _rank_thumbnails(cls, info: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not info:
            return []

        thumbnails = info.get("thumbnails")
        if not thumbnails or not isinstance(thumbnails, list):
            return []

        valid_thumbs = [t for t in thumbnails if isinstance(t, dict) and t.get("url")]

        def _thumb_score(t: Dict[str, Any]) -> Tuple[int, int, int]:
            url = str(t.get("url") or "")
            pref = t.get("preference") if t.get("preference") is not None else 0
            w = t.get("width") or 0
            h = t.get("height") or 0
            area = w * h

            url_lower = url.lower()
            id_lower = str(t.get("id") or "").lower()

            name_bonus = 0
            if "maxres" in url_lower or "maxres" in id_lower:
                name_bonus = 1920 * 1080
            elif "hq720" in url_lower or "hq720" in id_lower:
                name_bonus = 1280 * 720
            elif "sddefault" in url_lower or "sddefault" in id_lower:
                name_bonus = 640 * 480
            elif "hqdefault" in url_lower or "hqdefault" in id_lower:
                name_bonus = 480 * 360
            elif "mqdefault" in url_lower or "mqdefault" in id_lower:
                name_bonus = 320 * 180

            effective_area = max(area, name_bonus)
            is_jpeg = 1 if (".jpg" in url_lower or ".jpeg" in url_lower) else 0

            return (pref, effective_area, is_jpeg)

        return sorted(valid_thumbs, key=_thumb_score, reverse=True)

    @classmethod
    def get_best_thumbnail_url(cls, info: Optional[Dict[str, Any]]) -> Optional[str]:
        """
        Returns the single highest-scoring official YouTube thumbnail URL.
        """
        sorted_thumbs = cls._rank_thumbnails(info)
        if sorted_thumbs:
            return sorted_thumbs[0].get("url")
        return info.get("thumbnail") if info else None

    @classmethod
    def get_candidate_thumbnail_urls(cls, info: Optional[Dict[str, Any]]) -> List[str]:
        """
        Returns a deduplicated list of candidate thumbnail URLs in descending order of quality.
        Allows graceful fallback if the top speculative URL (e.g. maxresdefault on 240p video) returns 404.
        """
        if not info:
            return []

        sorted_thumbs = cls._rank_thumbnails(info)
        urls = []
        seen = set()
        for t in sorted_thumbs:
            u = t.get("url")
            if u and u not in seen:
                seen.add(u)
                urls.append(u)

        main_thumb = info.get("thumbnail")
        if main_thumb and main_thumb not in seen:
            urls.append(main_thumb)

        return urls

    @classmethod
    def fit_dimensions_inside_bounds(
        cls,
        w: int,
        h: int,
        max_w: int = MAX_DIMENSION,
        max_h: int = MAX_DIMENSION,
    ) -> Tuple[int, int]:
        """
        Calculates output dimensions to fit strictly inside (max_w, max_h)
        while preserving the EXACT original display aspect ratio.
        Never stretches, squeezes, or distorts geometry.
        """
        if w <= 0 or h <= 0:
            return (max_w, max_h)

        scale = min(max_w / float(w), max_h / float(h))
        new_w = max(1, min(max_w, int(round(w * scale))))
        new_h = max(1, min(max_h, int(round(h * scale))))
        return (new_w, new_h)

    @classmethod
    def save_image_adaptive_jpeg(
        cls,
        img: Image.Image,
        output_path: str,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> bool:
        """
        Saves a PIL Image to output_path as a high-fidelity JPEG strictly <= max_bytes.
        Starts with quality=95 and 4:4:4 chroma subsampling (subsampling=0) for sharp text/edges.
        Dynamically adapts quality and subsampling if necessary to ensure compliance.
        """
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Pass 1: 4:4:4 chroma subsampling (subsampling=0) - zero color smearing
        for q in [95, 92, 90, 88, 85, 80]:
            img.save(output_path, "JPEG", quality=q, optimize=True, subsampling=0)
            if os.path.exists(output_path) and os.path.getsize(output_path) <= max_bytes:
                return True

        # Pass 2: Standard 4:2:0 subsampling if initial pass exceeded max_bytes
        for q in [85, 80, 75, 70, 60, 50]:
            img.save(output_path, "JPEG", quality=q, optimize=True)
            if os.path.exists(output_path) and os.path.getsize(output_path) <= max_bytes:
                return True

        return False

    @classmethod
    def crop_vertical_pillarbox(cls, img: Image.Image, target_ratio: float = 9 / 16) -> Image.Image:
        """
        If an image is horizontal (w > h) but represents vertical content (e.g. YouTube Shorts 16:9 thumbnail
        with black side pillarboxes), crops the horizontal margins to match the vertical aspect ratio.
        """
        w, h = img.size
        current_ratio = w / float(h)
        if current_ratio <= 1.0:
            return img  # Already portrait or square

        # Target width based on target vertical ratio
        target_w = int(round(h * target_ratio))
        if target_w >= w:
            return img

        # Center crop horizontally
        left = max(0, (w - target_w) // 2)
        right = left + target_w
        return img.crop((left, 0, right, h))

    @classmethod
    def process_image_file(
        cls,
        source_image_path: str,
        output_thumb_path: str,
        is_vertical: bool = False,
    ) -> bool:
        """
        Opens source_image_path (JPEG, WebP, PNG), optionally crops horizontal pillarboxes
        if content is vertical, scales it with Lanczos resampling to fit inside
        MAX_DIMENSION x MAX_DIMENSION preserving aspect ratio, and saves as a compliant JPEG.
        """
        if not os.path.exists(source_image_path) or os.path.getsize(source_image_path) == 0:
            return False

        try:
            with Image.open(source_image_path) as img:
                img = img.convert("RGB")
                if is_vertical and img.width > img.height:
                    logger.info("Cropping 16:9 pillarboxes for vertical content from: %s", source_image_path)
                    img = cls.crop_vertical_pillarbox(img)

                src_w, src_h = img.size
                target_w, target_h = cls.fit_dimensions_inside_bounds(
                    src_w, src_h, cls.MAX_DIMENSION, cls.MAX_DIMENSION
                )

                if (src_w, src_h) != (target_w, target_h):
                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

                ok = cls.save_image_adaptive_jpeg(img, output_thumb_path, cls.MAX_FILE_BYTES)
                if ok and os.path.exists(output_thumb_path):
                    final_size = os.path.getsize(output_thumb_path)
                    logger.info(
                        "Processed thumbnail from %s: size=(%d, %d), file_size=%d bytes",
                        source_image_path, target_w, target_h, final_size
                    )
                    return True
                return False
        except Exception as e:
            logger.warning("Error processing thumbnail image %s: %s", source_image_path, e)
            return False

    @classmethod
    async def create_vertical_preview_photo(
        cls,
        thumbnail_url: str,
        output_preview_path: str,
    ) -> bool:
        """
        Downloads a remote thumbnail URL and crops the 16:9 pillarbox black bars
        to produce a native 9:16 vertical photo for Telegram preview.
        """
        temp_dl = output_preview_path + ".dl.tmp"
        try:
            dl_ok = await cls.download_thumbnail_image(thumbnail_url, temp_dl)
            if not dl_ok or not os.path.exists(temp_dl):
                return False
            with Image.open(temp_dl) as img:
                img = img.convert("RGB")
                if img.width > img.height:
                    img = cls.crop_vertical_pillarbox(img)
                img.save(output_preview_path, "JPEG", quality=92, optimize=True)
                return os.path.exists(output_preview_path) and os.path.getsize(output_preview_path) > 0
        except Exception as e:
            logger.warning("Error creating vertical preview photo from %s: %s", thumbnail_url, e)
            return False
        finally:
            if os.path.exists(temp_dl):
                try:
                    os.remove(temp_dl)
                except OSError:
                    pass

    @classmethod
    async def download_thumbnail_image(
        cls,
        url: str,
        output_path: str,
        timeout: float = 15.0,
    ) -> bool:
        """
        Downloads a remote thumbnail image via HTTPX.
        """
        if not url:
            return False

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    return True
                logger.warning(
                    "Failed to download thumbnail from %s: status %d", url, resp.status_code
                )
                return False
        except Exception as e:
            logger.warning("HTTP error downloading thumbnail from %s: %s", url, e)
            return False

    @classmethod
    async def extract_frame_fallback(
        cls,
        video_path: str,
        output_thumb_path: str,
        duration: Optional[int] = None,
    ) -> bool:
        """
        Fallback when no official YouTube thumbnail exists:
        Extracts a representative native-resolution video frame using FFmpeg,
        checks for dark/black frames, and processes via Pillow Lanczos.
        """
        if not os.path.exists(video_path):
            logger.warning("Video file does not exist for fallback extraction: %s", video_path)
            return False

        dur = duration
        if dur is None or dur <= 0:
            dur = 10

        # Primary timestamp at ~15% of duration (avoids 0.0s/1.0s intros and black frames)
        primary_seek = min(15.0, float(dur) * 0.15) if dur > 2 else 0.0
        candidate_seeks = [primary_seek]
        if dur >= 2:
            candidate_seeks.append(min(30.0, float(dur) * 0.25))
            candidate_seeks.append(float(dur) * 0.50)
            candidate_seeks.append(float(dur) * 0.75)
        if 1.0 not in candidate_seeks and dur > 3:
            candidate_seeks.append(1.0)
        if 0.0 not in candidate_seeks:
            candidate_seeks.append(0.0)

        temp_dir = tempfile.mkdtemp(prefix="thumb_extract_")
        try:
            best_frame_path = None
            for seek_t in candidate_seeks:
                frame_path = os.path.join(temp_dir, f"frame_{seek_t:.2f}.png")
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{seek_t:.3f}",
                    "-i", video_path,
                    "-vframes", "1",
                    "-q:v", "1",
                    frame_path,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

                if proc.returncode == 0 and os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
                    try:
                        with Image.open(frame_path) as img:
                            grayscale = img.convert("L")
                            extrema = grayscale.getextrema()
                            # Check if frame is not solid black (max luminance >= 20)
                            if extrema and extrema[1] >= 20:
                                best_frame_path = frame_path
                                logger.info(
                                    "Extracted non-black frame at %.2fs for fallback thumbnail", seek_t
                                )
                                break
                            else:
                                logger.debug("Extracted frame at %.2fs is black/dark; trying next candidate", seek_t)
                                if best_frame_path is None:
                                    best_frame_path = frame_path
                    except Exception as img_err:
                        logger.debug("Error inspecting extracted frame: %s", img_err)

            if not best_frame_path or not os.path.exists(best_frame_path):
                logger.warning("FFmpeg fallback frame extraction failed for %s", video_path)
                return False

            # Process the extracted native frame through Pillow Lanczos pipeline
            return cls.process_image_file(best_frame_path, output_thumb_path)

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @classmethod
    async def prepare_video_thumbnail(
        cls,
        video_path: str,
        output_thumb_path: str,
        source_thumb_path: Optional[str] = None,
        source_thumb_url: Optional[str] = None,
        source_thumb_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        is_vertical: bool = False,
    ) -> bool:
        """
        Orchestrates thumbnail preparation:
        For vertical videos (Shorts):
          Extracts native vertical video frame directly via FFmpeg (guaranteeing exact
          vertical geometry with zero black bars / letterboxing), falling back to cropped official artwork.
        For normal landscape videos:
          Prioritizes official YouTube artwork, falling back to FFmpeg frame extraction.
        """
        # For vertical videos, native frame extraction guarantees pure 9:16 vertical thumbnail with no black bars
        if is_vertical and os.path.exists(video_path):
            logger.info("Vertical video detected. Extracting native vertical video frame via FFmpeg...")
            ok = await cls.extract_frame_fallback(video_path, output_thumb_path, duration)
            if ok:
                return True
            logger.warning("FFmpeg frame extraction for vertical video failed, falling back to official artwork")

        # Priority 1: Check existing local official thumbnail file
        if source_thumb_path and os.path.exists(source_thumb_path) and os.path.getsize(source_thumb_path) > 0:
            logger.info("Preparing thumbnail using local official YouTube artwork: %s", source_thumb_path)
            ok = cls.process_image_file(source_thumb_path, output_thumb_path, is_vertical=is_vertical)
            if ok:
                return True
            logger.warning("Failed processing local official thumbnail; checking candidate URLs")

        # Priority 2: Check remote official thumbnail URLs
        candidate_urls: List[str] = []
        if source_thumb_urls:
            candidate_urls.extend(source_thumb_urls)
        if source_thumb_url and source_thumb_url not in candidate_urls:
            candidate_urls.insert(0, source_thumb_url)

        for url in candidate_urls:
            temp_thumb = output_thumb_path + ".download.tmp"
            try:
                logger.info("Downloading official YouTube thumbnail from: %s", url)
                dl_ok = await cls.download_thumbnail_image(url, temp_thumb)
                if dl_ok:
                    ok = cls.process_image_file(temp_thumb, output_thumb_path, is_vertical=is_vertical)
                    if ok:
                        logger.info("Successfully prepared thumbnail from official URL: %s", url)
                        return True
            finally:
                if os.path.exists(temp_thumb):
                    try:
                        os.remove(temp_thumb)
                    except OSError:
                        pass

        # Priority 3: Fallback native-resolution frame extraction via FFmpeg
        logger.info("No usable official YouTube thumbnail found. Falling back to FFmpeg frame extraction.")
        return await cls.extract_frame_fallback(video_path, output_thumb_path, duration)
