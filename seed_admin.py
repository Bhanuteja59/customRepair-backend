from database import SessionLocal, AdminUser
from auth import hash_password
import uuid

def seed():
    db = SessionLocal()
    try:
        # Check if admin already exists
        existing = db.query(AdminUser).filter(AdminUser.email == "admin@customrepair.com").first()
        if existing:
            print("Admin user already exists.")
            return

        admin = AdminUser(
            id="ADM-" + str(uuid.uuid4())[:8].upper(),
            name="System Admin",
            email="admin@customrepair.com",
            password_hash=hash_password("password123"),
            role="admin",
            department="Management",
            is_active=True
        )
        db.add(admin)
        db.commit()
        print("Admin user created successfully!")
        print("Email: admin@customrepair.com")
        print("Password: password123")
    except Exception as e:
        print(f"Error seeding admin: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    seed()
