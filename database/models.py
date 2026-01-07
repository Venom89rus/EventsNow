from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Float,
    Date,
    Time,
    DateTime,
    Text,
    ForeignKey,
    Enum as SQLEnum,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


# ---------- ENUMs ----------
class EventCategory(str, PyEnum):
    EXHIBITION = "EXHIBITION"
    MASTERCLASS = "MASTERCLASS"
    CONCERT = "CONCERT"
    PERFORMANCE = "PERFORMANCE"
    LECTURE = "LECTURE"
    OTHER = "OTHER"


class EventStatus(str, PyEnum):
    DRAFT = "draft"
    PENDING_MODERATION = "pending_moderation"
    APPROVED_WAITING_PAYMENT = "approved_waiting_payment"
    ACTIVE = "active"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class PaymentStatus(str, PyEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PricingModel(str, PyEnum):
    DAILY = "daily"
    PERIOD = "period"


class UserRole(str, PyEnum):
    RESIDENT = "resident"
    ORGANIZER = "organizer"
    ADMIN = "admin"


class CityStatus(str, PyEnum):
    ACTIVE = "active"
    COMING_SOON = "coming_soon"


# ---------- User ----------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)

    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=True)

    role = Column(SQLEnum(UserRole), default=UserRole.RESIDENT)
    city_slug = Column(String(50), default="nojabrsk")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # relationships
    events = relationship("Event", back_populates="organizer")
    payments = relationship("Payment", back_populates="organizer")
    comments = relationship("Comment", back_populates="user")


# ---------- City ----------
class City(Base):
    __tablename__ = "cities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(50), unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    status = Column(SQLEnum(CityStatus), default=CityStatus.ACTIVE)

    created_at = Column(DateTime, default=datetime.utcnow)


# ---------- Event ----------
class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    city_slug = Column(String(50), nullable=False)

    title = Column(String(255), nullable=False)
    category = Column(SQLEnum(EventCategory), nullable=False)
    description = Column(Text)

    # contacts
    contact_phone = Column(String(50))
    contact_email = Column(String(255))

    # location
    location = Column(String(500))

    # visitor pricing (simple)
    price_admission = Column(Float)

    # visitor pricing (tiers / misc)
    admission_price_json = Column(Text, nullable=True)  # JSON str: {"дети":300,"взрослые":600}
    free_kids_upto_age = Column(Integer, nullable=True)
    reject_reason = Column(Text, nullable=True)

    # DAILY
    event_date = Column(Date)
    event_time_start = Column(Time)
    event_time_end = Column(Time)

    # PERIOD (exhibition)
    period_start = Column(Date)
    period_end = Column(Date)
    working_hours_start = Column(Time)
    working_hours_end = Column(Time)

    # statuses
    status = Column(SQLEnum(EventStatus), default=EventStatus.DRAFT)
    payment_status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.PENDING)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # relationships
    organizer = relationship("User", back_populates="events")

    # 1 event -> 0/1 payment (через Payment.event_id)
    payment = relationship(
        "Payment",
        back_populates="event",
        uselist=False,
    )

    comments = relationship("Comment", back_populates="event")
    favorites = relationship("Favorite", back_populates="event")


# ---------- Payment ----------
class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)

    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)

    # Один платёж на одно событие (если захочешь историю платежей — убери unique=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, unique=True)

    category = Column(SQLEnum(EventCategory), nullable=False)
    pricing_model = Column(SQLEnum(PricingModel), nullable=False)

    # DAILY packages
    package_daily = Column(String(50))  # "1_post", "3_posts"
    num_posts = Column(Integer)

    # PERIOD packages
    package_period = Column(String(50))  # "7_days", "30_days"
    num_days = Column(Integer)

    amount = Column(Float, nullable=False)
    status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.PENDING)

    payment_system = Column(String(50))  # "yookassa", "telegram_payments", "test"
    transaction_id = Column(String(255), unique=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # relationships
    organizer = relationship("User", back_populates="payments")
    event = relationship("Event", back_populates="payment")


# ---------- Comment ----------
class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)

    text = Column(Text, nullable=False)
    rating = Column(Integer, nullable=True)  # 1-5
    created_at = Column(DateTime, default=datetime.utcnow)

    # relationships
    event = relationship("Event", back_populates="comments")
    user = relationship("User", back_populates="comments")


# ---------- Favorite ----------
class Favorite(Base):
    __tablename__ = "favorites"

    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"), primary_key=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    # relationships
    event = relationship("Event", back_populates="favorites")


# ---------- Feedback ----------
class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)

    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
