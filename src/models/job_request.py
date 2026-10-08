from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.constants import DeliveryStatus
from src.core.database import Base


class JobRequest(Base):
    __tablename__ = "job_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    menu_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    delivery_status: Mapped[str] = mapped_column(
        String(32), default=DeliveryStatus.PENDING.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    job = relationship("Job", back_populates="requests")
    user = relationship("User", back_populates="requests")
