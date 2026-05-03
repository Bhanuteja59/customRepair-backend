"""
JWT authentication utilities and dependency injectors.
Supports two user types: 'worker' and 'admin'.
"""

import os
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from database import get_db, Worker, AdminUser, User

import bcrypt

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    # In development, we can fallback, but we should warn the user.
    # In production, this should be a mandatory environment variable.
    SECRET_KEY = "dev-secret-change-me-in-production"
    print("WARNING: JWT_SECRET_KEY not set. Using insecure default for development.")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

bearer_scheme = HTTPBearer(auto_error=False)


# ─── Password helpers ─────────────────────────────────────

def hash_password(password: str) -> str:
    # Hash a password using bcrypt directly
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(plain: str, hashed: str) -> bool:
    # Verify a password using bcrypt directly
    return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))


# ─── Token helpers ────────────────────────────────────────

def create_token(sub: str, user_type: str, role: Optional[str] = None) -> str:
    """Create a signed JWT for a worker or admin user."""
    payload = {
        "sub": sub,
        "type": user_type,   # "worker" | "admin" | "customer"
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ─── FastAPI dependencies ─────────────────────────────────

def get_current_worker(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Worker:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "worker":
        raise HTTPException(status_code=403, detail="Worker token required")
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token payload")
    worker = db.query(Worker).filter(Worker.id == payload["sub"]).first()
    if not worker or not worker.is_active:
        raise HTTPException(status_code=401, detail="Worker not found or deactivated")
    return worker


def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "admin":
        raise HTTPException(status_code=403, detail="Admin token required")
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token payload")
    admin = db.query(AdminUser).filter(AdminUser.id == payload["sub"]).first()
    if not admin or not admin.is_active:
        raise HTTPException(status_code=401, detail="Admin not found or deactivated")
    return admin


def get_optional_customer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    if not credentials:
        return None
    try:
        payload = decode_token(credentials.credentials)
        if payload.get("type") != "customer" or not payload.get("sub"):
            return None
        user = db.query(User).filter(User.id == payload["sub"]).first()
        return user if user and user.is_active else None
    except:
        return None


def get_current_customer(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "customer":
        raise HTTPException(status_code=403, detail="Customer token required")
    user = db.query(User).filter(User.id == payload["sub"]).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Account not found or deactivated")
    return user


def require_roles(*roles: str):
    """Dependency factory: ensures the admin user has one of the given roles."""
    def _check(current_admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
        if current_admin.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions. Required: {', '.join(roles)}",
            )
        return current_admin
    return _check
