from enum import Enum


class JobStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    DOWNLOADING = "DOWNLOADING"
    PROCESSING = "PROCESSING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    WAITING_FOR_AUTHORIZATION = "WAITING_FOR_AUTHORIZATION"
    FAILED = "FAILED"


class OperationType(str, Enum):
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    SUBTITLE = "SUBTITLE"


class VideoCodec(str, Enum):
    H264 = "H264"
    H265 = "H265"


class AudioCodec(str, Enum):
    MP3 = "MP3"
    AAC = "AAC"


class SubtitleLang(str, Enum):
    EN = "EN"
    FA = "FA"


class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BANNED = "BANNED"


class AuditAction(str, Enum):
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    SETTING_UPDATE = "SETTING_UPDATE"
    QUEUE_PAUSE = "QUEUE_PAUSE"
    QUEUE_RESUME = "QUEUE_RESUME"
    JOB_CANCEL = "JOB_CANCEL"
    JOB_RETRY = "JOB_RETRY"
    USER_BAN = "USER_BAN"
    USER_UNBAN = "USER_UNBAN"
    USER_ROLE_CHANGE = "USER_ROLE_CHANGE"
    CHANNEL_ADD = "CHANNEL_ADD"
    CHANNEL_UPDATE = "CHANNEL_UPDATE"
    CHANNEL_REMOVE = "CHANNEL_REMOVE"
    COOKIES_UPDATE = "COOKIES_UPDATE"
    COOKIES_DELETE = "COOKIES_DELETE"
    AI_UPDATE = "AI_UPDATE"
    CACHE_INVALIDATE = "CACHE_INVALIDATE"
    CACHE_DELETE = "CACHE_DELETE"


# Redis Key Patterns
REDIS_KEY_QUEUE = "ytdl:queue:fifo"
REDIS_KEY_QUEUE_VIDEO = "ytdl:queue:video"
REDIS_KEY_QUEUE_SUBTITLE = "ytdl:queue:subtitle"
REDIS_KEY_ACTIVE_JOBS = "ytdl:active_jobs:set"
REDIS_KEY_ACTIVE_VIDEO = "ytdl:active_jobs:video"
REDIS_KEY_ACTIVE_SUBTITLE = "ytdl:active_jobs:subtitle"
REDIS_KEY_JOB_DATA_PREFIX = "ytdl:job:"
REDIS_KEY_PROGRESS_PREFIX = "ytdl:progress:"
REDIS_KEY_CACHE_LOCK_PREFIX = "ytdl:lock:cache:"
REDIS_KEY_QUEUE_PAUSED = "ytdl:queue_paused"
REDIS_KEY_MEMBERSHIP_PREFIX = "ytdl:membership:cache:"
REDIS_KEY_CANCEL_PREFIX = "ytdl:cancel:"
REDIS_KEY_METADATA_PREFIX = "ytdl:metadata:cache:"
REDIS_KEY_SUBTITLE_CACHE_PREFIX = "ytdl:subs:en:"
REDIS_KEY_SUBTITLE_FA_CACHE_PREFIX = "ytdl:subs:fa:"

# Default Timeouts and Settings
MEMBERSHIP_CACHE_TTL = 1800             # 30 minutes (backward compatible default)
MEMBERSHIP_CACHE_TTL_MEMBER = 1800      # 30 minutes for confirmed channel members
MEMBERSHIP_CACHE_TTL_NON_MEMBER = 15    # 15 seconds for unverified users
CACHE_LOCK_TTL = 3600                   # 1 hour duplicate lock
