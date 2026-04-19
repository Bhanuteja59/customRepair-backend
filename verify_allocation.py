import sys
import os
from datetime import datetime, date
import uuid

# Add the project root to sys.path
sys.path.append(os.getcwd())

from database import SessionLocal, Worker, WorkerSlot, ScheduleBooking, JobAssignment, User

def verify():
    db = SessionLocal()
    try:
        # 0. Create dummy user
        u = db.query(User).filter(User.email == "test_customer@example.com").first()
        if not u:
            u = User(name="Test Customer", email="test_customer@example.com", customer_id="C-TEST", phone="12345", address="Test")
            db.add(u)
            db.flush()

        # 1. Clean up or Find test workers
        w1 = db.query(Worker).filter(Worker.email == "test_force1@example.com").first()
        if not w1:
            w1 = Worker(name="Worker High Workload", email="test_force1@example.com", password_hash="dummy", specializations="plumbing", is_active=True, is_available=True)
            db.add(w1)
        
        w2 = db.query(Worker).filter(Worker.email == "test_force2@example.com").first()
        if not w2:
            w2 = Worker(name="Worker Low Workload", email="test_force2@example.com", password_hash="dummy", specializations="plumbing", is_active=True, is_available=True)
            db.add(w2)
        
        db.flush()

        # 2. Add historical jobs
        # W1 gets 3 old jobs
        if db.query(JobAssignment).filter(JobAssignment.worker_id == w1.id).count() < 3:
            for i in range(3):
                dummy_b = ScheduleBooking(user_id=u.id, service="Historical Test", status="completed")
                db.add(dummy_b)
                db.flush()
                db.add(JobAssignment(booking_id=dummy_b.id, worker_id=w1.id, status="completed"))
        
        # W2 gets 1 old job
        if db.query(JobAssignment).filter(JobAssignment.worker_id == w2.id).count() < 1:
            dummy_b = ScheduleBooking(user_id=u.id, service="Historical Test", status="completed")
            db.add(dummy_b)
            db.flush()
            db.add(JobAssignment(booking_id=dummy_b.id, worker_id=w2.id, status="completed"))
        
        db.flush()

        # 3. Add Slots (Same shift for both)
        test_date_str = "2026-04-30"
        for w in [w1, w2]:
            exists = db.query(WorkerSlot).filter(WorkerSlot.worker_id == w.id, WorkerSlot.slot_date == test_date_str).first()
            if not exists:
                db.add(WorkerSlot(worker_id=w.id, slot_date=test_date_str, start_time="09:00 AM", end_time="11:00 AM", is_available=True))
        
        db.commit()

        print(f"DEBUG: Worker 1 Jobs: {db.query(JobAssignment).filter(JobAssignment.worker_id == w1.id).count()}")
        print(f"DEBUG: Worker 2 Jobs: {db.query(JobAssignment).filter(JobAssignment.worker_id == w2.id).count()}")

        # 4. Trigger Allocation via perform_auto_allocation logic
        from main import perform_auto_allocation
        
        dummy_booking = ScheduleBooking(user_id=u.id, service="Plumbing Issue", preferred_date=test_date_str, preferred_time="09:00 AM - 11:00 AM")
        
        # Test 1: Both free. W2 (Least jobs) should win.
        best_w, slot = perform_auto_allocation(db, dummy_booking)
        print(f"RESULT 1 (Both Free): {best_w.name if best_w else 'NONE'} (Expected: Worker Low Workload)")

        # Test 2: Occupy W2 (make him busy)
        # Add a conflict job for W2
        dummy_b2 = ScheduleBooking(user_id=u.id, service="Conflict Check", status="confirmed")
        db.add(dummy_b2)
        db.flush()
        db.add(JobAssignment(booking_id=dummy_b2.id, worker_id=w2.id, status="claimed"))
        db.flush()
        
        # Now W1 is free, W2 is busy. W1 should win (Tier 1: No conflict).
        best_w, slot = perform_auto_allocation(db, dummy_booking)
        print(f"RESULT 2 (W2 Busy): {best_w.name if best_w else 'NONE'} (Expected: Worker High Workload)")

        # Test 3: Occupy W1 too. Both busy.
        dummy_b3 = ScheduleBooking(user_id=u.id, service="Conflict Check 2", status="confirmed")
        db.add(dummy_b3)
        db.flush()
        db.add(JobAssignment(booking_id=dummy_b3.id, worker_id=w1.id, status="claimed"))
        db.flush()
        
        # Both busy. Fallback should pick the one with least total jobs (W2 has 2 total, W1 has 4 total).
        best_w, slot = perform_auto_allocation(db, dummy_booking)
        print(f"RESULT 3 (Both Busy - Fallback): {best_w.name if best_w else 'NONE'} (Expected: Worker Low Workload)")

    finally:
        db.rollback() # Don't persist test junk
        db.close()

if __name__ == "__main__":
    verify()
