import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobState(str, enum.Enum):
    SUBMITTED = "submitted"
    PROVIDER_QUEUED = "provider_queued"
    PROVIDER_DOWNLOADING = "provider_downloading"
    PROVIDER_READY = "provider_ready"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    save_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    info_hash: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default=JobState.SUBMITTED.value, index=True)
    size: Mapped[int | None] = mapped_column(Integer)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    download_speed: Mapped[int] = mapped_column(Integer, default=0)
    eta: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    poll_failures: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[Category | None] = relationship(lazy="joined")
    provider_job: Mapped["ProviderJob | None"] = relationship(back_populates="job", lazy="joined",
                                                               cascade="all, delete-orphan")


class ProviderJob(Base):
    __tablename__ = "provider_jobs"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="torbox")
    remote_id: Mapped[int | None] = mapped_column(Integer, index=True)
    queued_id: Mapped[int | None] = mapped_column(Integer, index=True)
    provider_state: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text, default="{}")
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job: Mapped[Job] = relationship(back_populates="provider_job")
