from pydantic import BaseModel, EmailStr
from typing import Optional, List

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
    notif_prefs: Optional[dict] = None
    sched_prefs: Optional[dict] = None
    privacy_prefs: Optional[dict] = None

class WorkerSlotCreate(BaseModel):
    slot_date: str
    start_time: str
    end_time: str
