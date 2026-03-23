"""
platform/models.py
==================
Database Schema — Signal Distribution Platform

Tables:
  signals        — สัญญาณเทรดจาก Gate 19
  users          — สมาชิก (Guest / VIP)
  subscriptions  — ประวัติการสมัคร/ต่ออายุ
  notifications  — log การส่ง LINE/Telegram

SQLite สำหรับ MVP → ย้าย PostgreSQL ได้ง่าย (เปลี่ยนแค่ connection string)
"""

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, String, Float, Integer, Boolean,
    Text, DateTime, JSON, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

Base = declarative_base()


# ============================================================
# SIGNAL TABLE
# ============================================================

class Signal(Base):
    __tablename__ = "signals"

    id              = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    signal_id       = Column(String(50), unique=True, nullable=False, index=True)
    timestamp       = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    asset           = Column(String(20), nullable=False, index=True)
    timeframe       = Column(String(10), default="15m")
    action          = Column(String(10), nullable=False)          # BUY / SELL / AVOID
    side            = Column(String(10), default="LONG")          # LONG / SHORT

    # Pricing Zone
    entry_low       = Column(Float, default=0)
    entry_high      = Column(Float, default=0)
    take_profit     = Column(Float, default=0)
    stop_loss       = Column(Float, default=0)
    risk_reward     = Column(String(20), default="1:2")

    # Analysis
    technical_summary = Column(Text, default="")
    news_catalyst     = Column(Text, default="")
    strategy_type     = Column(String(50), default="Trend Following")
    llm_cio_comment   = Column(Text, default="")

    # Sizing (from Gate 19)
    size              = Column(Float, default=0)
    sizing_unit       = Column(String(20), default="SHARES")
    sizing_multiplier = Column(Float, default=1.0)

    # Status
    status          = Column(String(20), default="ACTIVE", index=True)
    # ACTIVE / WON / LOST / CANCELLED / EXPIRED

    # Result (filled after trade closes)
    exit_price      = Column(Float, nullable=True)
    pnl_usd         = Column(Float, nullable=True)
    pnl_pct         = Column(Float, nullable=True)
    closed_at       = Column(DateTime, nullable=True)

    # Metadata
    profile_name    = Column(String(50), default="")
    ml_score        = Column(Integer, default=0)
    regime_score    = Column(Float, default=0)
    confidence      = Column(Float, default=0)
    gate19_action   = Column(String(20), default="")
    raw_metadata    = Column(JSON, default=dict)

    created_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                             onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "signal_id": self.signal_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else "",
            "asset": self.asset,
            "timeframe": self.timeframe,
            "action": self.action,
            "side": self.side,
            "pricing_zone": {
                "entry_range": [self.entry_low, self.entry_high],
                "take_profit": self.take_profit,
                "stop_loss": self.stop_loss,
                "risk_reward": self.risk_reward,
            },
            "technical_summary": self.technical_summary,
            "news_catalyst": self.news_catalyst,
            "strategy_type": self.strategy_type,
            "llm_cio_comment": self.llm_cio_comment,
            "size": self.size,
            "sizing_unit": self.sizing_unit,
            "status": self.status,
            "exit_price": self.exit_price,
            "pnl_usd": self.pnl_usd,
            "pnl_pct": self.pnl_pct,
            "ml_score": self.ml_score,
            "confidence": self.confidence,
            "gate19_action": self.gate19_action,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


# ============================================================
# USER TABLE
# ============================================================

class User(Base):
    __tablename__ = "users"

    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email        = Column(String(200), unique=True, nullable=True, index=True)
    display_name = Column(String(100), default="")
    role         = Column(String(20), default="GUEST")   # GUEST / VIP / ADMIN
    password_hash = Column(String(200), default="")

    # LINE
    line_user_id = Column(String(50), nullable=True, index=True)
    line_name    = Column(String(100), default="")

    # Telegram
    telegram_chat_id = Column(String(50), nullable=True, index=True)
    telegram_username = Column(String(100), default="")

    # Subscription
    is_active       = Column(Boolean, default=True)
    vip_expires_at  = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    subscriptions = relationship("Subscription", back_populates="user")

    def is_vip(self) -> bool:
        if self.role == "ADMIN":
            return True
        if self.role != "VIP":
            return False
        if self.vip_expires_at is None:
            return False
        return self.vip_expires_at > datetime.now(timezone.utc)

    def to_dict(self):
        return {
            "id": self.id, "email": self.email,
            "display_name": self.display_name, "role": self.role,
            "is_vip": self.is_vip(),
            "vip_expires_at": self.vip_expires_at.isoformat() if self.vip_expires_at else None,
            "line_connected": bool(self.line_user_id),
            "telegram_connected": bool(self.telegram_chat_id),
        }


# ============================================================
# SUBSCRIPTION TABLE
# ============================================================

class Subscription(Base):
    __tablename__ = "subscriptions"

    id          = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id     = Column(String(36), ForeignKey("users.id"), nullable=False)
    plan        = Column(String(50), default="monthly")   # monthly / yearly
    amount_thb  = Column(Float, default=0)
    payment_ref = Column(String(100), default="")          # Stripe/Omise ref
    status      = Column(String(20), default="ACTIVE")     # ACTIVE / EXPIRED / CANCELLED
    starts_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at  = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="subscriptions")


# ============================================================
# NOTIFICATION LOG
# ============================================================

class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    signal_id  = Column(String(50), index=True)
    channel    = Column(String(20))     # LINE / TELEGRAM / WEB
    recipient  = Column(String(100))    # user_id or channel_id
    status     = Column(String(20))     # SENT / FAILED
    message    = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db(db_url: str = "sqlite:///platform/signals.db"):
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def get_session(db_url: str = "sqlite:///platform/signals.db"):
    _, Session = init_db(db_url)
    return Session()


# ============================================================
# ALEMBIC MIGRATION SETUP
# ============================================================
#
# เมื่อ Schema เปลี่ยน (เพิ่มคอลัมน์, เปลี่ยน type) ใช้ Alembic:
#
# ขั้นตอนติดตั้ง (ครั้งแรก):
#   pip install alembic
#   cd platform
#   alembic init migrations
#
#   แก้ migrations/env.py:
#     from models import Base
#     target_metadata = Base.metadata
#
#   แก้ alembic.ini:
#     sqlalchemy.url = sqlite:///signals.db
#
# ขั้นตอนใช้งาน (ทุกครั้งที่แก้ Schema):
#   alembic revision --autogenerate -m "add xyz column"
#   alembic upgrade head
#
# Rollback:
#   alembic downgrade -1
#
# ดู history:
#   alembic history --verbose

def generate_alembic_env_snippet() -> str:
    """
    สร้าง env.py snippet สำหรับ Alembic auto-generate

    Copy ไปวางใน migrations/env.py (แทนที่ target_metadata = None)
    """
    return '''
# ── เพิ่มใน migrations/env.py (หลัง import)
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from models import Base

# ── แก้บรรทัดนี้
target_metadata = Base.metadata
'''


if __name__ == "__main__":
    engine, Session = init_db()
    print("✅ Database created: platform/signals.db")
    print(f"   Tables: {list(Base.metadata.tables.keys())}")
