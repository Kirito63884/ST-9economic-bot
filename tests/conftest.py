import base64
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
import httpx

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
test_data = PROJECT_DIR / "tests" / ".test-data"
test_data.mkdir(exist_ok=True)
os.environ["DATABASE_URL"] = (
    f"sqlite:///{(test_data / (uuid.uuid4().hex + '.db')).as_posix()}"
)
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
for key in (
    "REQUIRED_GUILD_ID",
    "REQUIRED_ROLE_ID",
    "DISCORD_BOT_TOKEN",
    "ADMIN_DISCORD_ID",
):
    os.environ.pop(key, None)

import asyncio
import main
import runtime
from database import AppRuntime, GameHistory, LoginLink, SessionLocal, User
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner


@pytest.fixture(autouse=True)
def clean_database(monkeypatch):
    main.request_guard.reset()
    runtime.transaction_lock = asyncio.Lock()
    main.role_check_lock = asyncio.Lock()
    with SessionLocal() as db:
        db.query(LoginLink).delete()
        db.query(GameHistory).delete()
        db.query(User).delete()
        row = db.get(AppRuntime, 1)
        row.payload = "{}"
        row.next_user_id = 1
        db.commit()
    main.restore_runtime("{}")
    monkeypatch.setattr(main, "ADMIN_DISCORD_ID", "100000000000000000")
    monkeypatch.setattr(main, "REQUIRED_GUILD_ID", None)
    monkeypatch.setattr(main, "REQUIRED_ROLE_ID", None)
    monkeypatch.setattr(main, "DISCORD_BOT_TOKEN", None)


@pytest.fixture
def make_user():
    def create(uid=101, balance=1000, **values):
        with SessionLocal() as db:
            user = User(
                id=uid,
                discord_id=str(uid),
                username=f"user{uid}",
                balance=balance,
                **values,
            )
            db.add(user)
            db.commit()
        return uid

    return create


def session_cookie(uid):
    return (
        TimestampSigner(main.SECRET_KEY)
        .sign(
            base64.b64encode(
                json.dumps({"user_id": uid, "csrf_token": "test-csrf-token"}).encode()
            )
        )
        .decode()
    )


@pytest.fixture(autouse=True)
def csrf_client_headers(monkeypatch):
    original = httpx.Client.__init__

    def initialize(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("X-ST9-CSRF", "test-csrf-token")
        original(self, *args, headers=headers, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", initialize)
    original_async = httpx.AsyncClient.__init__

    def initialize_async(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("X-ST9-CSRF", "test-csrf-token")
        original_async(self, *args, headers=headers, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize_async)


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def login_as(client):
    def login(uid):
        client.cookies.set("session", session_cookie(uid))
        return client

    return login


def wallet(uid):
    with SessionLocal() as db:
        return db.get(User, uid).balance


def saved_state():
    with SessionLocal() as db:
        return json.loads(db.get(AppRuntime, 1).payload)


def save_state():
    with SessionLocal() as db:
        db.get(AppRuntime, 1).payload = main.serialize_runtime()
        db.commit()
