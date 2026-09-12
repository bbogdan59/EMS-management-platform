"""Regresie pentru un blocaj real, raportat local: apelarea fluxului SSE
(`GET /stations/{id}/sse`) bloca NEDEFINIT orice alta cerere autentificata cu
ACELASI cookie de sesiune.

Cauza: `get_current_context` actualiza `Session.last_seen_at` doar cu
`db.flush()` (fara `commit()`). `db` provine din `get_db()`, o dependinta
FastAPI cu `yield` -- Starlette/FastAPI nu inchide/rollback-uieste o astfel
de dependinta decat DUPA ce raspunsul e trimis INTEGRAL. Pentru un raspuns in
flux lung (`EventSourceResponse`), asta insemna "niciodata cat timp clientul
ramane conectat": UPDATE-ul necomis tinea un row lock Postgres deschis pe
randul `sessions` pe toata durata conexiunii SSE. Orice ALTA cerere care
folosea acelasi cookie (deci acelasi rand `sessions`) bloca la randul ei
nedefinit incercand acelasi UPDATE in `get_current_context`.

Testat aici cu doua conexiuni Postgres REALE (nu fixture-ul `db`, care
foloseste o singura conexiune/SAVEPOINT si nu poate exercita contentie de
lock reala). Conexiunea B ruleaza intr-un thread separat cu un timeout dur
de perete, ca un regres viitor sa esueze rapid si deterministic (cateva
secunde) in loc sa agate testul la infinit."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as OrmSession

from app.api.deps import get_current_context
from app.config import get_settings
from app.core.security import hash_token, utcnow
from app.models.organization import Membership, Organization
from app.models.station import Station
from app.models.user import Session as UserSession
from app.models.user import User
from tests.factories import make_org, make_station, make_user

settings = get_settings()


def _fake_request(raw_token: str) -> SimpleNamespace:
    return SimpleNamespace(cookies={settings.session_cookie_name: raw_token}, state=SimpleNamespace())


def test_get_current_context_releases_session_row_lock_immediately(engine):
    suffix = uuid4().hex
    raw_token = f"raw-token-{suffix}"

    with OrmSession(engine) as setup:
        user = make_user(setup, email=f"{suffix}@lockregression.test")
        org = make_org(setup, f"Lock Regression Org {suffix}")
        station = make_station(setup, org, user, name=f"Lock Regression Station {suffix}")
        sess = UserSession(
            user_id=user.id, token_hash=hash_token(raw_token), csrf_secret="csrf",
            expires_at=utcnow() + timedelta(hours=1),
        )
        setup.add(sess)
        setup.commit()
        ids = {"user_id": user.id, "org_id": org.id, "station_id": station.id, "session_id": sess.id}

    try:
        # Conexiunea A: exact ce face `station_live_stream` la deschiderea
        # fluxului SSE -- rezolva `get_current_context` (actualizeaza
        # `last_seen_at`) folosind o sesiune deschisa, FARA sa o inchida
        # explicit dupa (simuland dependinta FastAPI ramasa "vie" pe toata
        # durata unui raspuns in flux lung, inainte de fix-ul de `db.close()`
        # din ruta -- ceea ce testeaza aici e strict comportamentul de
        # commit din `get_current_context` insusi).
        session_a = OrmSession(engine, autoflush=False, autocommit=False, expire_on_commit=False)
        auth_context = get_current_context(_fake_request(raw_token), db=session_a)
        assert auth_context.user.id == ids["user_id"]

        # Conexiunea B: o cerere DIFERITA, cu acelasi cookie de sesiune,
        # incearca acelasi UPDATE. Rulata intr-un thread separat cu un
        # timeout dur de perete -- daca regreseaza (UPDATE-ul din conexiunea
        # A nu a fost comis imediat), acest apel ar bloca la nivelul
        # driverului Postgres pana la inchiderea conexiunii A, nu doar sa
        # ridice o exceptie SQLAlchemy pe care am putea-o prinde direct.
        def _second_request():
            with OrmSession(engine, autoflush=False, autocommit=False, expire_on_commit=False) as session_b:
                get_current_context(_fake_request(raw_token), db=session_b)
                session_b.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_second_request)
            try:
                future.result(timeout=5)
            except FutureTimeoutError:
                pytest.fail(
                    "get_current_context a blocat peste 5s pe row lock -- UPDATE-ul din "
                    "conexiunea A nu a fost comis imediat (regresia raportata local: fluxul "
                    "SSE bloca orice alta cerere cu acelasi cookie de sesiune)."
                )

        session_a.close()
    finally:
        with OrmSession(engine) as cleanup:
            cleanup.execute(delete(UserSession).where(UserSession.id == ids["session_id"]))
            cleanup.execute(delete(Membership).where(Membership.organization_id == ids["org_id"]))
            cleanup.execute(delete(Station).where(Station.id == ids["station_id"]))
            cleanup.execute(delete(Organization).where(Organization.id == ids["org_id"]))
            cleanup.execute(delete(User).where(User.id == ids["user_id"]))
            cleanup.commit()


def test_get_current_context_commits_last_seen_at_visibly_to_other_connections(engine):
    """Nu doar ca nu blocheaza -- scrierea chiar trebuie sa fie vizibila
    imediat altor conexiuni (nu doar eliberata, ci si persistata), altfel un
    `db.close()` ulterior (rollback implicit) ar sterge-o tacit."""
    suffix = uuid4().hex
    raw_token = f"raw-token-{suffix}"

    with OrmSession(engine) as setup:
        user = make_user(setup, email=f"{suffix}@lockregression2.test")
        sess = UserSession(
            user_id=user.id, token_hash=hash_token(raw_token), csrf_secret="csrf",
            expires_at=utcnow() + timedelta(hours=1), last_seen_at=None,
        )
        setup.add(sess)
        setup.commit()
        ids = {"user_id": user.id, "session_id": sess.id}

    try:
        with OrmSession(engine, autoflush=False, autocommit=False, expire_on_commit=False) as session_a:
            get_current_context(_fake_request(raw_token), db=session_a)

        with OrmSession(engine) as verify:
            row = verify.scalar(select(UserSession).where(UserSession.id == ids["session_id"]))
            assert row.last_seen_at is not None
    finally:
        with OrmSession(engine) as cleanup:
            cleanup.execute(delete(UserSession).where(UserSession.id == ids["session_id"]))
            cleanup.execute(delete(User).where(User.id == ids["user_id"]))
            cleanup.commit()
