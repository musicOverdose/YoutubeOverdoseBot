import re
from typing import Optional, Tuple


def parse_time_token(token: str) -> Optional[int]:
    """
    Parses a single time string token into total integer seconds.
    Supports:
      - MM:SS (e.g. "1:23", "01:23")
      - HH:MM:SS (e.g. "1:15:30", "01:15:30")
      - Unit formats: "45s", "2m", "1h30m", "2m10s"
      - Raw integer seconds: "75", "120"
    Returns integer seconds or None if invalid.
    """
    token = token.strip().lower()
    if not token:
        return None

    # 1. Colon-separated formats (MM:SS or HH:MM:SS)
    if ":" in token:
        parts = token.split(":")
        if len(parts) == 2:
            try:
                m, s = int(parts[0]), int(parts[1])
                if m >= 0 and 0 <= s < 60:
                    return m * 60 + s
            except ValueError:
                return None
        elif len(parts) == 3:
            try:
                h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                if h >= 0 and 0 <= m < 60 and 0 <= s < 60:
                    return h * 3600 + m * 60 + s
            except ValueError:
                return None
        return None

    # 2. Combined unit format (e.g. "1h30m", "2m15s", "45s", "2m")
    unit_match = re.match(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", token)
    if unit_match and any(unit_match.groups()):
        h = int(unit_match.group(1) or 0)
        m = int(unit_match.group(2) or 0)
        s = int(unit_match.group(3) or 0)
        return h * 3600 + m * 60 + s

    # 3. Plain seconds (e.g. "75s" or "75")
    if token.endswith("s"):
        token = token[:-1]
    if token.isdigit():
        return int(token)

    return None


def parse_timestamp_range(
    text: str,
    source_duration: Optional[int] = None,
    max_clip_duration: int = 1200,
    total_duration: Optional[int] = None,
) -> Tuple[bool, Optional[int], Optional[int], Optional[str]]:
    """
    Parses a user input string into start_seconds and end_seconds.
    Strictly requires hyphen delimiters: " - ", "-", "–", or "—".

    Validates:
      - Start >= 0
      - End > Start
      - End <= source_duration (if source_duration is provided and > 0)
      - (End - Start) <= max_clip_duration (default 20 minutes / 1200s)

    Returns:
      (is_valid, start_sec, end_sec, error_message)
    """
    if source_duration is None and total_duration is not None:
        source_duration = total_duration
    if not text or not text.strip():
        return False, None, None, "Please provide start and end timestamps (e.g. 1:23 - 3:30)."

    raw = text.strip()
    delim = None
    for d in [" - ", " – ", " — ", "-", "–", "—"]:
        if d in raw:
            delim = d
            break

    if not delim:
        return (
            False,
            None,
            None,
            "Please use format: start - end (e.g. 1:23 - 3:30)",
        )

    parts = raw.split(delim, 1)
    start_sec = parse_time_token(parts[0])
    end_sec = parse_time_token(parts[1])

    if start_sec is None or end_sec is None:
        return (
            False,
            None,
            None,
            "Invalid timestamp. Please use format: start - end (e.g. 1:23 - 3:30)",
        )

    if start_sec < 0:
        return False, None, None, "Start time cannot be negative."

    if end_sec <= start_sec:
        return (
            False,
            None,
            None,
            f"End time ({format_timestamp(end_sec)}) must be greater than start time ({format_timestamp(start_sec)}).",
        )

    clip_len = end_sec - start_sec
    if clip_len > max_clip_duration:
        max_min = max_clip_duration // 60
        return (
            False,
            None,
            None,
            f"Clip duration ({format_timestamp(clip_len)}) exceeds max limit of {max_min} minutes.",
        )

    if source_duration is not None and source_duration > 0:
        if end_sec > source_duration:
            return (
                False,
                None,
                None,
                f"End time ({format_timestamp(end_sec)}) exceeds video length ({format_timestamp(source_duration)}).",
            )

    return True, start_sec, end_sec, None


def format_timestamp(seconds: int) -> str:
    """Formats integer seconds into MM:SS or HH:MM:SS."""
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_clip_filename_tag(seconds: int) -> str:
    """Formats seconds into filename-safe tag, e.g. '01.23' or '01.15.30'."""
    return format_timestamp(seconds).replace(":", ".")
