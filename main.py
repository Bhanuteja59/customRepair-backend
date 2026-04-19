"""
Custom Repair — FastAPI Backend
Handles: Schedule Bookings, AI Chat, Auth (Worker + Admin RBAC), Real-time WebSocket
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime, timedelta
import asyncio
from sqlalchemy import func
from sqlalchemy.orm import Session
import openai
import re

from database import (
    SessionLocal, get_db, create_tables,
    User, ScheduleBooking, ChatSession, ChatMessage,
    Worker, AdminUser, JobAssignment, WorkerSlot,
)
from auth import (
    hash_password, verify_password, create_token,
    get_current_worker, get_current_admin, get_current_customer, 
    get_optional_customer, require_roles, verify_ws_token, bearer_scheme,
)

# App setup ───

app = FastAPI(
    title="Custom Repair API",
    description="Backend for Custom Repair — scheduling, AI chat, worker dispatch, admin RBAC",
    version="2.0.0",
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.responses import JSONResponse
import traceback

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print(f"GLOBAL ERROR: {exc}")
    # Dynamically pick the origin from the request to avoid CORS blocking the error message
    origin = request.headers.get("origin", "http://localhost:3000")
    
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": str(exc), "type": type(exc).__name__},
        headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*"
        }
    )

@app.on_event("startup")
async def startup_event():
    # Attempt to initialize database tables on startup
    try:
        create_tables()
    except Exception as e:
        print(f"STARTUP ERROR (DB): {e}")

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

# ─── Real-time WebSocket connection manager ───────────────

class ConnectionManager:
    """Manages active WebSocket connections for workers and admins."""

    def __init__(self):
        # worker_id → {"ws": WebSocket, "spec": str}
        self.workers: dict[str, dict] = {}
        # list of connected admin sockets
        self.admins: list[WebSocket] = []
        # user_id → list of WebSockets (one user may have multiple tabs)
        self.customers: dict[str, list[WebSocket]] = {}

    async def connect_worker(self, worker_id: str, ws: WebSocket, skills: List[str]):
        await ws.accept()
        self.workers[worker_id] = {"ws": ws, "skills": [s.lower() for s in skills]}

    def disconnect_worker(self, worker_id: str):
        self.workers.pop(worker_id, None)

    async def connect_admin(self, ws: WebSocket):
        await ws.accept()
        self.admins.append(ws)

    def disconnect_admin(self, ws: WebSocket):
        if ws in self.admins:
            self.admins.remove(ws)



    async def broadcast_to_workers(self, message: dict):
        """Broadcast to all connected workers."""
        dead = []
        for wid, data in list(self.workers.items()):
            try:
                await data["ws"].send_text(json.dumps(message))
            except Exception:
                dead.append(wid)
        for wid in dead:
            self.workers.pop(wid, None)

    async def broadcast_to_specialists(self, service: str, message: dict):
        """Broadcast to workers who have ALL required skills for the service (AND logic)."""
        target_skills = extract_required_skills(service)
        dead = []
        for wid, data in list(self.workers.items()):
            worker_skills = data.get("skills", ["general"])
            # SUPERSET CHECK: Worker must cover all requirements
            if set(target_skills).issubset(set(worker_skills)):
                try:
                    await data["ws"].send_text(json.dumps(message))
                except Exception:
                    dead.append(wid)
        for wid in dead:
            self.workers.pop(wid, None)

    async def send_to_worker(self, worker_id: str, message: dict):
        """Send to a specific worker if connected."""
        data = self.workers.get(worker_id)
        if data:
            try:
                await data["ws"].send_text(json.dumps(message))
            except Exception:
                self.workers.pop(worker_id, None)

    async def broadcast_to_admins(self, message: dict):
        """Broadcast to all connected admins."""
        dead = []
        for ws in list(self.admins):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.admins.remove(ws)

    async def connect_customer(self, user_id: str, ws: WebSocket):
        await ws.accept()
        if user_id not in self.customers:
            self.customers[user_id] = []
        self.customers[user_id].append(ws)

    def disconnect_customer(self, user_id: str, ws: WebSocket):
        if user_id in self.customers:
            self.customers[user_id] = [s for s in self.customers[user_id] if s is not ws]
            if not self.customers[user_id]:
                del self.customers[user_id]

    async def send_to_customer(self, user_id: str, message: dict):
        """Send a real-time update to all active sessions of a specific customer."""
        sockets = self.customers.get(user_id, [])
        dead = []
        for ws in list(sockets):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws)
        for ws in dead:
            if user_id in self.customers and ws in self.customers[user_id]:
                self.customers[user_id].remove(ws)
        if user_id in self.customers and not self.customers[user_id]:
            del self.customers[user_id]


manager = ConnectionManager()
async def check_job_expiry():
    """Background loop to mark missed slots as expired."""
    while True:
        try:
            # Run check every 1 minute
            await asyncio.sleep(60)
            print("Checking for expired job windows...")
            
            # Using SessionLocal directly with a try/finally to ensure closure
            db = SessionLocal()
            try:
                now = datetime.now() 
                
                # We only expire jobs that are assigned/claimed but not yet started
                expired_candidates = (
                    db.query(JobAssignment)
                    .filter(JobAssignment.status.in_(["assigned", "claimed"]))
                    .all()
                )
                
                for a in expired_candidates:
                    if not a.booking: continue
                    
                    try:
                        date_str = a.booking.preferred_date
                        time_str = a.booking.preferred_time
                        
                        if not date_str: continue
                        
                        # Handle flexible slots - expire at end of day
                        if not time_str or any(x in time_str.lower() for x in ["flex", "asap"]):
                            end_dt = datetime.strptime(f"{date_str} 11:59 PM", "%Y-%m-%d %I:%M %p")
                        else:
                            # "10:00 AM - 12:00 PM" -> "12:00 PM"
                            end_part = time_str.split("-")[-1].strip()
                            end_dt = datetime.strptime(f"{date_str} {end_part}", "%Y-%m-%d %I:%M %p")
                        
                        if now > end_dt:
                            print(f"Job {a.id} expired! Window ended at {end_dt}")
                            a.status = "expired"
                            a.booking.status = "overdue"
                            db.commit()
                            
                            # Real-time alert to admins
                            await manager.broadcast_to_admins({
                                "type": "job_status_update",
                                "assignment": a.to_dict(),
                                "message": f"Window missed for {a.booking.service}. Ticket marked as expired."
                            })
                    except Exception as parse_err:
                        # Log but don't crash
                        pass
            except Exception as db_err:
                print(f"Database Error in background task: {db_err}")
            finally:
                db.close()
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Background Task Error: {e}")
            await asyncio.sleep(5) # Cooldown before retry


# ─── Startup ──────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    try:
        create_tables()
        # Launch the expiry check in the background
        asyncio.create_task(check_job_expiry())
    except Exception as e:
        print(f"DATABASE ERROR: {e}")

# ─── Health ───────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "Custom Repair API", "version": "2.0.0"}

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "connected_workers": len(manager.workers),
        "connected_admins": len(manager.admins),
    }

# ─── Pydantic schemas ─────────────────────────────────────

class ScheduleRequest(BaseModel):
    service: str
    name: str
    phone: str
    email: str
    address: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    notes: Optional[str] = None
    slot_id: Optional[str] = None

class ChatSessionCreate(BaseModel):
    category: str
    category_label: Optional[str] = None

class ChatMessageRequest(BaseModel):
    session_id: str
    message: str
    category: Optional[str] = None

class WorkerSignupRequest(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    password: str
    specializations: List[str] = ["general"]

class CustomerSignupRequest(BaseModel):
    name: str
    email: EmailStr
    phone: str
    password: str
    address: Optional[str] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class AdminUserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "employee"  # admin | manager | employee
    department: Optional[str] = None

class AssignJobRequest(BaseModel):
    booking_id: str
    worker_id: str

class JobStatusUpdate(BaseModel):
    status: str   # claimed | rejected | in_progress | completed
    notes: Optional[str] = None

class WorkerAvailabilityUpdate(BaseModel):
    is_available: bool

class WorkerProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    specializations: Optional[List[str]] = None

class WorkerSlotCreate(BaseModel):
    slot_date: str
    start_time: str
    end_time: str

def segment_to_2h(start_str: str, end_str: str):
    """Yields 2-hour time window strings between start and end."""
    from datetime import datetime, timedelta
    fmt = "%I:%M %p"
    try:
        t_start = datetime.strptime(start_str.strip(), fmt)
        t_end = datetime.strptime(end_str.strip(), fmt)
        
        current = t_start
        while current + timedelta(hours=2) <= t_end:
            nxt = current + timedelta(hours=2)
            yield f"{current.strftime(fmt)} – {nxt.strftime(fmt)}"
            current = nxt
    except Exception:
        # Fallback if parsing fails
        yield f"{start_str} – {end_str}"

def get_buffer_range(window_str: str):
    """Returns (start_min - 120, end_min + 120) for adjacency checking."""
    # "08:00 AM – 10:00 AM"
    separators = ["-", "–", "—"]
    for sep in separators:
        if sep in window_str:
            parts = window_str.split(sep)
            s = parse_time_to_minutes(parts[0])
            if len(parts) > 1:
                e = parse_time_to_minutes(parts[1])
                return s - 120, e + 120
    # Single point
    s = parse_time_to_minutes(window_str)
    return s - 120, s + 120 + 60

def is_window_conflict(target_window_str: str, occupied_windows: List[tuple]) -> bool:
    """
    Checks if a target 2h window conflicts with occupied windows or their adjacent buffers.
    occupied_windows is a list of (start_min, end_min).
    """
    # Any job inside [start - 2h, end + 2h] counts as a conflict (Overlap + Adjacency)
    buf_start, buf_end = get_buffer_range(target_window_str)
    
    for os, oe in occupied_windows:
        # Standard overlap check between search buffer and occupied window
        if buf_start < oe and buf_end > os:
            return True
    return False

def get_occupied_minutes_for_worker(db: Session, worker_id: str, date: str) -> List[tuple]:
    """Fetches all (start_min, end_min) for a worker's assigned jobs on a date."""
    assignments = db.query(JobAssignment).join(ScheduleBooking).filter(
        JobAssignment.worker_id == worker_id,
        ScheduleBooking.preferred_date == date,
        JobAssignment.status.in_(["assigned", "claimed", "in_progress", "completed"])
    ).all()
    
    results = []
    for a in assignments:
        time_str = a.booking.preferred_time
        if not time_str: continue
        
        separators = ["-", "–", "—"]
        for sep in separators:
            if sep in time_str:
                parts = time_str.split(sep)
                results.append((parse_time_to_minutes(parts[0]), parse_time_to_minutes(parts[1])))
                break
        else:
            # Single point fallback
            m = parse_time_to_minutes(time_str)
            results.append((m, m + 120)) # Assume 2h if unknown
    return results


# ─── Public Availability API ──────────────────────────────

@app.get("/api/public/available-slots")
def get_public_slots(
    service: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Groups available 2-hour windows from ALL active workers matching the service category.
    Includes granular 'is booked' and 'adjacency' filtering.
    """
    from collections import defaultdict
    
    # 1. Get relevant workers
    worker_query = db.query(Worker).filter(Worker.is_active == True)
    if service:
        required = extract_required_skills(service)
        for skill in required:
            worker_query = worker_query.filter(Worker.specializations.contains(skill))
    
    workers = worker_query.all()
    result = defaultdict(list)
    seen_windows = set()

    for w in workers:
        # 2. Get all worker slots for future (simplified to all for now)
        slots = db.query(WorkerSlot).filter(WorkerSlot.worker_id == w.id).all()
        
        # Cache occupied windows for this worker by date to avoid N+1 inside the date loop
        # We'll just do it simply per date instead.
        
        processed_dates = set(s.slot_date for s in slots)
        occupied_by_date = {d: get_occupied_minutes_for_worker(db, w.id, d) for d in processed_dates}

        for s in slots:
            occupied = occupied_by_date.get(s.slot_date, [])
            for window in segment_to_2h(s.start_time, s.end_time):
                # 3. Apply Granular Filtering (Overlap + Adjacency)
                if not is_window_conflict(window, occupied):
                    key = (s.slot_date, window)
                    if key not in seen_windows:
                        result[s.slot_date].append({"id": s.id, "time": window})
                        seen_windows.add(key)
        
    # Sort dates and windows for clean UI
    sorted_result = {}
    for date in sorted(result.keys()):
        sorted_result[date] = sorted(result[date], key=lambda x: parse_time_to_minutes(x["time"].split(" – ")[0]))
        
    return sorted_result


# ─── Customer Helpers ──────────────────────────────────────

def generate_customer_id(db: Session):
    count = db.query(User).count()
    return f"CR-{1001 + count}"


# ─── Allocation Logic ──────────────────────────────────────

def perform_auto_allocation(db: Session, booking: ScheduleBooking, preferred_slot_id: str = None, exclude_worker_ids: List[str] = []):
    """
    Mandatory Auto-Allocation Engine:
    1. Filter by Skills
    2. Filter by Shift Availability (Worker MUST have the slot)
    3. Selection Tiers:
       - Tier 1: No time conflicts
       - Tier 2: Overbooked (Least-Total-Jobs technician wins)
    """
    required_skills = extract_required_skills(booking.service)
    
    # 1. Get all active workers
    all_workers = db.query(Worker).filter(Worker.is_active == True, Worker.is_available == True).all()
    if exclude_worker_ids:
        all_workers = [w for w in all_workers if w.id not in exclude_worker_ids]
        
    tier1 = [] # No conflicts
    tier2 = [] # Existing conflicts but has the shift
    
    for w in all_workers:
        # A. Skill check
        worker_skills = [s.strip().lower() for s in (w.specializations or "general").split(",")]
        if not set(required_skills).issubset(set(worker_skills)):
            continue
            
        # B. Shift check (Must have the slot for that day/time)
        slots = db.query(WorkerSlot).filter(WorkerSlot.worker_id == w.id, WorkerSlot.slot_date == booking.preferred_date).all()
        matched_slot = None
        for s in slots:
            if does_worker_match_time(booking.preferred_time, [s]):
                matched_slot = s
                break
        
        if not matched_slot:
            continue

        # C. Calculate Historical Workload (Total assignments in history)
        workload = db.query(JobAssignment).filter(JobAssignment.worker_id == w.id).count()
        
        # D. Conflict check
        occupied = get_occupied_minutes_for_worker(db, w.id, booking.preferred_date)
        if not is_window_conflict(booking.preferred_time, occupied):
            tier1.append({"worker": w, "workload": workload, "slot": matched_slot})
        else:
            tier2.append({"worker": w, "workload": workload, "slot": matched_slot})

    # Selection Logic: Prioritize tier 1, fallback to tier 2
    candidates = tier1 if tier1 else tier2
    if not candidates:
        return None, None

    # Sort by historical workload (Least total jobs wins)
    candidates.sort(key=lambda x: x["workload"])
    best = candidates[0]
    return best["worker"], best["slot"]


# ─── Schedule Booking API ─────────────────────────────────

@app.post("/api/schedule")
async def create_booking(
    payload: ScheduleRequest, 
    db: Session = Depends(get_db),
    current: Optional[User] = Depends(get_optional_customer)
):
    """Create a new service booking and broadcast real-time alert to all workers and admins."""
    
    # Find or create customer
    user = current
    if not user:
        user = db.query(User).filter(User.email == payload.email).first()
    if user:
        user.name = payload.name
        user.phone = payload.phone
        user.address = payload.address
    else:
        user = User(
            customer_id=generate_customer_id(db),
            name=payload.name,
            email=payload.email,
            phone=payload.phone,
            address=payload.address,
        )
        db.add(user)
        db.flush()

    booking = ScheduleBooking(
        user_id=user.id,
        service=payload.service,
        preferred_date=payload.date,
        preferred_time=payload.time,
        notes=payload.notes,
        status="pending",
    )
    db.add(booking)
    db.flush()

    # Create initial JobAssignment
    assignment = JobAssignment(
        booking=booking,
        status="pending",
    )
    db.add(assignment)
    db.flush()

    # Perform Strict Auto-Allocation
    worker, slot = perform_auto_allocation(db, booking, payload.slot_id)
    
    if worker:
        # Success: Assign job and auto-claim
        assignment.worker_id = worker.id
        assignment.status = "claimed" # SKIP assigned, go straight to claimed
        assignment.assigned_at = datetime.utcnow()
        assignment.accepted_at = datetime.utcnow()
        booking.status = "confirmed"
        
        if slot:
            slot.booking_id = booking.id
            
        db.commit()
        db.refresh(booking)
        db.refresh(assignment)

        # Real-time alert to chosen worker
        alert = {
            "type": "new_assignment",
            "assignment": assignment.to_dict(),
            "target_worker_id": worker.id,
            "title": "New Job Assigned",
            "msg": f"You've been assigned a {booking.service} job for {booking.preferred_date}."
        }
        await manager.broadcast_to_workers(alert)
    else:
        # ABSOLUTELY NO ONE has the shift: Remains pending for Admin review
        booking.status = "pending"
        db.commit()
        db.refresh(booking)

    # General broadcast to Admins only
    admin_alert = {
        "type": "new_lead",
        "booking": booking.to_dict(),
        "assignment_id": assignment.id,
        "auto_assigned": True if worker else False
    }
    await manager.broadcast_to_admins(admin_alert)

    return {
        "success": True,
        "booking_id": booking.id,
        "customer_id": user.customer_id,
        "message": f"Booking received for {user.name} (Ref: {user.customer_id}). We'll call you at {user.phone} shortly.",
    }


# ─── Customer Auth & Dashboard ──────────────────────────

@app.post("/api/auth/signup")
def customer_signup(payload: CustomerSignupRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user = User(
        customer_id=generate_customer_id(db),
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        address=payload.address
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    token = create_token(user.id, "customer", "user")
    return {"access_token": token, "token_type": "bearer", "user": user.to_dict()}


@app.post("/api/auth/login")
def customer_login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    token = create_token(user.id, "customer", "user")
    return {"access_token": token, "token_type": "bearer", "user": user.to_dict()}


@app.get("/api/auth/me")
def customer_me(current: User = Depends(get_current_customer)):
    return current.to_dict()


@app.get("/api/customer/dashboard")
def get_customer_dashboard(current: User = Depends(get_current_customer), db: Session = Depends(get_db)):
    """Fetch all bookings and active jobs for the logged-in customer."""
    bookings = (
        db.query(ScheduleBooking)
        .filter(ScheduleBooking.user_id == current.id)
        .order_by(ScheduleBooking.created_at.desc())
        .all()
    )
    
    result = []
    for b in bookings:
        d = b.to_dict()
        # Find assignment details
        assignment = db.query(JobAssignment).filter(JobAssignment.booking_id == b.id).first()
        d["assignment"] = assignment.to_dict() if assignment else None
        result.append(d)
        
    return {
        "customer": current.to_dict(),
        "bookings": result
    }

@app.get("/api/schedule")
def list_bookings(current: User = Depends(get_current_customer), db: Session = Depends(get_db)):
    """User can only list their own bookings."""
    bookings = db.query(ScheduleBooking).filter(ScheduleBooking.user_id == current.id).order_by(ScheduleBooking.created_at.desc()).all()
    return [b.to_dict() for b in bookings]

@app.get("/api/schedule/{booking_id}")
def get_booking(booking_id: str, current: User = Depends(get_current_customer), db: Session = Depends(get_db)):
    """User can only get their own booking."""
    booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == booking_id, ScheduleBooking.user_id == current.id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found or access denied")
    return booking.to_dict()


# ─── Worker Auth ──────────────────────────────────────────

@app.post("/api/workers/signup")
def worker_signup(payload: WorkerSignupRequest, db: Session = Depends(get_db)):
    if db.query(Worker).filter(Worker.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    worker = Worker(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        specializations=",".join(payload.specializations),
        role="technician",
    )
    db.add(worker)
    db.commit()
    db.refresh(worker)

    token = create_token(worker.id, "worker", worker.role)
    return {"access_token": token, "token_type": "bearer", "worker": worker.to_dict()}


@app.post("/api/workers/login")
def worker_login(payload: LoginRequest, db: Session = Depends(get_db)):
    worker = db.query(Worker).filter(Worker.email == payload.email).first()
    if not worker or not verify_password(payload.password, worker.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not worker.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated. Contact your manager.")

    token = create_token(worker.id, "worker", worker.role)
    return {"access_token": token, "token_type": "bearer", "worker": worker.to_dict()}


@app.get("/api/workers/me")
def worker_me(current: Worker = Depends(get_current_worker)):
    return current.to_dict()


@app.patch("/api/workers/availability")
def update_availability(
    payload: WorkerAvailabilityUpdate,
    current: Worker = Depends(get_current_worker),
    db: Session = Depends(get_db),
):
    worker = db.query(Worker).filter(Worker.id == current.id).first()
    worker.is_available = payload.is_available
    db.commit()
    return {"is_available": worker.is_available}


@app.patch("/api/workers/profile")
def update_worker_profile(
    payload: WorkerProfileUpdate,
    current: Worker = Depends(get_current_worker),
    db: Session = Depends(get_db),
):
    worker = db.query(Worker).filter(Worker.id == current.id).first()
    if payload.name: worker.name = payload.name
    if payload.phone: worker.phone = payload.phone
    if payload.specializations is not None:
        worker.specializations = ",".join(payload.specializations)
    db.commit()
    db.refresh(worker)
    return worker.to_dict()


@app.get("/api/workers/slots")
def list_worker_slots(
    current: Worker = Depends(get_current_worker),
    db: Session = Depends(get_db),
):
    slots = db.query(WorkerSlot).filter(WorkerSlot.worker_id == current.id).all()
    return [s.to_dict() for s in slots]


@app.post("/api/workers/slots")
def add_worker_slot(
    payload: WorkerSlotCreate,
    current: Worker = Depends(get_current_worker),
    db: Session = Depends(get_db),
):
    slot = WorkerSlot(
        worker_id=current.id,
        slot_date=payload.slot_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot.to_dict()


@app.delete("/api/workers/slots/{slot_id}")
def delete_worker_slot(
    slot_id: str,
    current: Worker = Depends(get_current_worker),
    db: Session = Depends(get_db),
):
    slot = db.query(WorkerSlot).filter(WorkerSlot.id == slot_id, WorkerSlot.worker_id == current.id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    
    # Check if any active assignments overlap with this slot's time range
    s_min = parse_time_to_minutes(slot.start_time)
    e_min = parse_time_to_minutes(slot.end_time)
    
    occupied = get_occupied_minutes_for_worker(db, current.id, slot.slot_date)
    for os, oe in occupied:
        # Standard overlap check
        if s_min < oe and e_min > os:
            raise HTTPException(
                status_code=400, 
                detail="Cannot delete slot: You have an assigned job during this time window."
            )

    db.delete(slot)
    db.commit()
    return {"success": True}


@app.get("/api/workers/jobs")
def worker_jobs(current: Worker = Depends(get_current_worker), db: Session = Depends(get_db)):
    """All jobs assigned to this worker (any non-pending status)."""
    assignments = (
        db.query(JobAssignment)
        .filter(JobAssignment.worker_id == current.id)
        .order_by(JobAssignment.created_at.desc())
        .all()
    )
    return [redact_assignment(a.to_dict()) for a in assignments]


def extract_required_skills(service: str) -> List[str]:
    """Identify ALL required skills for a booking based on keywords (Multi-trade support)."""
    s = (service or "").lower()
    required = []
    if "plumb" in s:
        required.append("plumbing")
    if any(x in s for x in ["hvac", "ac ", "furnace", "heat", "thermostat"]):
        required.append("hvac")
    if "electr" in s:
        required.append("electrical")
    
    # If no specialty detected, default to general
    if not required:
        required.append("general")
    return required


def parse_time_to_minutes(time_str: str) -> int:
    """Converts '09:00 AM' to minutes from midnight."""
    try:
        t = datetime.strptime(time_str.strip(), "%I:%M %p")
        return t.hour * 60 + t.minute
    except:
        return 0


def does_worker_match_time(booking_time: str, worker_slots: List[WorkerSlot]) -> bool:
    """Checks if ANY worker slot overlaps with the requested booking time."""
    if not booking_time or any(x in booking_time.lower() for x in ["flex", "asap"]):
        return len(worker_slots) > 0

    # Common separators including en-dash and em-dash
    separators = ["-", "–", "—"]
    booking_start, booking_end = 0, 0
    
    found_sep = False
    for sep in separators:
        if sep in booking_time:
            parts = booking_time.split(sep)
            if len(parts) == 2:
                booking_start = parse_time_to_minutes(parts[0])
                booking_end = parse_time_to_minutes(parts[1])
                found_sep = True
                break
    
    if not found_sep:
        # If no range, treat as a single point in time
        booking_start = parse_time_to_minutes(booking_time)
        booking_end = booking_start + 60 # Default 1h duration

    for slot in worker_slots:
        slot_start = parse_time_to_minutes(slot.start_time)
        slot_end = parse_time_to_minutes(slot.end_time)
        
        # Check for ANY overlap
        # (StartA < EndB) and (EndA > StartB)
        if (booking_start < slot_end) and (booking_end > slot_start):
            return True
            
    return False


def redact_assignment(a_dict: dict, reveal_all: bool = False):
    """
    Advanced security utility to mask PII (Personally Identifiable Information).
    Ensures data minimization by scrubbing names, phones, and addresses 
    based on the 'Least Privilege' principle.
    """
    if reveal_all:
        return a_dict
        
    import copy
    res = copy.deepcopy(a_dict)
    
    # Check if this assignment is unclaimed (pending) or completed
    status = res.get("status")
    
    # Reveal all details for active jobs so the worker can perform the service
    is_active_work = status in ["assigned", "claimed", "in_progress"]

    if "booking" in res and res["booking"]:
        booking = res["booking"]
        # Hide customer notes for unclaimed leads
        if status == "pending":
            booking["notes"] = "[Secure Content]: Claim job to view customer directives"
            
        if "user" in booking and booking["user"]:
            u = booking["user"]
            
            # If not active work, mask identity and location
            if not is_active_work:
                u["phone"] = "PII PROTECTED"
                u["name"] = f"Client {booking.get('id', '??')[:4]}"
                
                # Address redaction
                full_addr = u.get("address", "")
                if status == "pending":
                    # Show city/zip for locality context
                    parts = full_addr.split(",")
                    u["address"] = parts[-1].strip() if len(parts) > 1 else "Area Masked"
                else:
                    u["address"] = "Confidential - Ticket Closed"
            # Else (assigned, claimed, in_progress): The technician sees everything!
                
    return res





@app.patch("/api/jobs/{assignment_id}/status")
async def update_job_status(
    assignment_id: str,
    payload: JobStatusUpdate,
    current: Worker = Depends(get_current_worker),
    db: Session = Depends(get_db),
):
    assignment = db.query(JobAssignment).filter(JobAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    allowed_transitions = {
        "pending": ["claimed"], 
        "assigned": ["claimed", "rejected"],
        "claimed": ["in_progress", "rejected", "not_completed"],
        "in_progress": ["completed", "not_completed"],
    }

    current_status = assignment.status
    new_status = payload.status

    # Idempotency: If already in the target status, return success
    if current_status == new_status:
        return {"success": True, "message": f"Job is already {new_status}", "assignment": assignment.to_dict()}

    if new_status not in allowed_transitions.get(current_status, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from '{current_status}' to '{new_status}'",
        )

    # For 'pending' jobs, worker self-assigns by claiming — validate multi-skill overlap
    if current_status == "pending" and new_status == "claimed":
        worker_skills = current.to_dict().get("specializations", ["general"])
        required_skills = extract_required_skills(assignment.booking.service if assignment.booking else "")
        
        if not set(required_skills).issubset(set(worker_skills)):
            print(f"Skill mismatch for worker {current.name}. Required: {required_skills}, Has: {worker_skills}")
            raise HTTPException(
                status_code=403,
                detail=f"This job requires multiple skills: {', '.join(required_skills)}. Your skills ({', '.join(worker_skills)}) do not fully match. Please update your profile if you have these skills.",
            )
        assignment.worker_id = current.id
        assignment.assigned_at = datetime.utcnow()
    elif assignment.worker_id and assignment.worker_id != current.id:
        raise HTTPException(status_code=403, detail="This job is assigned to another worker")

    now = datetime.utcnow()
    assignment.status = new_status

    if new_status == "rejected":
        # Check if the job has already started (Time Window restriction)
        booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == assignment.booking_id).first()
        if booking:
            try:
                if not booking.preferred_time:
                    raise ValueError("No time specified")
                
                separators = [re.escape(s) for s in ["-", "–", "—"]]
                split_pattern = r"\s*(?:" + "|".join(separators) + r")\s*"
                start_time_part = re.split(split_pattern, booking.preferred_time)[0].strip()
                
                dt_str = f"{booking.preferred_date} {start_time_part}"
                # Use local system time for comparison with local booking time
                # preferred_date/time are assumed to be in the server/app local time for technician coordination
                start_dt = datetime.strptime(dt_str, "%Y-%m-%d %I:%M %p")
                
                # 24h Restriction: Must cancel at least 24 hours before start
                if datetime.now() >= (start_dt - timedelta(hours=24)):
                    raise HTTPException(
                        status_code=400,
                        detail="It's too late to cancel this job. Cancellation is only allowed more than 24 hours before the scheduled start time."
                    )
            except HTTPException:
                raise
            except Exception as e:
                print(f"Time validation failed: {e}")
                # If parsing fails, we allow rejection (safety fallback)
                pass

        # 1. Identify previous assignees for this booking
        previous_assignments = db.query(JobAssignment).filter(
            JobAssignment.booking_id == assignment.booking_id,
        ).all()
        exclude_ids = [pa.worker_id for pa in previous_assignments if pa.worker_id]
        
        # 2. Reset current assignment
        assignment.worker_id = None
        # assignment.status was already set to 'rejected' above
        assignment.assigned_at = None
        
        # 3. Attempt re-allocation
        booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == assignment.booking_id).first()
        if booking:
            new_worker, new_slot = perform_auto_allocation(db, booking, exclude_worker_ids=exclude_ids)
            if new_worker:
                # Create a NEW assignment for the new worker
                import uuid
                new_assignment = JobAssignment(
                    id=f"AS-{str(uuid.uuid4())[:8].upper()}",
                    booking_id=booking.id,
                    worker_id=new_worker.id,
                    status="claimed", # SKIP assigned, go straight to claimed
                    assigned_at=datetime.utcnow(),
                    accepted_at=datetime.utcnow(),
                )
                db.add(new_assignment)
                if new_slot:
                    new_slot.booking_id = booking.id
                
                booking.status = "confirmed"
                
                db.commit()
                db.refresh(new_assignment)

                # Notify the NEW worker
                await manager.broadcast_to_workers({
                    "type": "new_assignment",
                    "assignment": new_assignment.to_dict(),
                    "target_worker_id": new_worker.id,
                    "title": "New Job Assigned",
                    "msg": f"You've been assigned a {booking.service} job for {booking.preferred_date}."
                })
            else:
                # ABSOLUTELY NO ONE else has the shift
                booking.status = "pending"
                db.commit()
    elif new_status == "in_progress":
        assignment.started_at = now
        booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == assignment.booking_id).first()
        if booking:
            booking.status = "in_progress"
    elif new_status == "completed":
        assignment.completed_at = now
        booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == assignment.booking_id).first()
        if booking:
            booking.status = "completed"

    if payload.notes:
        assignment.worker_notes = payload.notes

    db.commit()
    db.refresh(assignment)

    # Broadcast status change to all admins
    await manager.broadcast_to_admins({
        "type": "job_status_update",
        "assignment": assignment.to_dict(),
    })

    # Notify the customer if they are online
    if assignment.booking and assignment.booking.user_id:
        await manager.send_to_customer(assignment.booking.user_id, {
            "type": "job_update",
            "assignment": assignment.to_dict(),
            "message": f"Your {assignment.booking.service} job status changed to {new_status}."
        })

    return assignment.to_dict()




# ─── Admin Auth ───────────────────────────────────────────

@app.post("/api/admin/setup")
def admin_setup(payload: AdminUserCreate, db: Session = Depends(get_db)):
    """Bootstrap: creates the first admin. Locked once any admin exists."""
    if db.query(AdminUser).count() > 0:
        raise HTTPException(
            status_code=403,
            detail="Admin already configured. Log in at /api/admin/login.",
        )
    admin = AdminUser(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="admin",
        department=payload.department,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    token = create_token(admin.id, "admin", admin.role)
    return {"access_token": token, "token_type": "bearer", "admin": admin.to_dict()}


@app.post("/api/admin/login")
def admin_login(payload: LoginRequest, db: Session = Depends(get_db)):
    admin = db.query(AdminUser).filter(AdminUser.email == payload.email).first()
    if not admin or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not admin.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated.")

    token = create_token(admin.id, "admin", admin.role)
    return {"access_token": token, "token_type": "bearer", "admin": admin.to_dict()}


@app.get("/api/admin/me")
def admin_me(current: AdminUser = Depends(get_current_admin)):
    return current.to_dict()


# ─── Admin User Management (admin role only) ──────────────

@app.get("/api/admin/users")
def list_admin_users(
    _current: AdminUser = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    users = db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()
    return [u.to_dict() for u in users]


@app.post("/api/admin/users")
def create_admin_user(
    payload: AdminUserCreate,
    _current: AdminUser = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    if db.query(AdminUser).filter(AdminUser.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = AdminUser(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        department=payload.department,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.to_dict()


@app.patch("/api/admin/users/{user_id}/toggle")
def toggle_admin_user(
    user_id: str,
    current: AdminUser = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    if user_id == current.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = not user.is_active
    db.commit()
    return user.to_dict()


# ─── Admin Bookings (manager + admin) ────────────────────

@app.get("/api/admin/bookings")
def admin_get_bookings(
    _current: AdminUser = Depends(require_roles("admin", "manager", "employee")),
    db: Session = Depends(get_db),
):
    bookings = db.query(ScheduleBooking).order_by(ScheduleBooking.created_at.desc()).all()
    result = []
    for b in bookings:
        data = b.to_dict()
        assignment = db.query(JobAssignment).filter(JobAssignment.booking_id == b.id).first()
        data["assignment"] = assignment.to_dict() if assignment else None
        result.append(data)
    return result


@app.post("/api/admin/assign")
async def admin_assign_job(
    payload: AssignJobRequest,
    current: AdminUser = Depends(require_roles("admin", "manager")),
    db: Session = Depends(get_db),
):
    """Assign a booking to a specific worker."""
    booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == payload.booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    worker = db.query(Worker).filter(Worker.id == payload.worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if not worker.is_active:
        raise HTTPException(status_code=400, detail="Worker is not active")

    # Strict Skill Validation
    worker_skills = worker.to_dict().get("specializations", ["general"])
    required_skills = extract_required_skills(booking.service)
    if not set(required_skills).issubset(set(worker_skills)):
        missing = list(set(required_skills) - set(worker_skills))
        raise HTTPException(
            status_code=403, 
            detail=f"Skill Mismatch: Worker ({worker.name}) lacks trade skills: {', '.join(missing)}"
        )

    assignment = db.query(JobAssignment).filter(JobAssignment.booking_id == payload.booking_id).first()
    now = datetime.utcnow()

    if assignment:
        assignment.worker_id = payload.worker_id
        assignment.assigned_by = current.id
        assignment.assigned_at = now
        assignment.accepted_at = now
        assignment.status = "claimed"
    else:
        import uuid
        assignment = JobAssignment(
            id=f"AS-{str(uuid.uuid4())[:8].upper()}",
            booking_id=payload.booking_id,
            worker_id=payload.worker_id,
            assigned_by=current.id,
            assigned_at=now,
            accepted_at=now,
            status="claimed",
        )
        db.add(assignment)

    booking.status = "confirmed"
    db.commit()
    db.refresh(assignment)

    # Alerts & Notifications
    alert = {
        "type": "new_assignment",
        "assignment": assignment.to_dict(),
    }
    await manager.send_to_worker(payload.worker_id, alert)
    await manager.broadcast_to_admins({"type": "job_assigned", "assignment": assignment.to_dict()})
    if booking.user_id:
        await manager.send_to_customer(booking.user_id, {
            "type": "job_update",
            "assignment": assignment.to_dict(),
            "message": f"A technician has been assigned to your {booking.service} request.",
        })

    return assignment.to_dict()


# ─── Admin: Workers management (manager + admin) ──────────

@app.get("/api/admin/workers")
def admin_get_workers(
    _current: AdminUser = Depends(require_roles("admin", "manager")),
    db: Session = Depends(get_db),
):
    workers = db.query(Worker).order_by(Worker.created_at.desc()).all()
    result = []
    for w in workers:
        data = w.to_dict()
        active_jobs = (
            db.query(JobAssignment)
            .filter(JobAssignment.worker_id == w.id)
            .filter(JobAssignment.status.in_(["assigned", "claimed", "in_progress"]))
            .count()
        )
        data["active_jobs"] = active_jobs
        result.append(data)
    return result


@app.patch("/api/admin/workers/{worker_id}/toggle")
def toggle_worker(
    worker_id: str,
    _current: AdminUser = Depends(require_roles("admin", "manager")),
    db: Session = Depends(get_db),
):
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    worker.is_active = not worker.is_active
    db.commit()
    return worker.to_dict()


# ─── Admin Customers (admin + manager) ─────────────────────

@app.get("/api/admin/customers")
def admin_get_customers(
    _current: AdminUser = Depends(require_roles("admin", "manager")),
    db: Session = Depends(get_db),
):
    customers = db.query(User).order_by(User.created_at.desc()).all()
    return [c.to_dict() for c in customers]


# ─── Consolidated Dashboard Data ──────────────────────────

@app.get("/api/admin/dashboard-data")
def get_dashboard_data(
    current: AdminUser = Depends(require_roles("admin", "manager", "employee")),
    db: Session = Depends(get_db),
):
    """Wait-free consolidation of all dashboard data for auto-fetch/real-time."""
    role = current.role
    is_admin_manager = role in ["admin", "manager"]
    
    # 1. Bookings
    from sqlalchemy.orm import joinedload
    bookings_raw = (
        db.query(ScheduleBooking)
        .options(
            joinedload(ScheduleBooking.user),
            joinedload(ScheduleBooking.assignments).joinedload(JobAssignment.worker)
        )
        .order_by(ScheduleBooking.created_at.desc())
        .limit(100)
        .all()
    )
    
    bookings = []
    for b in bookings_raw:
        d = b.to_dict()
        # Get the latest assignment if available
        d["assignment"] = b.assignments[-1].to_dict() if b.assignments else None
        # Redact for employees
        if not is_admin_manager:
            d = redact_assignment(d)
        bookings.append(d)

    # 2. Workers
    workers = []
    if is_admin_manager:
        workers_raw = db.query(Worker).order_by(Worker.created_at.desc()).all()
        for w in workers_raw:
            wd = w.to_dict()
            wd["active_jobs"] = db.query(JobAssignment).filter(
                JobAssignment.worker_id == w.id,
                JobAssignment.status.in_(["assigned", "claimed", "in_progress"])
            ).count()
            workers.append(wd)

    # 3. Analytics & Admin Users
    admin_users = []
    analytics = None
    if role == "admin":
        users_raw = db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()
        admin_users = [u.to_dict() for u in users_raw]
        
        total = db.query(ScheduleBooking).count()
        in_p = db.query(ScheduleBooking).filter(ScheduleBooking.status == "in_progress").count()
        comp = db.query(ScheduleBooking).filter(ScheduleBooking.status == "completed").count()
        avail = db.query(Worker).filter(Worker.is_available == True, Worker.is_active == True).count()
        breakdown = db.query(ScheduleBooking.service, func.count(ScheduleBooking.id)).group_by(ScheduleBooking.service).all()
        
        analytics = {
            "bookings": {"total": total, "in_progress": in_p, "completed": comp},
            "workers": {"available": avail},
            "service_breakdown": [{"service": r[0], "count": r[1]} for r in breakdown]
        }

    # 4. Customers
    customers_list = []
    if is_admin_manager:
        customers_list = [c.to_dict() for c in db.query(User).all()]

    return {
        "bookings": bookings,
        "workers": workers,
        "adminUsers": admin_users,
        "analytics": analytics,
        "customers": customers_list
    }


# ─── Admin Analytics (admin only) ─────────────────────────

@app.get("/api/admin/analytics")
def admin_analytics(
    _current: AdminUser = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    total_bookings = db.query(ScheduleBooking).count()
    pending = db.query(ScheduleBooking).filter(ScheduleBooking.status == "pending").count()
    assigned = db.query(ScheduleBooking).filter(ScheduleBooking.status == "assigned").count()
    confirmed = db.query(ScheduleBooking).filter(ScheduleBooking.status == "confirmed").count()
    in_progress = db.query(ScheduleBooking).filter(ScheduleBooking.status == "in_progress").count()
    completed = db.query(ScheduleBooking).filter(ScheduleBooking.status == "completed").count()

    total_workers = db.query(Worker).filter(Worker.is_active == True).count()
    available_workers = db.query(Worker).filter(Worker.is_available == True, Worker.is_active == True).count()

    category_breakdown = (
        db.query(ScheduleBooking.service, func.count(ScheduleBooking.id).label("count"))
        .group_by(ScheduleBooking.service)
        .all()
    )

    return {
        "bookings": {
            "total": total_bookings,
            "pending": pending,
            "assigned": assigned,
            "confirmed": confirmed,
            "in_progress": in_progress,
            "completed": completed,
        },
        "workers": {
            "total": total_workers,
            "available": available_workers,
        },
        "service_breakdown": [
            {"service": r.service, "count": r.count} for r in category_breakdown
        ],
    }


# ─── Employee: my issues view ─────────────────────────────

@app.get("/api/admin/my-issues")
def employee_issues(
    _current: AdminUser = Depends(require_roles("admin", "manager", "employee")),
    db: Session = Depends(get_db),
):
    """Employee-level view: active customer complaints/issues."""
    bookings = (
        db.query(ScheduleBooking)
        .filter(ScheduleBooking.status.in_(["pending", "assigned", "confirmed", "in_progress"]))
        .order_by(ScheduleBooking.created_at.desc())
        .all()
    )
    result = []
    for b in bookings:
        data = b.to_dict()
        assignment = db.query(JobAssignment).filter(JobAssignment.booking_id == b.id).first()
        data["assignment"] = assignment.to_dict() if assignment else None
        result.append(data)
    return result


# ─── Chat Session API ─────────────────────────────────────

@app.post("/api/chat/session")
def create_chat_session(payload: ChatSessionCreate, db: Session = Depends(get_db)):
    session = ChatSession(category=payload.category, category_label=payload.category_label)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session.to_dict()


@app.post("/api/chat/message")
def send_chat_message(payload: ChatMessageRequest, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.id == payload.session_id).first()
    if not session:
        session = ChatSession(
            id=payload.session_id,
            category=payload.category or "other",
            category_label=payload.category,
        )
        db.add(session)
        db.commit()

    user_msg = ChatMessage(
        session_id=session.id, role="user",
        content=payload.message, category=payload.category,
    )
    db.add(user_msg)
    db.flush()

    history = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )

    openai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if session.category_label:
        openai_messages.append({
            "role": "system",
            "content": f"The user's reported issue category is: {session.category_label}",
        })
    for msg in history:
        openai_messages.append({"role": msg.role, "content": msg.content})
    openai_messages.append({"role": "user", "content": payload.message})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", messages=openai_messages, max_tokens=400, temperature=0.7,
        )
        reply_text = response.choices[0].message.content
    except openai.AuthenticationError:
        reply_text = "I'm having trouble with AI services right now. Call us at (555) 123-4567 or schedule at /schedule."
    except openai.RateLimitError:
        reply_text = "High demand right now. Our team is at (555) 123-4567, or schedule online at /schedule."
    except Exception:
        reply_text = get_fallback_reply(payload.message, session.category_label or "")

    bot_msg = ChatMessage(session_id=session.id, role="assistant", content=reply_text)
    db.add(bot_msg)
    db.commit()

    return {"reply": reply_text, "session_id": session.id}


@app.get("/api/chat/session/{session_id}/history")
def get_chat_history(session_id: str, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    return {"session": session.to_dict(), "messages": [m.to_dict() for m in messages]}


@app.get("/api/chat/sessions")
def list_sessions(db: Session = Depends(get_db)):
    sessions = db.query(ChatSession).order_by(ChatSession.created_at.desc()).all()
    result = []
    for s in sessions:
        data = s.to_dict()
        data["message_count"] = db.query(ChatMessage).filter(ChatMessage.session_id == s.id).count()
        result.append(data)
    return result


@app.get("/api/analytics/problems")
def get_problem_analytics(db: Session = Depends(get_db)):
    results = (
        db.query(ChatSession.category, func.count(ChatSession.id).label("count"))
        .group_by(ChatSession.category)
        .all()
    )
    return [{"category": r.category, "count": r.count} for r in results]


# ─── WebSocket: Workers ───────────────────────────────────

@app.websocket("/ws/worker/{worker_id}")
async def ws_worker(
    websocket: WebSocket,
    worker_id: str,
    token: Optional[str] = Query(default=None),
):
    """Real-time channel for a worker. Pass token as ?token=<jwt>"""
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        payload = verify_ws_token(token, "worker")
        if payload.get("sub") != worker_id:
            await websocket.close(code=1008, reason="Token mismatch")
            return
    except Exception:
        await websocket.close(code=1008, reason="Invalid token")
        return

    # Use a manual session generator for WebSocket safety
    db = next(get_db())
    try:
        # Fetch specializations for targeted alerts (ensuring we strip spaces)
        worker = db.query(Worker).filter(Worker.id == worker_id).first()
        skills = [s.strip() for s in (worker.specializations or "general").split(",")] if worker else ["general"]

        await manager.connect_worker(worker_id, websocket, skills)
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect_worker(worker_id)
    finally:
        db.close()


# ─── WebSocket: Admins ────────────────────────────────────

@app.websocket("/ws/admin")
async def ws_admin(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
):
    """Real-time channel for admin users. Pass token as ?token=<jwt>"""
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        verify_ws_token(token, "admin")
    except Exception:
        await websocket.close(code=1008, reason="Invalid token")
        return

    await manager.connect_admin(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect_admin(websocket)


# ─── AI fallback replies ──────────────────────────────────

SYSTEM_PROMPT = """You are a helpful, friendly AI support assistant for Custom Repair, a Metro Atlanta home services company specializing in HVAC, plumbing, and electrical work.

Your role:
1. Help users troubleshoot their home repair problems
2. Ask clarifying questions to understand the issue better
3. Provide preliminary guidance (but always recommend professional service for safety)
4. Categorize the severity (urgent/can-wait)
5. Offer to connect them with a technician or direct them to schedule at /schedule

Company facts:
- Service 20+ Metro Atlanta cities
- 24/7 emergency service, 2-hour emergency response
- Free estimates
- $50 off first service
- 0% APR financing on qualifying systems
- Lifetime workmanship guarantee
- Phone: (555) 123-4567

Tone: Warm, professional, knowledgeable. Keep replies concise (2-4 sentences usually). Use bullet points for multi-step guidance.

IMPORTANT: Always prioritize safety. For gas leaks, electrical sparks, or flooding — tell them to shut it off immediately and call emergency services if needed.
"""


def get_fallback_reply(text: str, category: str) -> str:
    t, cat = text.lower(), category.lower()
    if "ac" in cat or "cooling" in cat:
        if any(w in t for w in ["not cool", "warm", "hot"]):
            return "Sounds like a refrigerant or airflow issue. Quick check: Is the outdoor unit running? Is the air filter clean? Our HVAC techs can diagnose same-day. Want to schedule a visit?"
        return "For AC problems, describe what's happening — not turning on, not cooling, making noise, or leaking water?"
    if "water" in cat or "plumb" in cat:
        if "leak" in t:
            return "⚠️ For an active leak: shut off the water supply valve near the source (or the main shutoff). Then book a plumber — we offer same-day service. Is it from a pipe, fixture, or appliance?"
        return "Our licensed plumbers handle leaks, clogs, water heaters, low pressure, and more. Can you share more specifics?"
    if "electric" in cat:
        return "⚡ Safety first — never work on live electrical systems. Is it a single outlet/fixture, a whole circuit, or a panel issue? Our certified electricians can run a full diagnostic."
    if any(w in t for w in ["book", "schedule"]):
        return "You can book a visit at our Schedule page, or I can take your details right here. What date and time works best?"
    return "Thanks for the details! Based on what you've described, I'd recommend a technician visit. We offer free estimates and same-day service. Would you like to schedule?"


@app.websocket("/ws/customer/{user_id}")
async def ws_customer(
    websocket: WebSocket,
    user_id: str,
    token: Optional[str] = Query(default=None),
):
    """Real-time channel for customers. Pass token as ?token=<jwt>"""
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    try:
        payload = verify_ws_token(token, "customer")
        if payload.get("sub") != user_id:
            await websocket.close(code=1008, reason="Token mismatch")
            return
    except Exception:
        await websocket.close(code=1008, reason="Invalid token")
        return

    await manager.connect_customer(user_id, websocket)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_customer(user_id, websocket)
