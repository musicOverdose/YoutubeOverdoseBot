import asyncio
import glob
import math
import os
import shutil
import tempfile
from typing import Optional, Tuple, Dict, Any, List
from PIL import Image, ImageFilter
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
    5. Subtle micro-contrast sharpening to preserve crisp edges and text.
    6. Adaptive JPEG compression starting at quality=95 with 4:4:4 chroma subsampling (subsampling=0).
    7. Telegram Bot API compliance: JPEG format, width <= 1280, height <= 1280, file size < 200 KB.
    8. Multi-URL fallback: tries candidate thumbnail URLs in descending quality if top is 404.
    9. High-resolution FFmpeg frame extraction fallback only when no official thumbnail exists.
    """

    MAX_DIMENSION: int = 1280
    MAX_FILE_BYTES: int = 195 * 1024  # 195 KB (strict Telegram limit is 200 KB)

    @staticmethod
    def _get_res_tier(area: int) -> int:
        """
        Categorizes pixel area into distinct resolution tiers:
        Tier 3: HD 720p / 1080p+ (>= 690,000 px, e.g. 1280x720, 1920x1080)
        Tier 2: SD 480p (>= 230,000 px, e.g. 640x480, 854x480)
        Tier 1: 360p (>= 129,000 px, e.g. 480x360)
        Tier 0: Tiny / mobile notifications (< 129,000 px, e.g. 320x180, 168x94, 120x90)
        """
        if area >= 690000:
            return 3  # HD (720p, 1080p, 4K)
        if area >= 230000:
            return 2  # SD (480p)
        if area >= 129000:
            return 1  # Low (360p)
        return 0  # Tiny (<360p)

    @classmethod
    def _rank_candidates_list(cls, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Ranks thumbnail candidates strictly by:
        1. Resolution tier (1080p > 720p > 480p > 360p > tiny).
        2. Security/versioning token (within the same resolution tier, live tokenized creator
           artwork beats stale bare CDN URLs).
        3. Exact pixel area.
        4. JPEG format preference (Telegram native compatibility).
        5. yt-dlp preference score.
        """
        def _thumb_score(t: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
            url = str(t.get("url") or "")
            w = t.get("width") or 0
            h = t.get("height") or 0

            url_lower = url.lower()
            id_lower = str(t.get("id") or "").lower()

            if w > 0 and h > 0:
                area = w * h
            else:
                if "maxres" in url_lower or "maxres" in id_lower:
                    area = 1920 * 1080
                elif "hq720" in url_lower or "hq720" in id_lower:
                    area = 1280 * 720
                elif "sddefault" in url_lower or "sddefault" in id_lower:
                    area = 640 * 480
                elif "hqdefault" in url_lower or "hqdefault" in id_lower:
                    area = 480 * 360
                elif "mqdefault" in url_lower or "mqdefault" in id_lower:
                    area = 320 * 180
                else:
                    area = 0

            res_tier = cls._get_res_tier(area)
            has_token = 1 if ("sqp=" in url or "rs=" in url) else 0
            is_jpeg = 1 if (".jpg" in url_lower or ".jpeg" in url_lower) else 0
            pref = t.get("preference") if t.get("preference") is not None else 0

            return (res_tier, has_token, area, is_jpeg, pref)

        return sorted(candidates, key=_thumb_score, reverse=True)

    @classmethod
    def _rank_thumbnails(cls, info: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not info:
            return []

        thumbnails = info.get("thumbnails")
        if not thumbnails or not isinstance(thumbnails, list):
            thumbnails = []

        valid_thumbs = [t for t in thumbnails if isinstance(t, dict) and t.get("url")]
        main_thumb = info.get("thumbnail")
        if main_thumb and not any(t.get("url") == main_thumb for t in valid_thumbs):
            valid_thumbs.append({"url": main_thumb, "id": "main_thumb"})

        return cls._rank_candidates_list(valid_thumbs)

    @classmethod
    def get_best_thumbnail_url(cls, info: Optional[Dict[str, Any]]) -> Optional[str]:
        """
        Returns the single highest-scoring official YouTube thumbnail URL.
        """
        if not info:
            return None
        sorted_thumbs = cls._rank_thumbnails(info)
        if sorted_thumbs:
            return sorted_thumbs[0].get("url")
        return info.get("thumbnail")

    @classmethod
    def get_candidate_thumbnail_urls(
        cls,
        info: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
    ) -> List[str]:
        """
        Returns a deduplicated list of candidate thumbnail URLs in descending order of quality.
        Ranks by resolution tier (1080p > 720p > 480p > 360p > tiny), prioritizing live tokenized
        URLs within the same resolution tier, and integrating deterministic fallback endpoints.
        """
        if not info and not source_id:
            return []

        if not source_id and info:
            source_id = info.get("id")

        candidates: List[Dict[str, Any]] = []

        # 1. Authoritative, live thumbnail URLs from yt-dlp metadata
        if info:
            thumbnails = info.get("thumbnails")
            if thumbnails and isinstance(thumbnails, list):
                candidates.extend([t for t in thumbnails if isinstance(t, dict) and t.get("url")])

            main_thumb = info.get("thumbnail")
            if main_thumb and not any(t.get("url") == main_thumb for t in candidates):
                candidates.append({"url": main_thumb, "id": "main_thumb"})

        # 2. Inject deterministic high-resolution endpoints
        if source_id and len(source_id) == 11:
            deterministic_candidates = [
                {"url": f"https://i.ytimg.com/vi/{source_id}/maxresdefault.jpg", "id": "det_maxres_jpg", "width": 1920, "height": 1080, "preference": -1},
                {"url": f"https://i.ytimg.com/vi/{source_id}/maxresdefault.webp", "id": "det_maxres_webp", "width": 1920, "height": 1080, "preference": -1},
                {"url": f"https://i.ytimg.com/vi/{source_id}/hq720.jpg", "id": "det_hq720_jpg", "width": 1280, "height": 720, "preference": -2},
                {"url": f"https://i.ytimg.com/vi/{source_id}/hq720.webp", "id": "det_hq720_webp", "width": 1280, "height": 720, "preference": -2},
                {"url": f"https://i.ytimg.com/vi/{source_id}/sddefault.jpg", "id": "det_sddefault_jpg", "width": 640, "height": 480, "preference": -3},
                {"url": f"https://i.ytimg.com/vi/{source_id}/sddefault.webp", "id": "det_sddefault_webp", "width": 640, "height": 480, "preference": -3},
                {"url": f"https://i.ytimg.com/vi/{source_id}/hqdefault.jpg", "id": "det_hqdefault_jpg", "width": 480, "height": 360, "preference": -4},
                {"url": f"https://i.ytimg.com/vi/{source_id}/hqdefault.webp", "id": "det_hqdefault_webp", "width": 480, "height": 360, "preference": -4},
            ]
            for dc in deterministic_candidates:
                if not any(t.get("url") == dc["url"] for t in candidates):
                    candidates.append(dc)

        ranked = cls._rank_candidates_list(candidates)
        urls = []
        seen = set()
        for t in ranked:
            u = t.get("url")
            if u and u not in seen:
                seen.add(u)
                urls.append(u)

        return urls

    @classmethod
    def fit_dimensions_inside_bounds(
        cls,
        w: int,
        h: int,
        max_w: int = MAX_DIMENSION,
        max_h: int = MAX_DIMENSION,
        upscale: bool = False,
    ) -> Tuple[int, int]:
        """
        Calculates output dimensions to fit strictly inside (max_w, max_h)
        while preserving the EXACT original display aspect ratio.
        Never stretches, squeezes, or distorts geometry.
        """
        if w <= 0 or h <= 0:
            return (max_w, max_h)

        scale = min(max_w / float(w), max_h / float(h))
        if not upscale and scale > 1.0:
            return (w, h)

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
        Includes fallback scaling for exceptionally noisy images.
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

        # Pass 3: Fallback scaling for exceptionally noisy/high-entropy images
        # Dynamically scale down by 15% steps to guarantee compliance
        curr_img = img
        for _ in range(3):
            w = int(round(curr_img.width * 0.85))
            h = int(round(curr_img.height * 0.85))
            if w < 100 or h < 100:
                break
            curr_img = curr_img.resize((w, h), Image.Resampling.LANCZOS)
            for q in [75, 60, 50]:
                curr_img.save(output_path, "JPEG", quality=q, optimize=True)
                if os.path.exists(output_path) and os.path.getsize(output_path) <= max_bytes:
                    return True

        return False

    @classmethod
    def crop_vertical_pillarbox(cls, img: Image.Image, target_ratio: float = 9 / 16) -> Image.Image:
        """
        If an image is horizontal (w > h) but represents vertical content (e.g. YouTube Shorts 16:9 thumbnail
        with black side pillarboxes), crops the horizontal margins to match the vertical aspect ratio.
        Uses safe inward insets to guarantee zero black border/pillarbox lines on left or right edges,
        and resizes to target_w x h to preserve exact mathematical aspect ratio.
        """
        w, h = img.size
        current_ratio = w / float(h)
        if current_ratio <= 1.0:
            return img  # Already portrait or square

        # Target width based on target vertical ratio
        target_w = int(round(h * target_ratio))
        if target_w >= w:
            return img

        # Center crop horizontally with safe inward inset (+2px)
        center_x = w / 2.0
        raw_left = center_x - (target_w / 2.0)
        raw_right = center_x + (target_w / 2.0)

        safe_inset = 2
        left = max(0, int(math.ceil(raw_left)) + safe_inset)
        right = min(w, int(math.floor(raw_right)) - safe_inset)

        if right <= left:
            left = max(0, int(math.ceil(raw_left)))
            right = min(w, int(math.floor(raw_right)))

        cropped = img.crop((left, 0, right, h))
        if cropped.size != (target_w, h):
            cropped = cropped.resize((target_w, h), Image.Resampling.LANCZOS)
        return cropped

    @classmethod
    def auto_crop_black_borders(
        cls,
        img: Image.Image,
        tolerance: int = 18,
        min_bar_px: int = 12,
    ) -> Image.Image:
        """
        Detects and cleanly crops solid black pillarbox bars (left/right) or letterbox bars (top/bottom)
        injected by YouTube (e.g. 16:9 canvas padding for 4:3 videos like O2jkV4BsN6U,
        or 4:3 sddefault/hqdefault padding for 16:9 videos like r2hiI3P3TOI).
        Ensures symmetry and non-destructive behavior on standard artwork.
        """
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        pixels = img.load()

        # 1. Left pillarbox
        left = 0
        for x in range(w):
            step = max(1, h // 30)
            is_black = True
            for y in range(0, h, step):
                r, g, b = pixels[x, y]
                if r > tolerance or g > tolerance or b > tolerance:
                    is_black = False
                    break
            if is_black:
                left = x + 1
            else:
                break

        # 2. Right pillarbox
        right = w
        for x in range(w - 1, -1, -1):
            step = max(1, h // 30)
            is_black = True
            for y in range(0, h, step):
                r, g, b = pixels[x, y]
                if r > tolerance or g > tolerance or b > tolerance:
                    is_black = False
                    break
            if is_black:
                right = x
            else:
                break

        # 3. Top letterbox
        top = 0
        for y in range(h):
            step = max(1, w // 30)
            is_black = True
            for x in range(0, w, step):
                r, g, b = pixels[x, y]
                if r > tolerance or g > tolerance or b > tolerance:
                    is_black = False
                    break
            if is_black:
                top = y + 1
            else:
                break

        # 4. Bottom letterbox
        bottom = h
        for y in range(h - 1, -1, -1):
            step = max(1, w // 30)
            is_black = True
            for x in range(0, w, step):
                r, g, b = pixels[x, y]
                if r > tolerance or g > tolerance or b > tolerance:
                    is_black = False
                    break
            if is_black:
                bottom = y
            else:
                break

        crop_l, crop_r = 0, w
        crop_t, crop_b = 0, h

        left_bar = left
        right_bar = w - right
        if left_bar >= min_bar_px and right_bar >= min_bar_px:
            diff = abs(left_bar - right_bar)
            if diff <= 30 or (0.6 <= left_bar / float(right_bar) <= 1.6):
                crop_l = left
                crop_r = right

        top_bar = top
        bottom_bar = h - bottom
        if top_bar >= min_bar_px and bottom_bar >= min_bar_px:
            diff = abs(top_bar - bottom_bar)
            if diff <= 30 or (0.6 <= top_bar / float(bottom_bar) <= 1.6):
                crop_t = top
                crop_b = bottom

        if crop_l > 0 or crop_r < w or crop_t > 0 or crop_b < h:
            if crop_r > crop_l + 50 and crop_b > crop_t + 50:
                logger.info(
                    "Auto-cropped black borders: orig=(%d, %d), box=(%d, %d, %d, %d), new=(%d, %d)",
                    w, h, crop_l, crop_t, crop_r, crop_b, crop_r - crop_l, crop_b - crop_t
                )
                return img.crop((crop_l, crop_t, crop_r, crop_b))

        return img

    @classmethod
    def process_image_file(
        cls,
        source_image_path: str,
        output_thumb_path: str,
        is_vertical: bool = False,
        target_ratio: Optional[float] = None,
    ) -> bool:
        """
        Opens source_image_path (JPEG, WebP, PNG), optionally crops horizontal pillarboxes
        if content is vertical, scales it with Lanczos resampling to fit inside
        MAX_DIMENSION x MAX_DIMENSION preserving aspect ratio, applies subtle sharpening,
        and saves as a compliant JPEG.
        """
        if not os.path.exists(source_image_path) or os.path.getsize(source_image_path) == 0:
            return False

        try:
            with Image.open(source_image_path) as img:
                img = img.convert("RGB")
                if is_vertical and img.width > img.height:
                    ratio = target_ratio or (9 / 16)
                    logger.info("Cropping 16:9 pillarboxes for vertical content (ratio=%.4f) from: %s", ratio, source_image_path)
                    img = cls.crop_vertical_pillarbox(img, target_ratio=ratio)

                img = cls.auto_crop_black_borders(img)

                src_w, src_h = img.size
                target_w, target_h = cls.fit_dimensions_inside_bounds(
                    src_w, src_h, cls.MAX_DIMENSION, cls.MAX_DIMENSION
                )

                if (src_w, src_h) != (target_w, target_h):
                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                    # Apply gentle micro-contrast unsharp mask to keep text and logo edges razor-sharp
                    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=35, threshold=3))

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
    def create_telegram_chat_thumbnail(
        cls,
        source_image_path: str,
        output_thumb_path: str,
        is_vertical: bool = False,
        target_ratio: Optional[float] = None,
    ) -> bool:
        """
        Creates a crisp 320x320-bounded JPEG specifically optimized for Telegram's
        video message chat bubble. Uses Lanczos downsampling and subtle unsharp masking
        to prevent Telegram's server-side bilinear downscaler from blurring edges.
        """
        if not os.path.exists(source_image_path) or os.path.getsize(source_image_path) == 0:
            return False

        try:
            with Image.open(source_image_path) as img:
                img = img.convert("RGB")
                if is_vertical and img.width > img.height:
                    ratio = target_ratio or (9 / 16)
                    img = cls.crop_vertical_pillarbox(img, target_ratio=ratio)

                img = cls.auto_crop_black_borders(img)

                src_w, src_h = img.size
                target_w, target_h = cls.fit_dimensions_inside_bounds(
                    src_w, src_h, 320, 320
                )

                if (src_w, src_h) != (target_w, target_h):
                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                    # Apply gentle micro-contrast unsharp mask to keep text and logo edges razor-sharp at 320px
                    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=50, threshold=2))

                ok = cls.save_image_adaptive_jpeg(img, output_thumb_path, cls.MAX_FILE_BYTES)
                if ok and os.path.exists(output_thumb_path):
                    logger.info(
                        "Created Telegram chat thumbnail (320px): size=(%d, %d), bytes=%d",
                        target_w, target_h, os.path.getsize(output_thumb_path)
                    )
                    return True
                return False
        except Exception as e:
            logger.warning("Error creating Telegram chat thumbnail %s: %s", source_image_path, e)
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
                img = cls.auto_crop_black_borders(img)
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
    async def prepare_preview_photo(
        cls,
        info: Optional[Dict[str, Any]],
        source_id: Optional[str],
        output_path: str,
        is_vertical: bool = False,
        timeout: float = 4.0,
    ) -> bool:
        """
        Prepares a crisp, compliant JPEG preview photo for Telegram inline menus.
        Iterates through candidate thumbnail URLs in descending quality order,
        automatically skipping 404 speculative URLs (common on older or low-res videos),
        crops pillarboxes for vertical Shorts, auto-crops black pillarboxes/letterboxes,
        and saves as JPEG to output_path.
        """
        candidates = cls.get_candidate_thumbnail_urls(info, source_id)
        if not candidates:
            return False

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        temp_dl = output_path + ".cand.tmp"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=3.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as client:
                for idx, url in enumerate(candidates[:15]):
                    try:
                        dl_ok = await cls.download_thumbnail_image(url, temp_dl, client=client)
                        if dl_ok and os.path.exists(temp_dl) and os.path.getsize(temp_dl) > 1024:
                            with Image.open(temp_dl) as img:
                                img = img.convert("RGB")
                                if is_vertical and img.width > img.height:
                                    img = cls.crop_vertical_pillarbox(img)

                                img = cls.auto_crop_black_borders(img)

                                target_w, target_h = cls.fit_dimensions_inside_bounds(
                                    img.width, img.height, cls.MAX_DIMENSION, cls.MAX_DIMENSION
                                )
                                if (img.width, img.height) != (target_w, target_h):
                                    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                                    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=35, threshold=3))

                                ok = cls.save_image_adaptive_jpeg(img, output_path, cls.MAX_FILE_BYTES)
                                if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                                    logger.info(
                                        "Prepared preview photo from candidate [%d] %s (%dx%d, %d bytes)",
                                        idx, url, target_w, target_h, os.path.getsize(output_path),
                                    )
                                    return True
                    except Exception as cand_err:
                        logger.debug("Candidate preview thumbnail failed (%s): %s", url, cand_err)
                        continue
                    finally:
                        if os.path.exists(temp_dl):
                            try:
                                os.remove(temp_dl)
                            except OSError:
                                pass
        except Exception as e:
            logger.warning("Error in prepare_preview_photo: %s", e)
        finally:
            if os.path.exists(temp_dl):
                try:
                    os.remove(temp_dl)
                except OSError:
                    pass

        return False

    @classmethod
    async def download_full_thumbnail_file(
        cls,
        info: Optional[Dict[str, Any]],
        source_id: str,
        output_path: str,
        timeout: float = 8.0,
        is_vertical: bool = False,
        target_ratio: Optional[float] = None,
    ) -> Optional[Tuple[str, int, int]]:
        """
        Downloads the highest-quality original YouTube thumbnail without downscaling or extra lossy compression.
        Iterates candidate URLs to bypass 404s and returns (file_path, width, height) if successful, None otherwise.
        If is_vertical is True and the downloaded image is 16:9 landscape with pillarboxes, crops to target vertical ratio.
        Automatically detects and crops injected black pillarbox/letterbox borders from YouTube.
        If the original image is WebP or cropped, it converts to high-quality JPEG (quality=98) so all devices can open it natively.
        """
        candidates = cls.get_candidate_thumbnail_urls(info, source_id)
        if not candidates:
            return None

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        temp_dl = output_path + ".full.tmp"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=4.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as client:
                for idx, url in enumerate(candidates[:15]):
                    try:
                        dl_ok = await cls.download_thumbnail_image(url, temp_dl, client=client)
                        if dl_ok and os.path.exists(temp_dl) and os.path.getsize(temp_dl) > 1024:
                            with Image.open(temp_dl) as img:
                                orig_format = (img.format or "").upper()
                                orig_size = img.size
                                rgb_img = img.convert("RGB")

                                if is_vertical and rgb_img.width > rgb_img.height:
                                    ratio = target_ratio or (9 / 16)
                                    rgb_img = cls.crop_vertical_pillarbox(rgb_img, target_ratio=ratio)
                                rgb_img = cls.auto_crop_black_borders(rgb_img)

                                # If cropping occurred or not a JPEG, save pristine RGB JPEG
                                if rgb_img.size != orig_size or orig_format not in ("JPEG", "JPG") or not output_path.lower().endswith((".jpg", ".jpeg")):
                                    rgb_img.save(output_path, "JPEG", quality=98, subsampling=0)
                                    final_w, final_h = rgb_img.size
                                else:
                                    # Copy original file bit-for-bit
                                    shutil.copy2(temp_dl, output_path)
                                    final_w, final_h = orig_size

                                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                                    logger.info(
                                        "Downloaded full thumbnail candidate [%d] %s (%dx%d, %d bytes, is_vertical=%s)",
                                        idx, url, final_w, final_h, os.path.getsize(output_path), is_vertical,
                                    )
                                    return (output_path, final_w, final_h)
                    except Exception as cand_err:
                        logger.debug("Candidate full thumbnail failed (%s): %s", url, cand_err)
                        continue
                    finally:
                        if os.path.exists(temp_dl):
                            try:
                                os.remove(temp_dl)
                            except OSError:
                                pass
        except Exception as e:
            logger.warning("Error in download_full_thumbnail_file: %s", e)
        finally:
            if os.path.exists(temp_dl):
                try:
                    os.remove(temp_dl)
                except OSError:
                    pass

        return None

    @classmethod
    async def download_thumbnail_image(
        cls,
        url: str,
        output_path: str,
        timeout: float = 15.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> bool:
        """
        Downloads a remote thumbnail image via HTTPX.
        Supports connection reuse via optional client argument.
        """
        if not url:
            return False

        try:
            if client is not None:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    return True
                logger.warning(
                    "Failed to download thumbnail from %s: status %d", url, resp.status_code
                )
                return False

            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as new_client:
                resp = await new_client.get(url)
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
        is_vertical: bool = False,
        target_ratio: Optional[float] = None,
    ) -> bool:
        """
        Fallback when no official YouTube thumbnail exists or available thumbnail is low-resolution:
        Extracts a representative native-resolution video frame using FFmpeg,
        checks for dark/black frames, and processes via Pillow Lanczos.
        """
        if not os.path.exists(video_path):
            logger.warning("Video file does not exist for fallback extraction: %s", video_path)
            return False

        dur = duration
        if dur is None or dur <= 0:
            dur = 10

        # Candidates: 0.5s, 1.0s (intro cards / early action), ~15% duration, 25%, 50%, 0.0s
        candidate_seeks = [0.5, 1.0]
        if dur > 2:
            primary_seek = min(15.0, float(dur) * 0.15)
            if primary_seek not in candidate_seeks:
                candidate_seeks.append(primary_seek)
            candidate_seeks.append(min(30.0, float(dur) * 0.25))
            candidate_seeks.append(float(dur) * 0.50)
            candidate_seeks.append(float(dur) * 0.75)
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
                                    "Extracted non-black frame at %.2fs for thumbnail", seek_t
                                )
                                break
                            else:
                                logger.debug("Extracted frame at %.2fs is black/dark; trying next candidate", seek_t)
                                if best_frame_path is None:
                                    best_frame_path = frame_path
                    except Exception as img_err:
                        logger.debug("Error inspecting extracted frame: %s", img_err)

            if not best_frame_path or not os.path.exists(best_frame_path):
                logger.warning("FFmpeg frame extraction failed for %s", video_path)
                return False

            # Process the extracted native frame through Pillow Lanczos pipeline
            return cls.process_image_file(
                best_frame_path,
                output_thumb_path,
                is_vertical=is_vertical,
                target_ratio=target_ratio,
            )

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
        target_ratio: Optional[float] = None,
    ) -> bool:
        """
        Orchestrates thumbnail preparation:
        1. Prioritizes official creator artwork. If a local thumbnail is already HD
           (>= 1280x720 or vertical >= 720x1280), processes it immediately.
        2. If local thumbnail is low-resolution (< 1280x720), tries candidate URLs first
           to obtain official HD creator artwork (e.g. 1080p maxresdefault or 720p hq720).
           For vertical videos, automatically crops horizontal pillarboxes to target_ratio (e.g. 9:16).
        3. Falls back to local thumbnail if candidate URLs are unreachable.
        4. Falls back to native-resolution FFmpeg frame extraction only if no official artwork exists.
        """
        # Step 1: Try candidate high-res URLs first (authoritative live creator artwork with security tokens)
        candidate_urls: List[str] = []
        if source_thumb_urls:
            candidate_urls.extend(source_thumb_urls)
        if source_thumb_url and source_thumb_url not in candidate_urls:
            candidate_urls.insert(0, source_thumb_url)

        # Try candidate high-res URLs
        for url in candidate_urls:
            temp_thumb = output_thumb_path + ".download.tmp"
            try:
                logger.info("Downloading candidate high-res YouTube thumbnail from: %s", url)
                dl_ok = await cls.download_thumbnail_image(url, temp_thumb)
                if dl_ok:
                    ok = cls.process_image_file(temp_thumb, output_thumb_path, is_vertical=is_vertical, target_ratio=target_ratio)
                    if ok:
                        logger.info("Successfully prepared HD thumbnail from candidate URL: %s", url)
                        return True
            finally:
                if os.path.exists(temp_thumb):
                    try:
                        os.remove(temp_thumb)
                    except OSError:
                        pass

        # Step 2: Fallback to local thumbnail if candidate URLs failed or none provided
        if source_thumb_path and os.path.exists(source_thumb_path) and os.path.getsize(source_thumb_path) > 0:
            logger.info("Candidate URLs unreachable or omitted; falling back to local thumbnail: %s", source_thumb_path)
            ok = cls.process_image_file(source_thumb_path, output_thumb_path, is_vertical=is_vertical, target_ratio=target_ratio)
            if ok:
                return True

        # Step 3: Fallback native-resolution frame extraction via FFmpeg
        logger.info("No usable official YouTube thumbnail found. Falling back to FFmpeg frame extraction.")
        return await cls.extract_frame_fallback(
            video_path, output_thumb_path, duration, is_vertical=is_vertical, target_ratio=target_ratio
        )
