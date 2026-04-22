"""
Database models and session setup for Custom Repair backend.
Uses SQLAlchemy with PostgreSQL.
"""

import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
from sqlalchemy import (
    create_engine, Column, String, Text, DateTime, Integer,
    ForeignKey, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import uuid
import json

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/customrepair")

engine = create_engine(
    DATABASE_URL, 
    pool_pre_ping=True,
    pool_recycle=300, # Recycle connections every 5 minutes (well within Neon's limits)
    connect_args={"connect_timeout": 10}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def safe_json_load(data, default_val):
    """Safely parse JSON string, returning default if invalid or empty."""
    if not data:
        return default_val
    try:
        import json
        return json.loads(data)
    except Exception:
        return default_val


# ─── Customer Models ────────────────────────────────────────

class User(Base):
    """Customer information"""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id = Column(String(20), unique=True, nullable=False)  # e.g. CR-1001
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False, index=True)
    phone = Column(String(50), nullable=False)
    address = Column(Text, nullable=True)
    password_hash = Column(String(300), nullable=True) # Optional for now to keep existing users working
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    bookings = relationship("ScheduleBooking", back_populates="user")

    def to_dict(self):
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "address": self.address,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ScheduleBooking(Base):
    """Customer booking / schedule request"""
    __tablename__ = "schedule_bookings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)

    service = Column(String(100), nullable=False)
    preferred_date = Column(String(20), nullable=True)
    preferred_time = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    # pending → assigned → confirmed → in_progress → completed | cancelled
    status = Column(String(30), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="bookings")
    assignments = relationship("JobAssignment", back_populates="booking")
    slot = relationship("WorkerSlot", back_populates="booking", uselist=False)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "service": self.service,
            "preferred_date": self.preferred_date,
            "preferred_time": self.preferred_time,
            "notes": self.notes,
            "status": self.status,
            "slot_id": self.slot.id if self.slot else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "user": self.user.to_dict() if self.user else None,
        }


class ChatSession(Base):
    """A support chat session"""
    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    category = Column(String(50), nullable=False)
    category_label = Column(String(100), nullable=True)
    status = Column(String(20), default="active")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "session_id": self.id,
            "category": self.category,
            "category_label": self.category_label,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ChatMessage(Base):
    """Individual chat message within a session"""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    category = Column(String(100), nullable=True)

    session = relationship("ChatSession", back_populates="messages")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


# ─── Worker (Field Technician) ──────────────────────────────

class Worker(Base):
    """Field technician / repair worker"""
    __tablename__ = "workers"

    id = Column(String(36), primary_key=True, default=lambda: "WK-" + str(uuid.uuid4())[:8].upper())
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False, index=True)
    phone = Column(String(50), nullable=True)
    password_hash = Column(String(300), nullable=False)
    # technician | senior_technician
    role = Column(String(30), default="technician")
    # hvac, plumbing, electrical, general (comma separated)
    specializations = Column(String(200), default="general")
    is_active = Column(Boolean, default=True)
    is_available = Column(Boolean, default=True)
    
    # Preferences stored as JSON strings
    notif_prefs = Column(Text, nullable=True)
    sched_prefs = Column(Text, nullable=True)
    privacy_prefs = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)

    assignments = relationship("JobAssignment", back_populates="worker")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "role": self.role,
            "specializations": [s.strip() for s in (self.specializations or "general").split(",")],
            "is_active": self.is_active,
            "is_available": self.is_available,
            "notif_prefs": safe_json_load(self.notif_prefs, { "newLead": True, "jobAssigned": True, "scheduleReminder": True, "systemUpdates": False, "marketing": False }),
            "sched_prefs": safe_json_load(self.sched_prefs, { "autoAccept": False, "bufferBetweenJobs": True, "weekendsAvailable": True, "maxJobsPerDay": "3" }),
            "privacy_prefs": safe_json_load(self.privacy_prefs, { "showPhone": False, "locationSharing": True, "twoFactor": False }),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ─── Admin / Internal Staff ─────────────────────────────────

class AdminUser(Base):
    """Internal staff: admin | manager | employee"""
    __tablename__ = "admin_users"

    id = Column(String(36), primary_key=True, default=lambda: "ADM-" + str(uuid.uuid4())[:8].upper())
    name = Column(String(200), nullable=False)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(300), nullable=False)
    # admin | manager | employee
    role = Column(String(20), default="employee")
    department = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "department": self.department,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ─── Job Assignment (Booking → Worker lifecycle) ─────────────

class JobAssignment(Base):
    """
    Tracks the full lifecycle of a booking assigned to a worker.
    Status flow: pending → assigned → claimed → in_progress → completed
                                   ↘ rejected (returns to pending)
    """
    __tablename__ = "job_assignments"

    id = Column(String(36), primary_key=True, default=lambda: "JA-" + str(uuid.uuid4())[:8].upper())
    booking_id = Column(String(36), ForeignKey("schedule_bookings.id"), nullable=False)
    worker_id = Column(String(36), ForeignKey("workers.id"), nullable=True)
    assigned_by = Column(String(36), nullable=True)   # AdminUser.id

    # pending | assigned | claimed | rejected | in_progress | completed | not_completed | expired
    status = Column(String(30), default="pending")

    assigned_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    worker_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    booking = relationship("ScheduleBooking", back_populates="assignments")
    worker = relationship("Worker", back_populates="assignments")

    def to_dict(self):
        return {
            "id": self.id,
            "booking_id": self.booking_id,
            "worker_id": self.worker_id,
            "status": self.status,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "worker_notes": self.worker_notes,
            "worker": self.worker.to_dict() if self.worker else None,
            "booking": self.booking.to_dict() if self.booking else None,
        }


# ─── Worker Availability Slots ─────────────────────────────

class WorkerSlot(Base):
    """Specific 2-hour or arbitrary availability window for a worker"""
    __tablename__ = "worker_slots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    worker_id = Column(String(36), ForeignKey("workers.id"), nullable=False, index=True)
    
    slot_date = Column(String(20), nullable=False) # e.g. "2024-04-10"
    start_time = Column(String(20), nullable=False) # e.g. "08:00 AM"
    end_time = Column(String(20), nullable=False)   # e.g. "10:00 AM"

    is_booked = Column(Boolean, default=False)
    booking_id = Column(String(36), ForeignKey("schedule_bookings.id", name="fk_slot_booking"), nullable=True)

    worker = relationship("Worker")
    booking = relationship("ScheduleBooking", back_populates="slot", uselist=False)

    def to_dict(self):
        # We include some booking basic info for the UI if it's booked
        res = {
            "id": self.id,
            "worker_id": self.worker_id,
            "slot_date": self.slot_date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "is_booked": self.is_booked,
            "booking_id": self.booking_id,
        }
        if self.booking:
            res["booking_service"] = self.booking.service
            res["booking_status"] = self.booking.status
            if self.booking.user:
                res["client_name"] = self.booking.user.name
        return res



def create_tables():
    """Create all tables and perform simple migrations if needed with retries."""
    import time
    max_retries = 5
    retry_delay = 2

    for attempt in range(max_retries):
        try:
            Base.metadata.create_all(bind=engine)
            print("Database tables created/verified.")
            
            # Simple migration: add password_hash to users if missing
            from sqlalchemy import text
            with engine.connect() as conn:
                try:
                    # Users migrations
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(300)"))
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
                    
                    # Workers migrations
                    conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS password_hash VARCHAR(300)"))
                    conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
                    
                    # Admin migrations
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(300)"))
                    conn.execute(text("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))

                    # Specialization to multi-skill migration
                    conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS specializations VARCHAR(200)"))
                    # If specializations is null but old specialization exists, migrate it
                    conn.execute(text("UPDATE workers SET specializations = specialization WHERE (specializations IS NULL OR specializations = '') AND specialization IS NOT NULL"))
                    conn.execute(text("UPDATE workers SET specializations = 'general' WHERE specializations IS NULL OR specializations = ''"))

                    
                    # Worker Prefs Migration
                    conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS notif_prefs TEXT"))
                    conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS sched_prefs TEXT"))
                    conn.execute(text("ALTER TABLE workers ADD COLUMN IF NOT EXISTS privacy_prefs TEXT"))
                    
                    conn.commit()
                except Exception as e:
                    print(f"Migration note: {e}")
                    pass 
            # If we reached here, everything is good
            return
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"Database initialization attempt {attempt + 1} failed: {e}. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                print(f"FATAL: Could not initialize database after {max_retries} attempts: {e}")
