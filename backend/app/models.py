import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base
from backend.app.enums import ChecklistSchedule, ChecklistStatus, EstimateStatus


class Contractor(Base):
    __tablename__ = "contractors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    trade: Mapped[str] = mapped_column(String(255), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    hourly_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    soul_text: Mapped[str] = mapped_column(Text, default="")
    business_hours: Mapped[str] = mapped_column(String(255), default="")
    preferred_channel: Mapped[str] = mapped_column(String(20), default="telegram")
    channel_identifier: Mapped[str] = mapped_column(String(255), default="")
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    preferences_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    clients: Mapped[list["Client"]] = relationship(back_populates="contractor")
    memories: Mapped[list["Memory"]] = relationship(back_populates="contractor")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="contractor")
    estimates: Mapped[list["Estimate"]] = relationship(back_populates="contractor")
    media_files: Mapped[list["MediaFile"]] = relationship(back_populates="contractor")
    checklist_items: Mapped[list["HeartbeatChecklistItem"]] = relationship(
        back_populates="contractor"
    )


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contractor_id: Mapped[int] = mapped_column(Integer, ForeignKey("contractors.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    contractor: Mapped["Contractor"] = relationship(back_populates="clients")


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contractor_id: Mapped[int] = mapped_column(Integer, ForeignKey("contractors.id"), index=True)
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(
        String(50), default="general"
    )  # pricing, client, job, general
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    contractor: Mapped["Contractor"] = relationship(back_populates="memories")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contractor_id: Mapped[int] = mapped_column(Integer, ForeignKey("contractors.id"), index=True)
    external_session_id: Mapped[str] = mapped_column(String(255), default="")
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_message_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_active: Mapped[bool] = mapped_column(default=True)

    contractor: Mapped["Contractor"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id"), index=True
    )
    direction: Mapped[str] = mapped_column(String(20))  # inbound, outbound
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    body: Mapped[str] = mapped_column(Text, default="")
    media_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    processed_context: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    media_files: Mapped[list["MediaFile"]] = relationship(back_populates="message")


class Estimate(Base):
    __tablename__ = "estimates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contractor_id: Mapped[int] = mapped_column(Integer, ForeignKey("contractors.id"), index=True)
    client_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("clients.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(
        String(20), default=EstimateStatus.DRAFT
    )  # draft, sent, accepted, rejected
    pdf_url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    contractor: Mapped["Contractor"] = relationship(back_populates="estimates")
    client: Mapped["Client | None"] = relationship()
    line_items: Mapped[list["EstimateLineItem"]] = relationship(back_populates="estimate")


class EstimateLineItem(Base):
    __tablename__ = "estimate_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    estimate_id: Mapped[int] = mapped_column(Integer, ForeignKey("estimates.id"), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)

    estimate: Mapped["Estimate"] = relationship(back_populates="line_items")


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("messages.id"), nullable=True
    )
    contractor_id: Mapped[int] = mapped_column(Integer, ForeignKey("contractors.id"), index=True)
    original_url: Mapped[str] = mapped_column(String(500), default="")
    mime_type: Mapped[str] = mapped_column(String(100), default="")
    processed_text: Mapped[str] = mapped_column(Text, default="")
    storage_url: Mapped[str] = mapped_column(String(500), default="")
    storage_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    message: Mapped["Message | None"] = relationship(back_populates="media_files")
    contractor: Mapped["Contractor"] = relationship(back_populates="media_files")


class HeartbeatChecklistItem(Base):
    __tablename__ = "heartbeat_checklist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contractor_id: Mapped[int] = mapped_column(Integer, ForeignKey("contractors.id"), index=True)
    description: Mapped[str] = mapped_column(Text)
    schedule: Mapped[str] = mapped_column(
        String(50), default=ChecklistSchedule.DAILY
    )  # daily, weekdays, once
    active_hours: Mapped[str] = mapped_column(String(255), default="")  # e.g. "7am-5pm"
    last_triggered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=ChecklistStatus.ACTIVE
    )  # active, paused, completed
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    contractor: Mapped["Contractor"] = relationship(back_populates="checklist_items")
