import enum
from datetime import datetime
from typing import List, Optional
from sqlalchemy import Table
from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, DateTime,
    ForeignKey, Enum, Text, Float, JSON
)
from sqlalchemy.orm import declarative_base, relationship, Mapped, mapped_column
from sqlalchemy.sql import func

Base = declarative_base()

contact_group_members = Table(
    "contact_group_members",
    Base.metadata,
    Column("group_id", ForeignKey("contact_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("contact_id", ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
)


class RoleEnum(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class GatewayTypeEnum(str, enum.Enum):
    GOIP_UDP = "goip_udp"
    GOIP_HTTP = "goip_http"
    SKYLINE = "skyline"
    DINSTAR = "dinstar"


class MessageDirectionEnum(str, enum.Enum):
    INCOMING = "in"
    OUTGOING = "out"


class MessageStatusEnum(str, enum.Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT_OK = "sent_ok"
    DELIVERED = "delivered"
    FAILED = "failed"
    RECEIVED = "received"


class GoIPEventTypeEnum(str, enum.Enum):
    KEEPALIVE = "keepalive"
    STATE = "state"
    RECORD = "record"
    REMAIN = "remain"
    CELLS = "cells"
    RECEIVE = "receive"


class PendingRegistrationSource(str, enum.Enum):
    """Откуда подана заявка на регистрацию (semi_open)."""
    TELEGRAM = "telegram"
    API = "api"


class IncomingRuleActionEnum(str, enum.Enum):
    WEBHOOK = "webhook"
    AUTOREPLY = "autoreply"


class User(Base):
    """Пользователь системы. Создаётся через /start в боте или через REST API."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[RoleEnum] = mapped_column(Enum(RoleEnum), default=RoleEnum.USER)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    sim_cards: Mapped[List["SimCard"]] = relationship("SimCard", back_populates="assigned_user")
    contacts: Mapped[List["Contact"]] = relationship("Contact", back_populates="user")
    sms_templates: Mapped[List["SMSTemplate"]] = relationship("SMSTemplate", back_populates="user")


class Gateway(Base):
    """GSM-шлюз. Управляется через /gateways в боте или через REST API."""
    __tablename__ = "gateways"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[GatewayTypeEnum] = mapped_column(Enum(GatewayTypeEnum))
    host: Mapped[str] = mapped_column(String(100))
    port: Mapped[int] = mapped_column(Integer, default=9991)
    username: Mapped[str] = mapped_column(String(50), default="admin")
    password: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    sim_cards: Mapped[List["SimCard"]] = relationship("SimCard", back_populates="gateway")


class SimCard(Base):
    """
    Физический SIM-слот (порт) в шлюзе.
    Заменяет старый PhoneNumber из SQLite-схемы.
    Поле port_number — номер порта/канала (1,2,3...), ранее называлось channel.
    """
    __tablename__ = "sim_cards"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("gateways.id", ondelete="CASCADE"))
    port_number: Mapped[int] = mapped_column(Integer)
    phone_number: Mapped[Optional[str]] = mapped_column(String(20), index=True, nullable=True)
    imei: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    iccid: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    operator: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="UNKNOWN")
    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    assigned_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    gateway: Mapped["Gateway"] = relationship("Gateway", back_populates="sim_cards")
    assigned_user: Mapped[Optional["User"]] = relationship("User", back_populates="sim_cards")
    messages: Mapped[List["Message"]] = relationship("Message", back_populates="sim_card")


class Contact(Base):
    """Контакт пользователя — подпись для внешнего номера телефона."""
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    phone_number: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))

    user: Mapped["User"] = relationship("User", back_populates="contacts")
    groups: Mapped[List["ContactGroup"]] = relationship(
        "ContactGroup",
        secondary=contact_group_members,
        back_populates="contacts",
    )


class ContactGroup(Base):
    """Группа контактов пользователя (для массовых рассылок)."""
    __tablename__ = "contact_groups"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship("User")
    contacts: Mapped[List["Contact"]] = relationship(
        "Contact",
        secondary=contact_group_members,
        back_populates="groups",
    )


class IncomingRule(Base):
    """
    Правило обработки входящих SMS.
    Например, webhook для пересылки на внешний сервер или autoreply (автоответчик).
    """
    __tablename__ = "incoming_rules"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    keyword: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    action_type: Mapped[IncomingRuleActionEnum] = mapped_column(Enum(IncomingRuleActionEnum))
    target_data: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship("User")


class SMSTemplate(Base):
    """Шаблон SMS пользователя."""
    __tablename__ = "sms_templates"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), default="general")
    is_global: Mapped[bool] = mapped_column(Boolean, default=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[Optional["User"]] = relationship("User", back_populates="sms_templates")


class Message(Base):
    """
    История входящих и исходящих SMS.
    sim_card_id может быть NULL для системных сообщений или при неизвестном порте.
    """
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sim_card_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sim_cards.id", ondelete="SET NULL"), nullable=True
    )
    external_phone: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[MessageDirectionEnum] = mapped_column(Enum(MessageDirectionEnum))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[MessageStatusEnum] = mapped_column(
        Enum(MessageStatusEnum), default=MessageStatusEnum.PENDING
    )
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    gateway_task_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)

    sim_card: Mapped[Optional["SimCard"]] = relationship("SimCard", back_populates="messages")


class GoIPEvent(Base):
    """
    Универсальная таблица push-событий от GoIP UDP.
    Хранит сырой payload для отладки, статистики и трассировки инцидентов.
    """
    __tablename__ = "goip_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    goip_id: Mapped[str] = mapped_column(String(100), index=True)
    host: Mapped[str] = mapped_column(String(100), index=True)
    port: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[GoIPEventTypeEnum] = mapped_column(Enum(GoIPEventTypeEnum), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PendingRegistration(Base):
    """
    Заявка на регистрацию (режим semi_open).
    После одобрения админом создаётся User, запись удаляется.
    """
    __tablename__ = "pending_registrations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[PendingRegistrationSource] = mapped_column(
        Enum(PendingRegistrationSource), default=PendingRegistrationSource.TELEGRAM
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SystemSetting(Base):
    """Ключ-значение настроек (в т.ч. переопределение режима регистрации из админки)."""
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
