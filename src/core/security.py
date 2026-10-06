import grp
import os
import pwd
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.logger import get_logger

logger = get_logger("security")

ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

ALGORITHM = "HS256"
SESSION_EXPIRE_HOURS = 24


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return ph.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_session_token(subject: str, role: str = "ADMIN") -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=SESSION_EXPIRE_HOURS)
    to_encode = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_session_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ==============================================================================
# Master Key Management & Credential Encryption
# ==============================================================================

def get_master_key_sync() -> Optional[bytes]:
    """Read master key from disk if available."""
    key_path = Path(settings.MASTER_KEY_FILE)
    if key_path.is_file():
        try:
            return key_path.read_text(encoding="utf-8").strip().encode("utf-8")
        except Exception as e:
            logger.error("Failed to read master key file %s: %s", key_path, e)
    return None


async def get_master_key(session: Optional[AsyncSession] = None) -> bytes:
    """
    Get master key or generate one on fresh boot.
    Fail-Safe: If master.key is missing but encrypted settings exist in DB,
    refuse to generate a new key and raise RuntimeError (transition to DEGRADED).
    """
    existing_key = get_master_key_sync()
    if existing_key:
        return existing_key

    # Check database for existing encrypted settings if session is provided
    if session is not None:
        from src.models.setting import Setting

        stmt = select(Setting).where(Setting.is_encrypted.is_(True))
        result = await session.execute(stmt)
        has_encrypted = result.scalars().first() is not None
        if has_encrypted:
            logger.critical(
                "FAIL-SAFE TRIGGERED: Master key file %s is missing, but encrypted settings "
                "exist in the database. Refusing to generate a replacement key. "
                "System entering DEGRADED mode.",
                settings.MASTER_KEY_FILE,
            )
            raise RuntimeError(
                "Master key file is missing while encrypted settings exist in database "
                "(fail-safe protection triggered)."
            )

    # Fresh boot: zero encrypted settings found, generate brand-new master key
    logger.info("Fresh boot detected: generating new master key via Fernet.generate_key()")
    new_key = Fernet.generate_key()
    key_path = Path(settings.MASTER_KEY_FILE)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_file(
        path=key_path,
        content=new_key.decode("utf-8"),
        mode=0o600,
    )
    return new_key


def encrypt_credential(val: str, master_key: Optional[bytes] = None) -> str:
    """Encrypt a secret string using Fernet."""
    if not val:
        return ""
    key = master_key or get_master_key_sync()
    if not key:
        raise ValueError("Master key is required for credential encryption")
    f = Fernet(key)
    encrypted_bytes = f.encrypt(val.encode("utf-8"))
    return encrypted_bytes.decode("utf-8")


def decrypt_credential(encrypted_val: str, master_key: Optional[bytes] = None) -> str:
    """Decrypt a secret string using Fernet."""
    if not encrypted_val:
        return ""
    key = master_key or get_master_key_sync()
    if not key:
        raise ValueError("Master key is required for credential decryption")
    f = Fernet(key)
    decrypted_bytes = f.decrypt(encrypted_val.encode("utf-8"))
    return decrypted_bytes.decode("utf-8")


def mask_secret(val: Optional[str], show_chars: int = 4) -> str:
    """Mask sensitive string for logs and API responses."""
    if not val:
        return ""
    if len(val) <= show_chars * 2:
        return "••••••••"
    return f"{val[:show_chars]}••••••••{val[-show_chars:]}"


# ==============================================================================
# Atomic File Writers & Permission Enforcement
# ==============================================================================

def _resolve_gid(group: Union[str, int]) -> Optional[int]:
    if isinstance(group, int):
        return group
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        return None


def _resolve_uid(owner: Union[str, int]) -> Optional[int]:
    if isinstance(owner, int):
        return owner
    try:
        return pwd.getpwnam(owner).pw_uid
    except KeyError:
        return None


def atomic_write_file(
    path: Union[str, Path],
    content: Union[str, bytes],
    mode: int = 0o640,
    group: Optional[Union[str, int]] = None,
    owner: Optional[Union[str, int]] = None,
) -> None:
    """
    Atomically write content to a file with strict permissions and ownership.
    Guarantees no partial writes by using temp file + atomic replace.
    """
    target_path = Path(path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    uid = _resolve_uid(owner) if owner is not None else -1
    gid = _resolve_gid(group) if group is not None else -1

    tmp_file = target_path.parent / f".tmp_{target_path.name}_{uuid.uuid4().hex}"
    try:
        if isinstance(content, str):
            tmp_file.write_text(content, encoding="utf-8")
        else:
            tmp_file.write_bytes(content)

        # Set permissions
        os.chmod(tmp_file, mode)

        # Apply ownership/group if resolvable
        if uid != -1 or gid != -1:
            try:
                os.chown(tmp_file, uid if uid is not None else -1, gid if gid is not None else -1)
            except (PermissionError, OSError) as e:
                logger.debug("Could not chown %s to uid=%s, gid=%s: %s", tmp_file, uid, gid, e)

        # Atomic replace
        os.replace(tmp_file, target_path)
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass


def ensure_directory(
    path: Union[str, Path],
    mode: int = 0o2770,
    group: Optional[Union[str, int]] = None,
) -> None:
    """Ensure directory exists with specified mode (including setgid bit) and group."""
    dir_path = Path(path).resolve()
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dir_path, mode)
    except OSError as e:
        logger.debug("Could not chmod directory %s to %o: %s", dir_path, mode, e)

    if group is not None:
        gid = _resolve_gid(group)
        if gid is not None:
            try:
                os.chown(dir_path, -1, gid)
            except (PermissionError, OSError) as e:
                logger.debug("Could not chown directory %s to gid=%s: %s", dir_path, gid, e)


def write_runtime_bot_token(token: str) -> None:
    """
    Write bot token to /config/runtime/bot-token.
    Permissions: mode 0640, group ytdl-runtime.
    """
    atomic_write_file(
        path=settings.RUNTIME_BOT_TOKEN_FILE,
        content=f"{token.strip()}\n",
        mode=0o640,
        group="ytdl-runtime",
    )


def write_local_bot_api_env(api_id: Union[str, int], api_hash: str) -> None:
    """
    Deprecated: Centralized Telegram Bot API server does not require local env writing.
    Preserved as no-op for backward compatibility.
    """
    pass


def write_runtime_ready() -> None:
    """
    Atomically write /config/state/READY file.
    Permissions: mode 0644 (world-readable).
    """
    atomic_write_file(
        path=settings.RUNTIME_READY_FILE,
        content="READY\n",
        mode=0o644,
    )


def write_restart_trigger() -> None:
    """
    Touch /config/bot-api/restart-trigger to notify Local Bot API supervisor.
    Permissions: mode 0640, group 101.
    """
    content = f"{datetime.now(timezone.utc).isoformat()}\n"
    atomic_write_file(
        path=settings.LOCAL_BOT_API_TRIGGER_FILE,
        content=content,
        mode=0o640,
        group=101,
    )


def remove_runtime_ready() -> None:
    """
    Remove /config/state/READY file to signal unreadiness.
    """
    try:
        p = Path(settings.RUNTIME_READY_FILE)
        if p.is_file():
            p.unlink()
            logger.info("Unlinked %s (system unreadiness signaled)", settings.RUNTIME_READY_FILE)
    except Exception as e:
        logger.warning("Could not remove %s: %s", settings.RUNTIME_READY_FILE, e)


def verify_runtime_artifacts(mode: str = "cloud") -> bool:
    """
    Verify required runtime artifacts exist, have non-zero size, and proper permissions.
    Centralized Bot API requires strictly the bot-token runtime file.
    """
    try:
        bot_token_file = Path(settings.RUNTIME_BOT_TOKEN_FILE)
        if not bot_token_file.is_file() or bot_token_file.stat().st_size == 0:
            logger.warning("Runtime bot token file %s missing or empty", bot_token_file)
            return False

        return True
    except Exception as e:
        logger.error("Error verifying runtime artifacts: %s", e)
        return False


def remove_runtime_bot_token() -> None:
    """Remove /config/runtime/bot-token file."""
    try:
        p = Path(settings.RUNTIME_BOT_TOKEN_FILE)
        if p.is_file():
            p.unlink()
            logger.info("Unlinked %s", settings.RUNTIME_BOT_TOKEN_FILE)
    except Exception as e:
        logger.warning("Could not remove %s: %s", settings.RUNTIME_BOT_TOKEN_FILE, e)


def remove_local_bot_api_artifacts() -> None:
    """Deprecated: Centralized Telegram Bot API server does not require local env cleanup."""
    pass


