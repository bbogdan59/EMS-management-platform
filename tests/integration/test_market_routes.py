from __future__ import annotations

from datetime import date

from app.core.rate_limit import reset_key
from tests.factories import make_market_day, make_user
from tests.web_helpers import login


def test_market_page_requires_login(client):
    resp = client.get("/market/prices", follow_redirects=False)
    assert resp.status_code in (303, 401)


def test_market_page_renders_for_logged_in_user(client, db):
    reset_key("login_attempts:testclient")
    make_user(db, email="market1@test.local", password="Password1234")
    make_market_day(db, date(2025, 6, 1), [200.0, 210.0])
    db.commit()

    login(client, "market1@test.local", "Password1234")
    resp = client.get("/market/prices")
    assert resp.status_code == 200
    assert "Piata energie" in resp.text


def test_market_data_endpoints_return_json(client, db):
    reset_key("login_attempts:testclient")
    make_user(db, email="market2@test.local", password="Password1234")
    make_market_day(db, date(2025, 6, 1), [200.0])
    make_market_day(db, date(2026, 6, 1), [220.0])
    db.commit()

    login(client, "market2@test.local", "Password1234")

    status = client.get("/market/data/status")
    assert status.status_code == 200
    assert status.json()["total_days_available"] == 2

    overlay = client.get("/market/data/yearly-overlay?years=2025,2026")
    assert overlay.status_code == 200
    body = overlay.json()
    assert "2025" in body and "2026" in body

    monthly = client.get("/market/data/monthly?years=2025,2026")
    assert monthly.status_code == 200

    forecast = client.get("/market/data/forecast")
    assert forecast.status_code == 200
    assert "method" in forecast.json()


def test_market_data_timeline_stays_bounded_for_large_windows(client, db):
    """Issue #33: cererea `/market/data/timeline` pentru un interval mare
    (ex. 365 zile) nu trebuie sa returneze rezolutia bruta de import (ar
    insemna zeci de mii de puncte la 15 minute) -- raspunsul ramane in
    ordinul sutelor de puncte, gratie agregarii server-side adaptive."""
    reset_key("login_attempts:testclient")
    make_user(db, email="market-timeline@test.local", password="Password1234")
    from datetime import timedelta

    from app.core.security import utcnow

    today = utcnow().date()
    for offset in range(40):
        make_market_day(db, today - timedelta(days=offset + 3), [100.0] * 96)  # 15 min/rand
    db.commit()

    login(client, "market-timeline@test.local", "Password1234")
    resp = client.get("/market/data/timeline?days=365")
    assert resp.status_code == 200
    points = resp.json()
    assert 0 < len(points) <= 366


def test_market_export_csv(client, db):
    reset_key("login_attempts:testclient")
    make_user(db, email="market3@test.local", password="Password1234")
    make_market_day(db, date(2025, 6, 1), [150.0, 250.0])
    db.commit()

    login(client, "market3@test.local", "Password1234")
    resp = client.get("/market/export.csv?years=2025")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "2025-06-01" in resp.text
    assert "200.0" in resp.text  # media (150+250)/2
