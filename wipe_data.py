
from database import SessionLocal, ScheduleBooking, JobAssignment, WorkerSlot, ChatSession, ChatMessage, User
from sqlalchemy import text

def wipe_data():
    db = SessionLocal()
    try:
        print("Cleaning up database tables...")
        
        # We use truncate with CASCADE for a truly fresh start if supported, 
        # or just delete all records in order of dependencies.
        
        # 1. Delete Chat Data
        db.query(ChatMessage).delete()
        db.query(ChatSession).delete()
        print("- Deleted all chat sessions and messages.")
        
        # 2. Delete Booking & Assignment Data
        db.query(JobAssignment).delete()
        db.query(WorkerSlot).delete()
        db.query(ScheduleBooking).delete()
        print("- Deleted all job assignments, slots, and bookings.")
        
        # Optional: Delete guest users (those without passwords)
        # Note: We keep Workers and Admins so you don't have to re-register.
        db.query(User).filter(User.password_hash == None).delete()
        print("- Deleted guest customer records.")
        
        db.commit()
        print("Database wipe complete. You are starting fresh!")
        
    except Exception as e:
        print(f"Error during wipe: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    wipe_data()
