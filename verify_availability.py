
from database import SessionLocal, WorkerSlot
from main import does_worker_match_time
from typing import List

def test_overlap():
    # Helper to create pseudo-slots
    def s(start, end):
        return WorkerSlot(start_time=start, end_time=end)

    test_cases = [
        # (Booking Time, Worker Slots, Expected)
        ("09:00 AM - 11:00 AM", [s("08:00 AM", "12:00 PM")], True), # Covered
        ("09:00 AM - 11:00 AM", [s("10:30 AM", "12:00 PM")], True), # Partial overlap
        ("09:00 AM - 11:00 AM", [s("12:00 PM", "02:00 PM")], False), # No overlap
        ("09:00 AM – 11:00 AM", [s("08:00 AM", "12:00 PM")], True), # EN DASH support
        ("Flexible", [s("09:00 AM", "10:00 AM")], True),           # Flex with any slot
        ("Flexible", [], False),                                    # Flex with no slots
        ("09:00 AM", [s("08:30 AM", "09:30 AM")], True),           # Single point matching
    ]

    for booking_time, slots, expected in test_cases:
        result = does_worker_match_time(booking_time, slots)
        if result == expected:
            print(f"PASS: '{booking_time}' matching {len(slots)} slots -> {result}")
        else:
            print(f"FAIL: '{booking_time}' matching {len(slots)} slots -> Expected {expected}, got {result}")

if __name__ == "__main__":
    test_overlap()
