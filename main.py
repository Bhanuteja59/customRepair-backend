import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
import openai

from database import (
    get_db, create_tables,
    User, ScheduleBooking, ChatSession, ChatMessage,
    Worker, AdminUser, JobAssignment, WorkerSlot, SessionLocal,
)
from auth import (
    hash_password, verify_password, create_token,
    get_current_worker, get_current_admin, get_current_customer,
    get_optional_customer, require_roles,
)
from schemas import *
from utils import (
    segment_to_2h, generate_customer_id, extract_required_skills,
    parse_time_to_minutes, does_worker_match_time, redact_assignment,
    SYSTEM_PROMPT, get_fallback_reply
)

# App setup ───

app = FastAPI(
    title="Custom Repair API",
    description="Backend for Custom Repair — scheduling, AI chat, worker dispatch, admin RBAC",
    version="2.0.0",
)

_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()] + [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3002",
    "https://custom-repair-workers.vercel.app",
    "https://custom-repair-frontend.vercel.app",
    "https://custom-repair-admin.vercel.app",
    "https://custom-repair-client.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(ALLOWED_ORIGINS)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print(f"GLOBAL ERROR: {exc}")
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": str(exc), "type": type(exc).__name__},
    )

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

def run_expiry_check():
    """Mark assigned/claimed jobs as expired if their time window has passed."""
    db = SessionLocal()
    try:
        now = datetime.now()
        expired_candidates = (
            db.query(JobAssignment)
            .filter(JobAssignment.status.in_(["assigned", "claimed"]))
            .all()
        )
        for a in expired_candidates:
            if not a.booking:
                continue
            try:
                date_str = a.booking.preferred_date
                time_str = a.booking.preferred_time
                if not date_str:
                    continue
                if not time_str or any(x in time_str.lower() for x in ["flex", "asap"]):
                    end_dt = datetime.strptime(f"{date_str} 11:59 PM", "%Y-%m-%d %I:%M %p")
                else:
                    end_part = time_str.split("-")[-1].strip()
                    end_dt = datetime.strptime(f"{date_str} {end_part}", "%Y-%m-%d %I:%M %p")
                if now > end_dt:
                    a.status = "expired"
                    a.booking.status = "overdue"
                    db.commit()
            except Exception:
                pass
    except Exception as e:
        print(f"Database error in expiry check: {e}")
    finally:
        db.close()


# ─── Public Availability API ──────────────────────────────

@app.get("/api/public/available-slots")
def get_public_slots(
    service: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Groups available 2-hour windows from ALL active workers matching the service category.
    """
    from collections import defaultdict
    query = db.query(WorkerSlot).join(Worker).filter(
        WorkerSlot.is_booked == False,
        Worker.is_active == True
    )
    
    # Simple skill matching
    if service:
        required = extract_required_skills(service)
        for skill in required:
            query = query.filter(Worker.specializations.contains(skill))

    slots = query.all()
    
    # Aggregate and segment: result[date] = [ {time: "...", id: "..."} ]
    result = defaultdict(list)
    seen_windows = set() # (date, window) to deduplicate multi-worker overlaps

    for s in slots:
        for window in segment_to_2h(s.start_time, s.end_time):
            key = (s.slot_date, window)
            if key not in seen_windows:
                result[s.slot_date].append({"id": s.id, "time": window})
                seen_windows.add(key)
        
    # Sort dates and windows for clean UI
    sorted_result = {}
    for date in sorted(result.keys()):
        # Sort by parsing the start time of the window (e.g. "09:00 AM")
        sorted_result[date] = sorted(result[date], key=lambda x: parse_time_to_minutes(x["time"].split(" – ")[0]))
        
    return sorted_result




# ─── Schedule Booking API ─────────────────────────────────

@app.post("/api/schedule")
def create_booking(
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

    # Auto-create a pending JobAssignment so it's tracked from the start
    assignment = JobAssignment(
        booking=booking,
        status="pending",
    )
    
    # We no longer reserve specific slots or auto-assign workers. 
    # The booking simply goes to the 'Open Market' as a pending job.

    db.add(assignment)
    db.commit()
    db.refresh(booking)
    db.refresh(assignment)

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
    
    if slot.is_booked:
        raise HTTPException(status_code=400, detail="Cannot delete a booked slot")
    
    # If this slot was tentatively linked to a booking (e.g. from older logic)
    # we explicitly unlink it to prevent ghosting.
    if slot.booking:
        slot.booking.status = "pending"
        slot.is_booked = False
        slot.booking_id = None
        db.flush()

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





@app.get("/api/workers/pending-jobs")
def pending_jobs(current: Worker = Depends(get_current_worker), db: Session = Depends(get_db)):
    """Unassigned pending jobs filtered to match this worker's specialization. Redacted for privacy."""
    assignments = (
        db.query(JobAssignment)
        .filter(JobAssignment.status == "pending")
        .order_by(JobAssignment.created_at.desc())
        .all()
    )
    worker_skills = current.to_dict().get("specializations", ["general"])
    
    # Pre-fetch all slots for this worker to speed up filtering
    worker_slots_all = db.query(WorkerSlot).filter(WorkerSlot.worker_id == current.id).all()
    
    # Group slots by date for efficient lookup
    from collections import defaultdict
    slots_by_date = defaultdict(list)
    for s in worker_slots_all:
        slots_by_date[s.slot_date].append(s)

    filtered = []
    for a in assignments:
        if not a.booking: continue
        
        # 1. Skill Check
        required = extract_required_skills(a.booking.service)
        if not set(required).issubset(set(worker_skills)):
            continue
            
        # 2. Availability Check
        job_date = a.booking.preferred_date
        job_time = a.booking.preferred_time
        day_slots = slots_by_date.get(job_date, [])
        
        if does_worker_match_time(job_time, day_slots):
            filtered.append(a)

    raw_list = filtered
        
    # Apply server-side redaction for unclaimed leads
    return [redact_assignment(a.to_dict()) for a in raw_list]



@app.patch("/api/jobs/{assignment_id}/status")
def update_job_status(
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
        "claimed": ["in_progress", "rejected"],
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

    if new_status == "claimed":
        assignment.accepted_at = now
        booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == assignment.booking_id).first()
        if booking:
            booking.status = "confirmed"
    elif new_status == "rejected":
        assignment.worker_id = None
        assignment.status = "pending"
        assignment.assigned_at = None
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
    elif new_status == "not_completed":
        booking = db.query(ScheduleBooking).filter(ScheduleBooking.id == assignment.booking_id).first()
        if booking:
            booking.status = "overdue"

    if payload.notes:
        assignment.worker_notes = payload.notes

    db.commit()
    db.refresh(assignment)

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
def admin_assign_job(
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
        assignment.status = "assigned"
    else:
        assignment = JobAssignment(
            booking_id=payload.booking_id,
            worker_id=payload.worker_id,
            assigned_by=current.id,
            assigned_at=now,
            status="assigned",
        )
        db.add(assignment)

    booking.status = "assigned"
    db.commit()
    db.refresh(assignment)

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
            joinedload(ScheduleBooking.assignment).joinedload(JobAssignment.worker)
        )
        .order_by(ScheduleBooking.created_at.desc())
        .limit(100)
        .all()
    )
    
    bookings = []
    for b in bookings_raw:
        d = b.to_dict()
        d["assignment"] = b.assignment.to_dict() if b.assignment else None
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





# ─── End of API ───────────────────────────────────────────


