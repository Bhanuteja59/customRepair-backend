import copy
from datetime import datetime, timedelta
from typing import List, Optional
from sqlalchemy.orm import Session
from database import User, WorkerSlot

def segment_to_2h(start_str: str, end_str: str):
    """Yields 2-hour time window strings between start and end."""
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

def generate_customer_id(db: Session):
    from sqlalchemy import func
    count = db.query(User).count()
    return f"CR-{1001 + count}"

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
