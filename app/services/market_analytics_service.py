"""Analitica preturilor de piata (PZU/OPCOM): medii zilnice/lunare, suprapunere
an-peste-an si o predictie simpla, documentata, pana la finalul anului curent.

Toate functiile lucreaza pe `market_price_intervals` (is_current=True), adica
pe ultima revizie a fiecarei zile -- consistent cu ce afiseaza restul
platformei (dashboard de statie, admin).

Predictia (`get_forecast_to_year_end`) NU e un model econometric riguros --
e o metoda simpla si transparenta ("seasonal-naive" ajustat cu tendinta
recenta), documentata explicit ca atare in UI. Vezi docstring-ul functiei.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.market import ImportRun, MarketPriceInterval

SOURCE = "opcom_pzu"
TREND_RATIO_MIN = 0.5
TREND_RATIO_MAX = 2.0
RECENT_WINDOW_DAYS = 30
BUCHAREST = ZoneInfo("Europe/Bucharest")
# Peste TIMELINE_HOURLY_THRESHOLD_DAYS, `get_timeline_split` agrega pe ora in
# loc sa returneze rezolutia bruta a randurilor -- un an intreg la 15 minute
# ar insemna ~35.000 de puncte intr-un singur grafic altfel. Peste
# TIMELINE_DAILY_THRESHOLD_DAYS, agrega pe ZI -- chiar si agregarea orara ar
# produce mii de puncte pentru o fereastra de un an (issue #33).
TIMELINE_HOURLY_THRESHOLD_DAYS = 10
TIMELINE_DAILY_THRESHOLD_DAYS = 60


def _today_local() -> date:
    """Data curenta in fusul pietei (Europe/Bucharest), nu UTC.

    La ora ~21:00-23:59 UTC e deja o alta zi in Bucuresti; folosirea UTC aici
    ar arata gresit "azi"/"maine" pe pagina de piata langa acea fereastra."""
    return datetime.now(BUCHAREST).date()


def get_available_years(db: Session, source: str = SOURCE) -> list[int]:
    rows = db.execute(
        select(func.distinct(func.extract("year", MarketPriceInterval.delivery_date)))
        .where(MarketPriceInterval.source == source, MarketPriceInterval.is_current.is_(True))
        .order_by(func.extract("year", MarketPriceInterval.delivery_date))
    ).all()
    return [int(r[0]) for r in rows]


def get_daily_averages(
    db: Session,
    years: list[int] | None = None,
    source: str = SOURCE,
    include_synthetic: bool = False,
) -> list[dict]:
    """O singura interogare agregata (AVG/MIN/MAX per zi), nu N interogari.

    Implicit EXCLUDE zilele importate din fixture-uri sintetice
    (`ImportRun.is_synthetic_fixture`): sunt date de test/demo, nu preturi
    reale de piata, si nu trebuie sa intre in mediile/predictiile afisate ca
    reale. `include_synthetic=True` le include explicit (util pentru
    panoul de admin/diagnostic), iar fiecare rand marcheaza `is_synthetic`
    ca sa nu se piarda provenienta in continuare (API/CSV/UI)."""
    stmt = (
        select(
            MarketPriceInterval.delivery_date,
            func.avg(MarketPriceInterval.price_lei_per_mwh).label("avg_mwh"),
            func.min(MarketPriceInterval.price_lei_per_mwh).label("min_mwh"),
            func.max(MarketPriceInterval.price_lei_per_mwh).label("max_mwh"),
            func.count().label("n"),
            func.bool_or(ImportRun.is_synthetic_fixture).label("is_synthetic"),
        )
        .join(ImportRun, ImportRun.id == MarketPriceInterval.import_run_id)
        .where(MarketPriceInterval.source == source, MarketPriceInterval.is_current.is_(True))
        .group_by(MarketPriceInterval.delivery_date)
        .order_by(MarketPriceInterval.delivery_date)
    )
    if years:
        stmt = stmt.where(func.extract("year", MarketPriceInterval.delivery_date).in_(years))
    if not include_synthetic:
        stmt = stmt.having(func.bool_or(ImportRun.is_synthetic_fixture).is_(False))

    rows = db.execute(stmt).all()
    out = []
    for d, avg_mwh, min_mwh, max_mwh, n, is_synthetic in rows:
        out.append(
            {
                "date": d,
                "year": d.year,
                "month": d.month,
                "day": d.day,
                "day_of_year": d.timetuple().tm_yday,
                "avg_price_lei_mwh": float(avg_mwh),
                "avg_price_lei_kwh": float(avg_mwh) / 1000.0,
                "min_price_lei_mwh": float(min_mwh),
                "max_price_lei_mwh": float(max_mwh),
                "sample_count": n,
                "is_synthetic": bool(is_synthetic),
            }
        )
    return out


def get_year_over_year_overlay(db: Session, years: list[int] | None = None, source: str = SOURCE) -> dict[int, list[dict]]:
    """Grupeaza mediile zilnice pe an, cu cheie de axa X comuna 'MM-DD' pentru
    suprapunerea directa a mai multor ani in acelasi grafic (independent de an)."""
    daily = get_daily_averages(db, years=years, source=source)
    by_year: dict[int, list[dict]] = {}
    for row in daily:
        by_year.setdefault(row["year"], []).append(
            {
                "month_day": f"{row['month']:02d}-{row['day']:02d}",
                "day_of_year": row["day_of_year"],
                "avg_price_lei_mwh": row["avg_price_lei_mwh"],
                "avg_price_lei_kwh": row["avg_price_lei_kwh"],
            }
        )
    return by_year


def get_monthly_averages(db: Session, years: list[int] | None = None, source: str = SOURCE) -> dict[int, list[dict]]:
    daily = get_daily_averages(db, years=years, source=source)
    buckets: dict[tuple[int, int], list[float]] = {}
    for row in daily:
        buckets.setdefault((row["year"], row["month"]), []).append(row["avg_price_lei_mwh"])

    by_year: dict[int, list[dict]] = {}
    for (year, month), values in sorted(buckets.items()):
        by_year.setdefault(year, []).append(
            {"month": month, "avg_price_lei_mwh": sum(values) / len(values)}
        )
    return by_year


def get_timeline_split(
    db: Session, start: datetime, end: datetime, source: str = SOURCE, include_synthetic: bool = False
) -> list[dict]:
    """Serie de preturi pentru graficul principal, cu marcaj explicit
    `is_future` -- UI-ul deseneaza portiunea viitoare (nerealizata inca, ex.
    'maine') cu linie punctata, iar restul (trecut/realizat) cu linie
    continua.

    Rezolutia raspunsului se adapteaza la marimea ferestrei cerute, ca
    numarul de puncte trimise catre grafic sa ramana rezonabil indiferent cat
    de lung e intervalul (issue #33): fereastra <= `TIMELINE_HOURLY_THRESHOLD_DAYS`
    zile primeste rezolutia BRUTA a randurilor stocate (15/30/60 minute, dupa
    cum a fost publicata fiecare zi -- vezi `opcom_service.parse_csv`).
    Fereastra intre acest prag si `TIMELINE_DAILY_THRESHOLD_DAYS` zile (asta
    include fereastra implicita de 30 de zile din UI, neschimbata) e agregata
    pe ORA (medie). Peste
    `TIMELINE_DAILY_THRESHOLD_DAYS` zile (ex. un an intreg de istoric),
    agregarea trece pe ZI, ca numarul de puncte sa ramana in sute, nu mii.

    Implicit EXCLUDE intervalele provenite din fixture-uri sintetice, la fel
    ca `get_daily_averages` (vezi acolo motivul); fiecare punct ramas
    marcheaza `is_synthetic=False` explicit pentru claritate in consumatori."""
    window = end - start
    if window > timedelta(days=TIMELINE_DAILY_THRESHOLD_DAYS):
        return _get_timeline_daily(db, start, end, source, include_synthetic)
    if window > timedelta(days=TIMELINE_HOURLY_THRESHOLD_DAYS):
        return _get_timeline_hourly(db, start, end, source, include_synthetic)
    return _get_timeline_raw(db, start, end, source, include_synthetic)


def _get_timeline_raw(
    db: Session, start: datetime, end: datetime, source: str, include_synthetic: bool
) -> list[dict]:
    now = utcnow()
    stmt = (
        select(MarketPriceInterval, ImportRun.is_synthetic_fixture)
        .join(ImportRun, ImportRun.id == MarketPriceInterval.import_run_id)
        .where(
            MarketPriceInterval.source == source,
            MarketPriceInterval.is_current.is_(True),
            MarketPriceInterval.interval_start >= start,
            MarketPriceInterval.interval_start < end,
        )
        .order_by(MarketPriceInterval.interval_start)
    )
    if not include_synthetic:
        stmt = stmt.where(ImportRun.is_synthetic_fixture.is_(False))
    rows = db.execute(stmt).all()
    return [
        {
            "t": r.interval_start.isoformat(),
            "price_lei_mwh": float(r.price_lei_per_mwh),
            "price_lei_kwh": float(r.price_lei_per_kwh),
            "is_negative": r.is_negative,
            "is_future": r.interval_start > now,
            "is_synthetic": bool(is_synthetic),
        }
        for r, is_synthetic in rows
    ]


def _get_timeline_aggregated(
    db: Session, start: datetime, end: datetime, source: str, include_synthetic: bool, trunc_unit: str
) -> list[dict]:
    """Agregare pe bucket-uri de `trunc_unit` ('hour' sau 'day'), o singura
    interogare (AVG/BOOL_OR), impartita intre `_get_timeline_hourly` si
    `_get_timeline_daily` -- vezi `get_timeline_split` pentru pragurile care
    aleg intre ele."""
    now = utcnow()
    if trunc_unit == "day":
        # OPCOM delivery days follow the Europe/Bucharest market calendar.
        # Grouping a timestamptz with date_trunc("day") would depend on the
        # PostgreSQL session timezone and can split one delivery day across
        # two UTC dates. delivery_date is the canonical market-day key.
        bucket = MarketPriceInterval.delivery_date
    elif trunc_unit == "hour":
        # Hourly buckets are absolute UTC intervals, made independent of the
        # database session timezone (including DST transition days).
        bucket = func.date_trunc("hour", func.timezone("UTC", MarketPriceInterval.interval_start))
    else:
        raise ValueError(f"Unsupported timeline aggregation unit: {trunc_unit}")
    stmt = (
        select(
            bucket.label("bucket_start"),
            func.avg(MarketPriceInterval.price_lei_per_mwh).label("avg_mwh"),
            func.avg(MarketPriceInterval.price_lei_per_kwh).label("avg_kwh"),
            func.bool_or(MarketPriceInterval.is_negative).label("has_negative"),
            func.bool_or(ImportRun.is_synthetic_fixture).label("has_synthetic"),
        )
        .join(ImportRun, ImportRun.id == MarketPriceInterval.import_run_id)
        .where(
            MarketPriceInterval.source == source,
            MarketPriceInterval.is_current.is_(True),
            MarketPriceInterval.interval_start >= start,
            MarketPriceInterval.interval_start < end,
        )
        .group_by(bucket)
        .order_by(bucket)
    )
    if not include_synthetic:
        stmt = stmt.where(ImportRun.is_synthetic_fixture.is_(False))
    rows = db.execute(stmt).all()
    result = []
    for row in rows:
        if isinstance(row.bucket_start, datetime):
            bucket_start = row.bucket_start
            if bucket_start.tzinfo is None:
                bucket_start = bucket_start.replace(tzinfo=UTC)
        else:
            bucket_start = datetime.combine(row.bucket_start, time.min, tzinfo=BUCHAREST).astimezone(UTC)
        result.append(
            {
                "t": bucket_start.isoformat(),
                "price_lei_mwh": float(row.avg_mwh),
                "price_lei_kwh": float(row.avg_kwh),
                "is_negative": bool(row.has_negative),
                "is_future": bucket_start > now,
                "is_synthetic": bool(row.has_synthetic),
            }
        )
    return result


def _get_timeline_hourly(
    db: Session, start: datetime, end: datetime, source: str, include_synthetic: bool
) -> list[dict]:
    return _get_timeline_aggregated(db, start, end, source, include_synthetic, "hour")


def _get_timeline_daily(
    db: Session, start: datetime, end: datetime, source: str, include_synthetic: bool
) -> list[dict]:
    return _get_timeline_aggregated(db, start, end, source, include_synthetic, "day")


def get_forecast_to_year_end(db: Session, target_year: int | None = None, source: str = SOURCE) -> dict:
    """Predictie simpla, transparenta, pana la 31 decembrie din `target_year`
    (implicit anul curent).

    Metoda ("seasonal-naive ajustat cu tendinta recenta"):
      1. Baza sezoniera: pentru fiecare zi calendaristica ramasa (cheie
         `(luna, zi)`, NU numarul ordinal "zi-din-an" -- acesta se
         decaleaza intre un an bisect si unul obisnuit dupa 29 februarie si
         ar amesteca zile calendaristice diferite), media pretului din ANII
         ANTERIORI disponibili in acea zi calendaristica (ex. media 2024+2025
         pentru "15 noiembrie"). 29 februarie foloseste propria cheie (2, 29)
         si ramane fara baza sezoniera in anii care nu au avut-o istoric.
      2. Ajustare de tendinta: raportul dintre media ultimelor
         `RECENT_WINDOW_DAYS` zile REALE din anul curent si media bazei
         sezoniere pentru ACELEASI zile calendaristice -- surprinde daca anul
         curent ruleaza sistematic peste/sub tiparul istoric (ex. preturi
         combustibili, capacitate noua). Raportul e limitat la
         [0.5, 2.0] ca sa nu produca predictii absurde din date rare/sintetice.
      3. predictie(zi) = baza_sezoniera(zi) * raport_tendinta.

      Daca nu exista NICIUN an anterior cu date, se foloseste un fallback
      naiv: media ultimelor `RECENT_WINDOW_DAYS` zile reale, constanta pentru
      tot restul anului (metoda marcata explicit ca atare, incredere scazuta).

    NU este un model econometric riguros (nu modeleaza sezonalitate
    saptamanala, evenimente, capacitate noua etc.) -- e o aproximare simpla
    si usor de explicat, tratata ca atare in UI (linie punctata, eticheta
    explicita a metodei).
    """
    now = datetime.now(BUCHAREST)
    if target_year is None:
        target_year = now.year

    year_end = date(target_year, 12, 31)
    tomorrow = (now + timedelta(days=1)).date()
    forecast_start = max(tomorrow, date(target_year, 1, 1))
    if forecast_start > year_end:
        return {"method": "none", "note": "Anul tinta s-a incheiat deja.", "points": []}

    all_daily = get_daily_averages(db, source=source)
    current_year_daily = {row["date"]: row["avg_price_lei_mwh"] for row in all_daily if row["year"] == target_year}
    prior_years_by_month_day: dict[tuple[int, int], list[float]] = {}
    for row in all_daily:
        if row["year"] < target_year:
            prior_years_by_month_day.setdefault((row["month"], row["day"]), []).append(row["avg_price_lei_mwh"])

    have_seasonal_baseline = len(prior_years_by_month_day) >= 30  # cel putin o luna de referinta istorica

    trend_ratio = 1.0
    method = "seasonal_naive_trend_adjusted"
    if have_seasonal_baseline:
        recent_cutoff = forecast_start - timedelta(days=RECENT_WINDOW_DAYS)
        recent_actual = [v for d, v in current_year_daily.items() if recent_cutoff <= d < forecast_start]
        recent_baseline = [
            sum(prior_years_by_month_day[(d.month, d.day)]) / len(prior_years_by_month_day[(d.month, d.day)])
            for d in current_year_daily
            if recent_cutoff <= d < forecast_start and (d.month, d.day) in prior_years_by_month_day
        ]
        if recent_actual and recent_baseline and sum(recent_baseline) > 0:
            trend_ratio = (sum(recent_actual) / len(recent_actual)) / (sum(recent_baseline) / len(recent_baseline))
            trend_ratio = max(TREND_RATIO_MIN, min(TREND_RATIO_MAX, trend_ratio))
    else:
        method = "flat_recent_average"
        recent_cutoff = forecast_start - timedelta(days=RECENT_WINDOW_DAYS)
        recent_actual = [v for d, v in current_year_daily.items() if recent_cutoff <= d < forecast_start]
        flat_value = sum(recent_actual) / len(recent_actual) if recent_actual else None

    points = []
    d = forecast_start
    while d <= year_end:
        if method == "seasonal_naive_trend_adjusted":
            baseline_values = prior_years_by_month_day.get((d.month, d.day))
            predicted = (sum(baseline_values) / len(baseline_values) * trend_ratio) if baseline_values else None
        else:
            predicted = flat_value

        if predicted is not None:
            points.append({"date": d.isoformat(), "predicted_price_lei_mwh": round(predicted, 2), "predicted_price_lei_kwh": round(predicted / 1000.0, 5)})
        d += timedelta(days=1)

    return {
        "method": method,
        "trend_ratio": round(trend_ratio, 4) if method == "seasonal_naive_trend_adjusted" else None,
        "target_year": target_year,
        "forecast_start": forecast_start.isoformat(),
        "note": (
            "Predictie orientativa: medie sezoniera istorica (zi-din-an) ajustata cu tendinta ultimelor "
            f"{RECENT_WINDOW_DAYS} zile reale." if method == "seasonal_naive_trend_adjusted" else
            f"Predictie orientativa (fara istoric anterior suficient): medie constanta a ultimelor {RECENT_WINDOW_DAYS} zile reale."
        ),
        "points": points,
    }


def get_market_status(db: Session, source: str = SOURCE) -> dict:
    now_local_date = _today_local()
    tomorrow = now_local_date + timedelta(days=1)

    def _status_for(d: date) -> dict | None:
        run = db.scalar(
            select(ImportRun)
            .where(ImportRun.source == source, ImportRun.delivery_date == d)
            .order_by(ImportRun.revision.desc())
            .limit(1)
        )
        if run is None:
            return None
        return {
            "date": d.isoformat(),
            "status": run.status,
            "is_synthetic": run.is_synthetic_fixture,
            "revision": run.revision,
            "imported_at": run.fetched_at.isoformat() if run.fetched_at else None,
        }

    last_run = db.scalar(select(ImportRun).where(ImportRun.source == source).order_by(ImportRun.created_at.desc()).limit(1))
    total_days = db.scalar(
        select(func.count(func.distinct(MarketPriceInterval.delivery_date))).where(
            MarketPriceInterval.source == source, MarketPriceInterval.is_current.is_(True)
        )
    )
    earliest = db.scalar(
        select(func.min(MarketPriceInterval.delivery_date)).where(
            MarketPriceInterval.source == source, MarketPriceInterval.is_current.is_(True)
        )
    )
    latest = db.scalar(
        select(func.max(MarketPriceInterval.delivery_date)).where(
            MarketPriceInterval.source == source, MarketPriceInterval.is_current.is_(True)
        )
    )

    return {
        "today": _status_for(now_local_date),
        "tomorrow": _status_for(tomorrow),
        "last_import": {
            "delivery_date": last_run.delivery_date.isoformat(),
            "status": last_run.status,
            "is_synthetic": last_run.is_synthetic_fixture,
            "created_at": last_run.created_at.isoformat(),
        } if last_run else None,
        "total_days_available": total_days or 0,
        "earliest_date": earliest.isoformat() if earliest else None,
        "latest_date": latest.isoformat() if latest else None,
    }
