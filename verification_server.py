import hashlib
import os
import re
import secrets
import threading
import time
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# Render sets RENDER=true and injects every variable from the dashboard's Environment tab.
# Locally, variables come from a .env file instead.
ON_RENDER = bool(os.getenv("RENDER"))
if not ON_RENDER:
    load_dotenv()

# ---------------------------------------------------------------- settings
MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI is not set. On Render add it under Environment; locally copy .env.example to .env."
    )
PORT = int(os.getenv("PORT", "8080"))  # Render injects PORT (default 10000)
# Bearer-token APIs use wildcard CORS and do not rely on browser cookies.
SESSION_TTL_MINUTES = 720
# Header that carries the real visitor IP when behind a proxy. Render sits behind Cloudflare, which sets True-Client-IP.
# Leave empty to use the socket address (local development).
CLIENT_IP_HEADER = (os.getenv("CLIENT_IP_HEADER") or ("True-Client-IP" if ON_RENDER else "")).lower()

RATE_LIMIT_MESSAGE = "Sorry, you're going too fast. Please come back later."
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 15 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MESSAGE = "Password must be 10-64 characters with an uppercase letter, a lowercase letter, a number and a symbol."

# ---------------------------------------------------------------- database
client = MongoClient(MONGODB_URI)
db = client.get_default_database("authdb")
users = db["users"]        # {firstName,lastName,username,email,usernameLower,emailLower,passwordHash,createdAt}
sessions = db["sessions"]  # {_id: sha256(sessionKey), userId, expiresAt}


@asynccontextmanager
async def lifespan(app: FastAPI):
    users.create_index([("usernameLower", ASCENDING)], unique=True)
    existing_email_index = users.index_information().get("emailLower_1")
    if existing_email_index and not existing_email_index.get("partialFilterExpression"):
        users.drop_index("emailLower_1")
    users.create_index([("emailLower", ASCENDING)], unique=True, partialFilterExpression={"emailLower": {"$type": "string"}})
    users.create_index([("discordId", ASCENDING)], unique=True, sparse=True)
    sessions.create_index([("expiresAt", ASCENDING)], expireAfterSeconds=0)  # Mongo auto-deletes expired keys
    yield


# ---------------------------------------------------------------- rate limiter (in memory, one instance)
class RateLimiter:
    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    @staticmethod
    def _prune(dq, window):
        cutoff = time.monotonic() - window
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _blocked(self, key, mx, window):
        dq = self._hits.get(key)
        if not dq:
            return False
        self._prune(dq, window)
        return len(dq) >= mx

    def _add(self, key, window):
        dq = self._hits.setdefault(key, deque())
        self._prune(dq, window)
        dq.append(time.monotonic())
        if len(self._hits) > 10_000:
            self._cleanup()

    def _cleanup(self):
        cutoff = time.monotonic() - 3600
        for k in [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]:
            del self._hits[k]

    def is_blocked(self, key, mx, window):
        with self._lock:
            return self._blocked(key, mx, window)

    def record(self, key, window):
        with self._lock:
            self._add(key, window)

    def try_acquire(self, key, mx, window):
        """True if allowed (and counted), False if over the limit."""
        with self._lock:
            if self._blocked(key, mx, window):
                return False
            self._add(key, window)
            return True

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)


limiter = RateLimiter()

# ---------------------------------------------------------------- helpers
DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(12))  # equalizes timing for unknown accounts


def client_ip(request: Request) -> str:
    if CLIENT_IP_HEADER:
        value = request.headers.get(CLIENT_IP_HEADER)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def too_many() -> JSONResponse:
    return JSONResponse({"error": RATE_LIMIT_MESSAGE}, status_code=429)


def normalize(s: str) -> str:
    return (s or "").strip().lower()[:254]


def password_ok(pw: str) -> bool:
    if not 10 <= len(pw) <= 64 or len(pw.encode("utf-8")) > 72:  # bcrypt only reads 72 bytes
        return False
    return (
        any(c.isupper() for c in pw)
        and any(c.islower() for c in pw)
        and any(c.isdigit() for c in pw)
        and any(not c.isalnum() and not c.isspace() for c in pw)
    )


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def find_user(ident: str):
    return users.find_one({"emailLower": ident} if "@" in ident else {"usernameLower": ident})


# ---------------------------------------------------------------- app
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Limits "Next" (lookup) and "Create Account" (signup). Login failures are limited in login()."""
    if request.method == "POST":
        ip = client_ip(request)
        path = request.url.path
        if path == "/api/auth/lookup" and not limiter.try_acquire(f"lookup:{ip}", 10, 60):          # 10 / minute
            return too_many()
        if path == "/api/auth/signup" and not limiter.try_acquire(f"signup:{ip}", 5, 3600):         # 5 / hour
            return too_many()
        if path == "/api/auth/me" and not limiter.try_acquire(f"me:{ip}", 60, 60):                  # 60 / minute
            return too_many()
    return await call_next(request)


# Added after the rate limiter so it wraps it: 429 responses still carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=False,
    max_age=3600,
)


class LookupRequest(BaseModel):
    identifier: str = ""


class LoginRequest(BaseModel):
    identifier: str = ""
    password: str = ""


class SignupRequest(BaseModel):
    username: str = ""
    password: str = ""
    confirmPassword: str = ""


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    """Public endpoint for uptime monitors (UptimeBot). Not rate limited, touches no data."""
    return {"status": "up"}


@app.post("/api/auth/lookup")
def lookup(body: LookupRequest):
    """Page 1 "Next": does this username/email exist? Always 200 with the same shape."""
    ident = normalize(body.identifier)
    return {"exists": bool(ident) and find_user(ident) is not None}


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request):
    """Page 2 "Log In": verifies identifier + password together, then issues the session key."""
    ident = normalize(body.identifier)
    pw = body.password.encode("utf-8")
    if len(pw) > 72:
        pw = b""

    ip_key, acct_key = f"login-ip:{client_ip(request)}", f"login-acct:{ident}"
    if limiter.is_blocked(ip_key, LOGIN_MAX_FAILURES, LOGIN_WINDOW_S) or limiter.is_blocked(
        acct_key, LOGIN_MAX_FAILURES, LOGIN_WINDOW_S
    ):
        return too_many()

    user = find_user(ident) if ident else None
    stored = user["passwordHash"].encode("utf-8") if user else DUMMY_HASH
    ok = bcrypt.checkpw(pw, stored) and user is not None

    if not ok:
        limiter.record(ip_key, LOGIN_WINDOW_S)
        limiter.record(acct_key, LOGIN_WINDOW_S)
        return JSONResponse({"error": "Incorrect username or password."}, status_code=401)

    limiter.clear(ip_key)
    limiter.clear(acct_key)

    session_key = secrets.token_urlsafe(32)  # 256 bits
    sessions.insert_one(
        {
            "_id": sha256(session_key),  # only the hash is stored
            "userId": user["_id"],
            "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=SESSION_TTL_MINUTES),
        }
    )
    return {"sessionKey": session_key, "expiresInSeconds": SESSION_TTL_MINUTES * 60}


@app.post("/api/auth/signup")
def signup(body: SignupRequest):
    """Create Account. Field errors come back as {"errors": {"field": "message"}}."""
    username = body.username.strip()
    pw, confirm = body.password, body.confirmPassword

    errors = {}
    if not USERNAME_RE.match(username):
        errors["username"] = "Username must be 3-30 characters: letters, numbers, . _ -"
    if not password_ok(pw):
        errors["password"] = PASSWORD_MESSAGE
    if pw != confirm:
        errors["confirmPassword"] = "Passwords don't match."
    if errors:
        return JSONResponse({"errors": errors}, status_code=400)

    username_lower = username.lower()
    if users.find_one({"usernameLower": username_lower}, {"_id": 1}):
        errors["username"] = "Username is already used."
    if errors:
        return JSONResponse({"errors": errors}, status_code=409)

    try:
        users.insert_one(
            {
                "username": username,
                "usernameLower": username_lower,
                "passwordHash": bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8"),
                "createdAt": datetime.now(timezone.utc),
                "whitelisted": False,
            }
        )
    except DuplicateKeyError as e:  # two sign-ups racing past the checks above
        return JSONResponse({"errors": {"username": "Username is already used."}}, status_code=409)

    return JSONResponse({"created": True}, status_code=201)


@app.post("/api/auth/me")
def me(authorization: Optional[str] = Header(default=None)):
    """Dashboard gate: confirms the session key with the server and returns who is signed in."""
    unauthorized = JSONResponse({"error": "Not signed in."}, status_code=401, headers={"Cache-Control": "no-store"})
    if not authorization or not authorization.startswith("Bearer "):
        return unauthorized
    sess = sessions.find_one({"_id": sha256(authorization[7:].strip())})
    if not sess:
        return unauthorized
    expires = sess["expiresAt"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        return unauthorized
    user = users.find_one({"_id": sess.get("userId")}, {"firstName": 1, "lastName": 1, "username": 1, "email": 1, "discordId": 1, "discordUsername": 1, "whitelisted": 1})
    if not user and sess.get("discordId"):
        user = users.find_one({"discordId": sess["discordId"]}, {"firstName": 1, "lastName": 1, "username": 1, "email": 1, "discordId": 1, "discordUsername": 1, "whitelisted": 1})
    if not user:
        return unauthorized
    discord_id = user.get("discordId") or sess.get("discordId")
    result = {k: user[k] for k in ("firstName", "lastName", "username", "email", "discordUsername") if k in user}
    is_primary_owner = str(user.get("username", "")).casefold() == "glitchstorm7320"
    result["whitelisted"] = bool(user.get("whitelisted", False) or is_primary_owner)
    result["discordLinked"] = bool(discord_id)
    if discord_id:
        result["discordId"] = discord_id
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


class StaffAccountDeleteRequest(BaseModel):
    discordId: str = ""


@app.post("/api/internal/staff/delete-account")
def staff_delete_dashboard_account(body: StaffAccountDeleteRequest, x_staff_manager_key: Optional[str] = Header(default=None)):
    expected = os.getenv("STAFF_ACCOUNT_DELETE_SECRET", "").strip()
    if not expected or not x_staff_manager_key or not secrets.compare_digest(x_staff_manager_key, expected):
        return JSONResponse({"error": "Unauthorized internal request."}, status_code=403)
    discord_id = body.discordId.strip()
    if not discord_id.isdigit():
        return JSONResponse({"error": "Invalid Discord account ID."}, status_code=400)
    user = users.find_one({"discordId": discord_id}, {"_id": 1})
    if not user:
        return JSONResponse({"error": "No linked dashboard account was found for this Discord user."}, status_code=404)
    sessions.delete_many({"userId": user["_id"]})
    users.delete_one({"_id": user["_id"]})
    return {"ok": True, "deleted": True}


class DashboardAccessRequest(BaseModel):
    username: str = ""
    discordId: str = ""
    allowed: bool = False


@app.post("/api/internal/staff/dashboard-access")
def staff_set_dashboard_access(body: DashboardAccessRequest, x_staff_manager_key: Optional[str] = Header(default=None)):
    expected = os.getenv("STAFF_ACCOUNT_DELETE_SECRET", "").strip()
    if not expected or not x_staff_manager_key or not secrets.compare_digest(x_staff_manager_key, expected):
        return JSONResponse({"error": "Unauthorized internal request."}, status_code=403)

    discord_id = body.discordId.strip()
    username = body.username.strip()
    if discord_id:
        if not discord_id.isdigit():
            return JSONResponse({"error": "Invalid Discord account ID."}, status_code=400)
        user = users.find_one({"discordId": discord_id}, {"_id": 1, "username": 1, "discordId": 1})
    elif username and len(username) <= 100:
        user = users.find_one({"usernameLower": username.lower()}, {"_id": 1, "username": 1, "discordId": 1})
    else:
        return JSONResponse({"error": "Provide a valid website username or Discord account ID."}, status_code=400)

    if not user:
        return JSONResponse({"error": "No linked website account was found."}, status_code=404)
    users.update_one(
        {"_id": user["_id"]},
        {"$set": {"whitelisted": bool(body.allowed), "dashboardAccessUpdatedAt": datetime.now(timezone.utc)}},
    )
    if not body.allowed:
        sessions.delete_many({"userId": user["_id"]})
    return {
        "ok": True,
        "username": user.get("username", ""),
        "discordId": user.get("discordId", discord_id),
        "whitelisted": bool(body.allowed),
    }


class AccountUpdateRequest(BaseModel):
    username: str = ""
    currentPassword: str = ""
    newPassword: str = ""
    confirmPassword: str = ""


@app.post("/api/auth/account/update")
def update_account(body: AccountUpdateRequest, authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        return JSONResponse({"error": "Sign in before changing account settings."}, status_code=401)
    session_key = authorization[7:].strip()
    session_hash = sha256(session_key)
    sess = sessions.find_one({"_id": session_hash})
    if not sess or not sess.get("userId"):
        return JSONResponse({"error": "Your session expired. Sign in again."}, status_code=401)
    expires = sess["expiresAt"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        return JSONResponse({"error": "Your session expired. Sign in again."}, status_code=401)

    user = users.find_one({"_id": sess["userId"]})
    if not user:
        return JSONResponse({"error": "Account not found."}, status_code=404)

    username = body.username.strip()
    new_password = body.newPassword
    confirm_password = body.confirmPassword
    changing_username = bool(username) and username != user.get("username", "")
    changing_password = bool(new_password)
    errors = {}

    if not changing_username and not changing_password:
        return JSONResponse({"error": "Enter a new username or a new password."}, status_code=400)
    if changing_username and not USERNAME_RE.match(username):
        errors["username"] = "Username must be 3-30 characters: letters, numbers, . _ -"
    if changing_password:
        if not password_ok(new_password):
            errors["newPassword"] = PASSWORD_MESSAGE
        if new_password != confirm_password:
            errors["confirmPassword"] = "Passwords don't match."
    elif confirm_password:
        errors["confirmPassword"] = "Enter a new password first."

    current_hash = user.get("passwordHash")
    if changing_password and current_hash:
        if not body.currentPassword:
            errors["currentPassword"] = "Enter your current password to change your password."
        elif not bcrypt.checkpw(body.currentPassword.encode("utf-8")[:72], current_hash.encode("utf-8")):
            errors["currentPassword"] = "Your current password is incorrect."
    if errors:
        return JSONResponse({"errors": errors, "error": next(iter(errors.values()))}, status_code=400)

    updates = {}
    if changing_username:
        username_lower = username.lower()
        existing = users.find_one({"usernameLower": username_lower, "_id": {"$ne": user["_id"]}}, {"_id": 1})
        if existing:
            return JSONResponse({"errors": {"username": "Username is already used."}, "error": "Username is already used."}, status_code=409)
        updates["username"] = username
        updates["usernameLower"] = username_lower
    if changing_password:
        updates["passwordHash"] = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")
        updates["passwordUpdatedAt"] = datetime.now(timezone.utc)

    if updates:
        try:
            users.update_one({"_id": user["_id"]}, {"$set": updates})
        except DuplicateKeyError:
            return JSONResponse({"error": "Username is already used."}, status_code=409)
    if changing_password:
        sessions.delete_many({"userId": user["_id"], "_id": {"$ne": session_hash}})
    updated = users.find_one({"_id": user["_id"]}, {"username": 1})
    return JSONResponse({"ok": True, "username": updated.get("username", ""), "passwordUpdated": changing_password}, headers={"Cache-Control": "no-store"})


@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    """Deletes the session key server-side. Send "Authorization: Bearer <sessionKey>"."""
    if authorization and authorization.startswith("Bearer "):
        sessions.delete_one({"_id": sha256(authorization[7:].strip())})
    return Response(status_code=204)


# Imported by KIWI; the bot starts this ASGI app on a private loopback port.
