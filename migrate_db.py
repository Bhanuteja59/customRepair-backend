from database import engine
from sqlalchemy import text

def migrate():
    print("Starting migration: Dropping unique constraint on booking_id...")
    with engine.connect() as conn:
        try:
            # 1. Drop the unique constraint
            conn.execute(text("ALTER TABLE job_assignments DROP CONSTRAINT IF EXISTS job_assignments_booking_id_key;"))
            
            # 2. Update existing rejected assignments to ensure they don't block future work (optional safety)
            # Since we removed the unique constraint, this is just to be clean.
            
            conn.commit()
            print("Successfully dropped 'job_assignments_booking_id_key'.")
        except Exception as e:
            print(f"Error during migration: {e}")
            conn.rollback()

if __name__ == "__main__":
    migrate()
