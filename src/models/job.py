from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.constants import JobStatus, OperationType
from src.core.database import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), default=OperationType.VIDEO.value, nullable=False)
    output_codec: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # e.g. "1080p"
    target_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # e.g. 1080
    subtitle_lang: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # e.g. "EN", "FA"
    clip_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # start in seconds
    clip_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)    # end in seconds
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING.value, nullable=False, index=True)
    cache_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    speed: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    eta: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    requests = relationship("JobRequest", back_populates="job", cascade="all, delete-orphan")

    @property
    def queue_type(self) -> str:
        """
        Determines logical queue destination:
        - SUBTITLE -> SUBTITLE queue
        - VIDEO and AUDIO (MP3) -> VIDEO (media) queue
        """
        if self.operation == OperationType.SUBTITLE.value:
            return "SUBTITLE"
        return "VIDEO"


def get_queue_type(operation: str) -> str:
    """Helper to map an operation string to its logical queue type ('VIDEO' or 'SUBTITLE')."""
    if str(operation).upper() == OperationType.SUBTITLE.value:
        return "SUBTITLE"
    return "VIDEO"


Index("ix_jobs_status_created", Job.status, Job.created_at)
Index("ix_jobs_source_codec_res", Job.source_id, Job.output_codec, Job.resolution)
