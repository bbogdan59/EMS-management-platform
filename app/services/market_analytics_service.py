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

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.market import ImportRun, MarketPriceInterval

SOURCE = "opcom_pzu"
TREND_RATIO_MIN = 0.5
TREND_RATIO_MAX = 2.0
RECENT_WINDOW_DAYS = 30


def get_available_years(db: Session, source: str = SOURCE) -> list[int]:
    rows = db.execute(
        select(func.distinct(func.extract("year", MarketPriceInterval.delivery_date)))
        .where(MarketPriceInterval.source == source, MarketPriceInterval.is_current.is_(True))
        .order_by(func.extract("year", MarketPriceInterval.delivery_date))
    ).all()
    return [int(r[0]) for r in rows]


def get_daily_averages(db: Session, years: list[int] | None = None, source: str = SOURCE) -> list[dict]:
    """O singura interogare agregata (AVG/MIN/MAX per zi), nu N interogari."""
    stmt = (
        select(
            MarketPriceInterval.delivery_date,
            func.avg(MarketPriceInterval.price_lei_per_mwh).label("avg_mwh"),
            func.min(MarketPriceInterval.price_lei_per_mwh).label("min_mwh"),
            func.max(MarketPriceInterval.price_lei_per_mwh).label("max_mwh"),
            func.count().label("n"),
        )
        .where(MarketPriceInterval.source == source, MarketPriceInterval.is_current.is_(True))
        .group_by(MarketPriceInterval.delivery_date)
        .order_by(MarketPriceInterval.delivery_date)
    )
    if years:
        stmt = stmt.where(func.extract("year", MarketPriceInterval.delivery_date).in_(years))

    rows = db.execute(stmt).all()
    out = []
    for d, avg_mwh, min_mwh, max_mwh, n in rows:
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


def get_timeline_split(db: Session, start: datetime, end: datetime, source: str = SOURCE) -> list[dict]:
    """Serie pe interval de 15 minute, cu marcaj explicit `is_future` -- UI-ul
    deseneaza portiunea viitoare (nerealizata inca, ex. 'maine') cu linie
    punctata, iar restul (trecut/realizat) cu linie continua."""
    now = utcnow()
    rows = db.scalars(
        select(MarketPriceInterval)
        .where(
            MarketPriceInterval.source == source,
            MarketPriceInterval.is_current.is_(True),
            MarketPriceInterval.interval_start >= start,
            MarketPriceInterval.interval_start < end,
        )
        .order_by(MarketPriceInterval.interval_start)
    ).all()
    return [
        {
            "t": r.interval_start.isoformat(),
            "price_lei_mwh": float(r.price_lei_per_mwh),
            "price_lei_kwh": float(r.price_lei_per_kwh),
            "is_negative": r.is_negative,
            "is_future": r.interval_start > now,
        }
        for r in rows
    ]


def get_forecast_to_year_end(db: Session, target_year: int | None = None, source: str = SOURCE) -> dict:
    """Predictie simpla, transparenta, pana la 31 decembrie din `target_year`
    (implicit anul curent).

    Metoda ("seasonal-naive ajustat cu tendinta recenta"):
      1. Baza sezoniera: pentru fiecare zi-din-an ramasa, media pretului din
         ANII ANTERIORI disponibili in acea zi-din-an (ex. media 2024+2025
         pentru "15 noiembrie").
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
    now = utcnow()
    if target_year is None:
        target_year = now.year

    year_end = date(target_year, 12, 31)
    tomorrow = (now + timedelta(days=1)).date()
    forecast_start = max(tomorrow, date(target_year, 1, 1))
    if forecast_start > year_end:
        return {"method": "none", "note": "Anul tinta s-a incheiat deja.", "points": []}

    all_daily = get_daily_averages(db, source=source)
    current_year_daily = {row["date"]: row["avg_price_lei_mwh"] for row in all_daily if row["year"] == target_year}
    prior_years_by_doy: dict[int, list[float]] = {}
    for row in all_daily:
        if row["year"] < target_year:
            prior_years_by_doy.setdefault(row["day_of_year"], []).append(row["avg_price_lei_mwh"])

    have_seasonal_baseline = len(prior_years_by_doy) >= 30  # cel putin o luna de referinta istorica

    trend_ratio = 1.0
    method = "seasonal_naive_trend_adjusted"
    if have_seasonal_baseline:
        recent_cutoff = forecast_start - timedelta(days=RECENT_WINDOW_DAYS)
        recent_actual = [v for d, v in current_year_daily.items() if recent_cutoff <= d < forecast_start]
        recent_baseline = [
            sum(prior_years_by_doy[d.timetuple().tm_yday]) / len(prior_years_by_doy[d.timetuple().tm_yday])
            for d in current_year_daily
            if recent_cutoff <= d < forecast_start and d.timetuple().tm_yday in prior_years_by_doy
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
            doy = d.timetuple().tm_yday
            baseline_values = prior_years_by_doy.get(doy)
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
    now_local_date = utcnow().date()
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
