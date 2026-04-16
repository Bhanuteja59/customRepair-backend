
from database import SessionLocal, ScheduleBooking, JobAssignment, WorkerSlot
from datetime import datetime

def cleanup():
    db = SessionLocal()
    try:
        # 1. Delete test bookings starting with BK-1A3 (my test ID)
        test_bookings = db.query(ScheduleBooking).filter(ScheduleBooking.id.like('BK-1A3%')).all()
        for b in test_bookings:
            print(f"Deleting test booking {b.id}")
            # Delete related assignments
            db.query(JobAssignment).filter(JobAssignment.booking_id == b.id).delete()
            # Unlink slots
            db.query(WorkerSlot).filter(WorkerSlot.booking_id == b.id).update({
                "is_booked": False,
                "booking_id": None
            })
            db.delete(b)
        
        # 2. Revert any 'assigned' jobs to 'pending' if they haven't been claimed yet
        # This matches the user's request for "open schedule only"
        auto_assigned = db.query(JobAssignment).filter(JobAssignment.status == 'assigned').all()
        for a in auto_assigned:
            print(f"Reverting assignment {a.id} to pending")
            a.status = 'pending'
            a.worker_id = None
            a.assigned_at = None
            if a.booking:
                a.booking.status = 'pending'
        
        db.commit()
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    cleanup()
