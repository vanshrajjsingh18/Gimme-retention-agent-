"""Test fixtures: an isolated database and an authenticated API client."""
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture(scope="session")
def _test_db_url() -> Iterator[str]:
    """A file-backed SQLite database, isolated from the developer's data."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="gimme-test-")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["DATABASE_URL"] = url
    os.environ["ENABLE_SCHEDULER"] = "false"
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["ADMIN_EMAIL"] = "admin@gimmedelivery.co.nz"
    os.environ["ADMIN_PASSWORD"] = "GimmeAdmin123!"
    yield url
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture(scope="session")
def engine(_test_db_url):
    from app.core import database

    test_engine = create_engine(
        _test_db_url, connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(test_engine, "connect")
    def _pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    TestSession = sessionmaker(bind=test_engine, autocommit=False, autoflush=False, future=True)

    # Point every module that captured the engine/session at the test database.
    database.engine = test_engine
    database.SessionLocal = TestSession

    import app.models  # noqa: F401 - registers tables

    database.Base.metadata.create_all(test_engine)
    return test_engine


@pytest.fixture()
def db(engine) -> Iterator[Session]:
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def bootstrapped(engine):
    """Baseline configuration: admin user, brand, compliance rules, segments."""
    from app.core.database import SessionLocal
    from app.services.bootstrap import bootstrap

    session = SessionLocal()
    try:
        bootstrap(session)
    finally:
        session.close()
    return True


@pytest.fixture(scope="session")
def seeded(bootstrapped):
    """A small but behaviourally complete synthetic dataset."""
    from app.core.database import SessionLocal
    from app.services.intelligence import refresh_all_customers
    from app.services.seed import generate_customers
    from app.services.segments import refresh_all_segments

    session = SessionLocal()
    try:
        generate_customers(session, count=120, seed=4242)
        refresh_all_customers(session)
        refresh_all_segments(session)
        session.commit()
    finally:
        session.close()
    return True


@pytest.fixture()
def client(engine, bootstrapped) -> Iterator[TestClient]:
    """A TestClient whose get_db dependency uses the test session."""
    from app.core.database import SessionLocal, get_db
    from app.main import app

    def override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    # The app's own lifespan would re-create tables against the real engine, so
    # drive the client without it.
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_headers(client) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@gimmedelivery.co.nz", "password": "GimmeAdmin123!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def api_key(client, auth_headers) -> str:
    response = client.post(
        "/api/v1/api-keys", json={"name": "test-key"}, headers=auth_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["api_key"]
