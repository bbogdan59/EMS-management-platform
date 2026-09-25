from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.core.csrf import csrf_token_for_template

settings = get_settings()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_token"] = csrf_token_for_template
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["demo_mode_enabled"] = settings.demo_mode_enabled


def fmt_number(value, digits: int = 2) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return "-"
    if not number.is_finite():
        return "-"
    return f"{number:,.{digits}f}".translate(str.maketrans({",": ".", ".": ","}))


def fmt_percent(value) -> str:
    if value is None:
        return "-"
    return f"{fmt_number(Decimal(str(value)) * 100, 0)}%"


def fmt_lei(value) -> str:
    return "-" if value is None else f"{fmt_number(value)} lei"


def fmt_lei_per_kwh(value) -> str:
    """Pretul pe kWh e stocat cu 5 zecimale (`Numeric(10, 5)`, vezi
    `app/models/tariff.py`) -- rotunjirea la 2 zecimale a `fmt_lei` ar
    ascunde marje mici reale (ex. 0.00085 lei/kWh ar aparea ca 0.00)."""
    if value is None:
        return "-"
    return f"{fmt_number(value, 5)} lei/kWh"


def fmt_local_dt(value, tz_name: str | None) -> str:
    """Converteste un datetime (sau un string ISO 8601, cum sunt stocate
    timestamp-urile in `OptimizationRun.input_snapshot`, un camp JSON) in ora
    LOCALA a statiei (`tz_name`) -- planul e calculat pe grila UTC, dar un
    operator citeste orele in fusul local al statiei, nu in UTC."""
    if value is None:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    tz_label = tz_name or "UTC"
    tz = ZoneInfo(tz_label)
    return f"{value.astimezone(tz).strftime('%d.%m.%Y %H:%M')} {tz_label}"


templates.env.filters["lei"] = fmt_lei
templates.env.filters["lei_per_kwh"] = fmt_lei_per_kwh
templates.env.filters["local_dt"] = fmt_local_dt
templates.env.filters["number"] = fmt_number
templates.env.filters["percent"] = fmt_percent
