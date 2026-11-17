"""SQLAlchemy-модели.

Разделение данных:
- offers / campaigns / streams — известные удалённые сущности (кэш последнего чтения Keitaro);
- stream_offers.pub_* — подтверждённое опубликованное состояние связи;
- stream_offers.status/share/pinned/kt_state — сохраняемый черновик;
- stream_offers со status='removed' и pub_present=False — локальный архив связей.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(500), default="")
    state: Mapped[str] = mapped_column(String(32), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(500), default="")
    alias: Mapped[str] = mapped_column(String(200), default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    streams: Mapped[list["Stream"]] = relationship(
        back_populates="campaign", order_by="Stream.position", cascade="all, delete-orphan"
    )


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Последний прочитанный объект потока целиком — для проверки, что Push не изменил чужие поля.
    remote_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    remote_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    # Увеличивается при каждой правке черновика; клиент присылает ожидаемую версию.
    draft_version: Mapped[int] = mapped_column(Integer, default=0)
    push_state: Mapped[str] = mapped_column(String(16), default="idle")  # idle | pushing
    push_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Защищённые поля потока, изменившиеся после последнего Push. Пока не пусто — Push потока запрещён.
    side_effect_fields: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Защищённые поля потока, прочитанные непосредственно перед первым PUT ещё не подтверждённого Push.
    # Хранится до подтверждения или Fetch: повтор / already_applied / Fetch сравнивают текущий поток с ним.
    push_baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="streams")
    links: Mapped[list["StreamOffer"]] = relationship(
        back_populates="stream", order_by="StreamOffer.position", cascade="all, delete-orphan"
    )


class StreamOffer(Base):
    __tablename__ = "stream_offers"
    __table_args__ = (UniqueConstraint("stream_id", "offer_id", name="uq_stream_offer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    # Черновик
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | removed (локально)
    share: Mapped[int] = mapped_column(Integer, default=0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    kt_state: Mapped[str] = mapped_column(String(16), default="active")  # state связи в Keitaro

    # Подтверждённое опубликованное состояние (последнее чтение Keitaro / проверенный Push)
    pub_present: Mapped[bool] = mapped_column(Boolean, default=False)
    pub_share: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pub_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pub_pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    ever_published: Mapped[bool] = mapped_column(Boolean, default=False)
    remote_link_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stream: Mapped[Stream] = relationship(back_populates="links")


class CampaignJob(Base):
    """Пошаговое создание кампании. Сохраняет достигнутый результат между REST-вызовами."""

    __tablename__ = "campaign_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    request: Mapped[dict] = mapped_column(JSON)
    alias: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|partial|done|failed
    # Аренда выполнения: кто и до какого момента (unix-время, сек) выполняет задание.
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geo_stream_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offer_stream_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
