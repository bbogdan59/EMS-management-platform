"""Contract nou al fluxului SSE (issue #50): `diff_metrics` (coalescing intre
tururile de polling) si `_authorized_live_metrics` (aceeasi reautorizare
per-tick ca `_authorized_summary`, dar returnand contractul per-metrica).
Vezi `tests/unit/test_sse_authorization.py` pentru testele originale ale
`_authorized_summary`, ramase neschimbate."""
from __future__ import annotations

from datetime import timedelta

from app.core.security import utcnow
from app.models.user import Session as UserSession
from app.services import membership_service
from app.web.routes import sse
from tests.factories import make_membership, make_org, make_station, make_user


def _session_for(db, user):
    s = UserSession(
        user_id=user.id, token_hash="sse-live-token", csrf_secret="sse-live-csrf",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(s)
    db.flush()
    return s


# --- diff_metrics (coalescing) ------------------------------------------


def _metric(name, value, quality="measured"):
    return {"metric": name, "value": value, "unit": "kW", "measured_at": None, "received_at": None, "quality": quality, "source": "telemetry"}


def test_diff_metrics_returns_everything_on_empty_previous():
    current = [_metric("pv_power_kw", 1.0), _metric("load_power_kw", 0.5)]
    assert sse.diff_metrics({}, current) == current


def test_diff_metrics_skips_unchanged_values():
    previous = {"pv_power_kw": _metric("pv_power_kw", 1.0)}
    current = [_metric("pv_power_kw", 1.0), _metric("load_power_kw", 0.5)]
    changed = sse.diff_metrics(previous, current)
    assert [m["metric"] for m in changed] == ["load_power_kw"]


def test_diff_metrics_detects_value_change():
    previous = {"pv_power_kw": _metric("pv_power_kw", 1.0)}
    current = [_metric("pv_power_kw", 1.5)]
    changed = sse.diff_metrics(previous, current)
    assert changed == current


def test_diff_metrics_detects_quality_change_even_with_same_value():
    """O metrica ramasa la aceeasi valoare dar devenita `stale` trebuie
    retransmisa -- calitatea conteaza, nu doar valoarea numerica."""
    previous = {"pv_power_kw": _metric("pv_power_kw", 1.0, quality="measured")}
    current = [_metric("pv_power_kw", 1.0, quality="stale")]
    changed = sse.diff_metrics(previous, current)
    assert changed == current


def test_diff_metrics_coalesces_multiple_intermediate_changes_into_latest_only():
    """Simuleaza mai multe schimbari fizice intre doua tururi de polling --
    doar ultima valoare cunoscuta conteaza pentru urmatorul delta, niciodata
    un istoric al valorilor intermediare."""
    previous = {"pv_power_kw": _metric("pv_power_kw", 1.0)}
    latest_known = [_metric("pv_power_kw", 3.0)]  # 1.0 -> 2.0 -> 3.0 intre tick-uri, dar DB are doar ultima
    changed = sse.diff_metrics(previous, latest_known)
    assert len(changed) == 1
    assert changed[0]["value"] == 3.0


# --- _authorized_live_metrics (reautorizare per-tick) --------------------


def test_live_metrics_available_for_active_member(db):
    org = make_org(db, "SSE Live Active Org")
    user = make_user(db, email="sse-live-active@test.local")
    make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="SSE Live Station")
    db.commit()
    sess = _session_for(db, user)
    db.commit()

    metrics = sse._authorized_live_metrics(db, station.id, user.id, sess.id)
    assert metrics is not None
    assert any(m["metric"] == "pv_power_kw" for m in metrics)


def test_live_metrics_blocked_after_membership_deactivated(db):
    org = make_org(db, "SSE Live Deactivate Org")
    admin = make_user(db, email="sse-live-admin@test.local", is_platform_admin=True)
    user = make_user(db, email="sse-live-target@test.local")
    membership = make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="SSE Live Deactivate Station")
    db.commit()
    sess = _session_for(db, user)
    db.commit()

    assert sse._authorized_live_metrics(db, station.id, user.id, sess.id) is not None

    membership_service.deactivate_member(db, org, membership, admin)
    db.commit()

    assert sse._authorized_live_metrics(db, station.id, user.id, sess.id) is None


def test_live_metrics_blocked_for_removed_membership(db):
    org = make_org(db, "SSE Live Remove Org")
    admin = make_user(db, email="sse-live-admin2@test.local", is_platform_admin=True)
    user = make_user(db, email="sse-live-target2@test.local")
    membership = make_membership(db, user, org, role="viewer")
    station = make_station(db, org, user, name="SSE Live Remove Station")
    db.commit()
    sess = _session_for(db, user)
    db.commit()

    membership_service.remove_member(db, org, membership, admin)
    db.commit()

    assert sse._authorized_live_metrics(db, station.id, user.id, sess.id) is None
