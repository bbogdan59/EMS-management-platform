from __future__ import annotations

import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.security import utcnow
from app.database import get_db
from app.models.user import User
from app.services import market_analytics_service as market
from app.web.context import build_nav_context
from app.web.templating import templates

router = APIRouter()


def _parse_years(years: str | None) -> list[int] | None:
    if not years:
        return None
    out = []
    for part in years.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or None


@router.get("/market/prices")
def market_prices_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    available_years = market.get_available_years(db)
    status = market.get_market_status(db)
    context = {
        "available_years": available_years,
        "status": status,
        **build_nav_context(db, user),
    }
    return templates.TemplateResponse(request, "market/prices.html", context)


@router.get("/market/data/status")
def market_status(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return JSONResponse(market.get_market_status(db))


@router.get("/market/data/yearly-overlay")
def market_yearly_overlay(
    years: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    overlay = market.get_year_over_year_overlay(db, years=_parse_years(years))
    return JSONResponse({str(year): points for year, points in overlay.items()})


@router.get("/market/data/monthly")
def market_monthly(
    years: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    monthly = market.get_monthly_averages(db, years=_parse_years(years))
    return JSONResponse({str(year): rows for year, rows in monthly.items()})


@router.get("/market/data/forecast")
def market_forecast(
    year: int | None = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return JSONResponse(market.get_forecast_to_year_end(db, target_year=year))


@router.get("/market/data/timeline")
def market_timeline(
    days: int = Query(default=30, le=365),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    now = utcnow()
    start = now - timedelta(days=days)
    end = now + timedelta(days=2)  # include "maine" daca e deja publicat
    return JSONResponse(market.get_timeline_split(db, start, end))


@router.get("/market/export.csv")
def market_export_csv(
    years: str | None = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    daily = market.get_daily_averages(db, years=_parse_years(years))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["data", "pret_mediu_lei_MWh", "pret_mediu_lei_kWh", "pret_min_lei_MWh", "pret_max_lei_MWh", "nr_intervale"])
    for row in daily:
        writer.writerow([
            row["date"].isoformat(), row["avg_price_lei_mwh"], row["avg_price_lei_kwh"],
            row["min_price_lei_mwh"], row["max_price_lei_mwh"], row["sample_count"],
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=preturi_opcom_pzu.csv"},
    )
