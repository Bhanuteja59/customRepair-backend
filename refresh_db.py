from database import engine, Base, create_tables

def refresh():
    print("🚀 Starting Database Refresh...")
    
    # 1. Drop all existing tables
    print("🗑️  Dropping old tables...")
    Base.metadata.drop_all(bind=engine)
    
    # 2. Recreate them with the new schema (including user_id)
    print("🏗️  Recreating tables with new schema...")
    create_tables()
    
    print("\n✅ Database is now synchronized with your code!")
    print("You can now start your server: uvicorn main:app --reload")

if __name__ == "__main__":
    refresh()
