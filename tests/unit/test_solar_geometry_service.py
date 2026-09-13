from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from app.services.solar_geometry_service import compute_sun_window, sun_window_for_local_date

BUCHAREST = ZoneInfo("Europe/Bucharest")
LAT, LON = 44.43, 26.10  # Bucuresti, aceleasi coordonate ca statia demo din tests/factories.py


def _seconds_since_midnight(moment_utc):
    return moment_utc.hour * 3600 + moment_utc.minute * 60 + moment_utc.second


def test_sun_window_dst_spring_forward_shifts_local_time_but_not_utc_instant():
    """Romania trece la ora de vara (UTC+2 -> UTC+3) in noaptea de sambata
    spre duminica, 28->29 martie 2026. Rasaritul FIZIC (UTC) se schimba doar
    cu cateva minute intre doua zile consecutive (miscarea normala a
    calendarului solar) -- dar reprezentarea lui in ora LOCALA sare cu
    aproape o ora intreaga, pentru ca zoneinfo aplica regula DST reala, nu un
    offset fix presupus."""
    before = compute_sun_window(LAT, LON, date(2026, 3, 28))
    after = compute_sun_window(LAT, LON, date(2026, 3, 29))

    utc_time_of_day_shift_minutes = abs(_seconds_since_midnight(before.sunrise_utc) - _seconds_since_midnight(after.sunrise_utc)) / 60
    assert utc_time_of_day_shift_minutes < 5  # continuitate fizica: ora UTC a rasaritului nu sare

    local_before = before.sunrise_local(BUCHAREST)
    local_after = after.sunrise_local(BUCHAREST)
    assert local_before.utcoffset().total_seconds() / 3600 == 2  # EET
    assert local_after.utcoffset().total_seconds() / 3600 == 3  # EEST
    local_hour_shift = (_seconds_since_midnight(local_after) - _seconds_since_midnight(local_before)) / 60
    assert 50 < local_hour_shift < 70  # ~o ora, din trecerea DST, nu din miscarea solara


def test_sun_window_dst_fall_back_shifts_local_time_but_not_utc_instant():
    """Simetric, la revenirea la ora de iarna (ultima duminica din octombrie
    2026: 24->25 octombrie)."""
    before = compute_sun_window(LAT, LON, date(2026, 10, 24))
    after = compute_sun_window(LAT, LON, date(2026, 10, 25))

    utc_time_of_day_shift_minutes = abs(_seconds_since_midnight(before.sunrise_utc) - _seconds_since_midnight(after.sunrise_utc)) / 60
    assert utc_time_of_day_shift_minutes < 5

    local_before = before.sunrise_local(BUCHAREST)
    local_after = after.sunrise_local(BUCHAREST)
    assert local_before.utcoffset().total_seconds() / 3600 == 3  # EEST
    assert local_after.utcoffset().total_seconds() / 3600 == 2  # EET
    local_hour_shift = (_seconds_since_midnight(local_after) - _seconds_since_midnight(local_before)) / 60
    assert -70 < local_hour_shift < -50


def test_sun_window_daylight_hours_longer_in_summer_than_winter():
    summer = compute_sun_window(LAT, LON, date(2026, 6, 21))
    winter = compute_sun_window(LAT, LON, date(2026, 12, 21))
    assert summer.daylight_hours > 14
    assert winter.daylight_hours < 10
    assert summer.daylight_hours > winter.daylight_hours


def test_sun_window_is_daylight_true_at_noon_false_at_midnight():
    from datetime import UTC, datetime

    window = compute_sun_window(LAT, LON, date(2026, 6, 21))
    noon_utc = datetime(2026, 6, 21, 10, 0, tzinfo=UTC)  # ~13:00 local vara, clar ziua
    midnight_utc = datetime(2026, 6, 21, 1, 0, tzinfo=UTC)  # ~4:00 local vara, clar noapte
    assert window.is_daylight(noon_utc) is True
    assert window.is_daylight(midnight_utc) is False


def test_sun_window_for_local_date_matches_compute_sun_window_for_this_timezone():
    """Pentru Europe/Bucharest (offset UTC intreg, fara traversare de zi la
    pranz), pornirea de la o zi LOCALA trebuie sa dea acelasi rezultat ca
    pornirea directa de la ziua UTC corespunzatoare."""
    direct = compute_sun_window(LAT, LON, date(2026, 6, 21))
    via_local = sun_window_for_local_date(LAT, LON, BUCHAREST, date(2026, 6, 21))
    assert direct.sunrise_utc == via_local.sunrise_utc
    assert direct.sunset_utc == via_local.sunset_utc
