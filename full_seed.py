from seed_admin import seed as seed_admin
from seed_slots import seed_slots
import os
import sys

# Ensure backend is in path if running from here
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def run_all():
    print("Starting Full Database Seeding...")
    
    print("\n--- Seeding Admin ---")
    seed_admin()
    
    print("\n--- Seeding Workers & Slots ---")
    seed_slots()
    
    print("\nSeeding Complete!")
    print("Worker Login: john@example.com / dummy")
    print("Admin Login: admin@customrepair.com / password123")

if __name__ == "__main__":
    run_all()
