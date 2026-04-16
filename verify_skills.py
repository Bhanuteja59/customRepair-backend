
from database import SessionLocal, Worker
import json

def test_skill_parsing():
    db = SessionLocal()
    try:
        # Find a worker with spaces in skills or create one
        name = "Space Tester"
        worker = db.query(Worker).filter(Worker.name == name).first()
        if not worker:
            worker = Worker(
                name=name,
                email="tester@space.com",
                password_hash="...",
                specializations="electrical,  plumbing, hvac " # intentionally messy
            )
            db.add(worker)
            db.commit()
            db.refresh(worker)
        
        # Check to_dict()
        data = worker.to_dict()
        print(f"Parsed skills: {data['specializations']}")
        
        expected = ["electrical", "plumbing", "hvac"]
        for s in expected:
            if s not in data['specializations']:
                print(f"FAILED: '{s}' not found in {data['specializations']}")
                return
        print("SUCCESS: Skills parsed correctly without whitespace.")
        
    finally:
        db.close()

if __name__ == "__main__":
    test_skill_parsing()
