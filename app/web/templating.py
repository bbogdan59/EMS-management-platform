from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.core.csrf import csrf_token_for_template

settings = get_settings()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_token"] = csrf_token_for_template
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["demo_mode_enabled"] = settings.demo_mode_enabled


def fmt_kw(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.2f} kW".replace(",", " ")


def fmt_kwh(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.2f} kWh".replace(",", " ")


def fmt_pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}%"


def fmt_lei(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.2f} lei".replace(",", " ")


templates.env.filters["kw"] = fmt_kw
templates.env.filters["kwh"] = fmt_kwh
templates.env.filters["pct"] = fmt_pct
templates.env.filters["lei"] = fmt_lei
