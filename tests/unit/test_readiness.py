"""Testeaza ca /readiness nu expune unui apelant neautentificat detaliul
intern al unei erori de conexiune la baza de date (host/port/user, mesaje de
driver) -- issue #11. Detaliul complet trebuie doar logat server-side."""
from __future__ import annotations

from app.database import engine


def test_readiness_ok_when_db_reachable(client):
    resp = client.get("/readiness")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readiness_does_not_leak_exception_detail_on_db_failure(client, monkeypatch):
    def _broken_connect(*args, **kwargs):
        raise Exception("connection to server at \"10.0.0.5\", port 5432 failed: password authentication failed for user \"ems\"")

    monkeypatch.setattr(engine, "connect", _broken_connect)

    resp = client.get("/readiness")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert body["detail"] == "Serviciul nu este pregatit."
    assert "10.0.0.5" not in resp.text
    assert "password" not in resp.text.lower()
    assert "ems" not in resp.text
