from database import SessionLocal, Worker, WorkerSlot, create_tables
from auth import hash_password
from datetime import datetime, timedelta
import uuid

def seed_slots():
    db = SessionLocal()
    try:
        # 1. Ensure we have at least one active worker
        worker = db.query(Worker).filter(Worker.is_active == True).first()
        if not worker:
            print("No active worker found. Creating one...")
            worker = Worker(
                name="John Doe",
                email="john@example.com",
                phone="555-0199",
                password_hash=hash_password("dummy"),
                specializations="ac,heating,water,electrical,thermostat,other",
                is_active=True,
                is_available=True
            )
            db.add(worker)
            db.commit()
            db.refresh(worker)
        
        print(f"Using worker: {worker.name} ({worker.id})")

        # 2. Generate slots for the next 60 days
        now = datetime.utcnow()
        slots_created = 0
        
        # Define some common time windows
        windows = [
            ("08:00 AM", "10:00 AM"),
            ("10:00 AM", "12:00 PM"),
            ("12:00 PM", "02:00 PM"),
            ("02:00 PM", "04:00 PM"),
            ("04:00 PM", "06:00 PM"),
            ("06:00 PM", "08:00 PM"),
        ]

        for i in range(60):
            date_obj = now + timedelta(days=i)
            date_str = date_obj.date().isoformat()
            
            # Skip some days to make it look realistic
            if date_obj.weekday() >= 5: # Weekend
                # Maybe fewer slots on weekends
                selected_windows = windows[:2]
            else:
                selected_windows = windows

            for start, end in selected_windows:
                # Check if slot already exists
                existing = db.query(WorkerSlot).filter(
                    WorkerSlot.worker_id == worker.id,
                    WorkerSlot.slot_date == date_str,
                    WorkerSlot.start_time == start
                ).first()
                
                if not existing:
                    slot = WorkerSlot(
                        worker_id=worker.id,
                        slot_date=date_str,
                        start_time=start,
                        end_time=end,
                        is_booked=False
                    )
                    db.add(slot)
                    slots_created += 1
        
        db.commit()
        print(f"Successfully created {slots_created} slots across 60 days.")

    except Exception as e:
        print(f"Error seeding slots: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    seed_slots()
