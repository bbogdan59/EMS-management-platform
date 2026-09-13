from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    port = _free_port()
    db_name = "ems_e2e"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"postgresql+psycopg://ems:ems@localhost:5432/{db_name}"
    env["REDIS_URL"] = "redis://localhost:6379/2"
    env["SECRET_KEY"] = "e2e-test-secret"
    env["BOOTSTRAP_ADMIN_TOKEN"] = "e2e-bootstrap-token"
    env["ENVIRONMENT"] = "test"
    env["SESSION_COOKIE_SECURE"] = "false"
    env["LEGACY_CLAIM_CODE_ENABLED"] = "true"

    subprocess.run(
        ["psql", "-h", "localhost", "-U", "ems", "-c", f"DROP DATABASE IF EXISTS {db_name};"],
        env={**os.environ, "PGPASSWORD": "ems"}, cwd=ROOT,
    )
    subprocess.run(
        ["psql", "-h", "localhost", "-U", "ems", "-c", f"CREATE DATABASE {db_name};"],
        env={**os.environ, "PGPASSWORD": "ems"}, cwd=ROOT,
    )
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True)

    import redis as redis_lib

    redis_lib.from_url(env["REDIS_URL"]).flushdb()  # reseteaza rate limiting intre rulari

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("Serverul de test (live_server) nu a pornit.")

    time.sleep(1)
    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
