from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.sun_map_service import sun_map


def test_sun_moves_east_to_west_and_is_below_horizon_at_night():
    morning, noon, evening, night = [
        sun_map(44.43, 26.1, "Europe/Bucharest", datetime(2026, 6, 21, hour, tzinfo=UTC))
        for hour in (5, 10, 16, 22)
    ]
    assert 50 < morning["current"]["azimuth"] < 100
    assert 150 < noon["current"]["azimuth"] < 190
    assert 260 < evening["current"]["azimuth"] < 310
    assert noon["current"]["elevation"] > 65
    assert morning["current"]["is_daylight"] is True
    assert evening["current"]["is_daylight"] is True
    assert night["current"]["is_daylight"] is False
    assert night["current"]["elevation"] < 0
    assert noon["source"] == "calculated"


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25), ("2024-02-29", 24)])
def test_path_uses_station_calendar_with_dst_and_leap_day(day, hours):
    now = datetime.fromisoformat(day + "T12:00:00+00:00")
    data = sun_map(44.43, 26.1, "Europe/Bucharest", now)
    start, end = [datetime.fromisoformat(data[key]) for key in ("day_start", "day_end")]
    assert end - start == timedelta(hours=hours)
    assert data["local_date"] == day
    assert all(start <= datetime.fromisoformat(point["at"]) < end for point in data["path"])
    assert len(data["path"]) >= hours * 12
    assert start.astimezone(ZoneInfo(data["timezone"])).hour == 0
    assert end.astimezone(ZoneInfo(data["timezone"])).hour == 0


@pytest.mark.parametrize("latitude,longitude,timezone", [
    (-36.85, 174.76, "Pacific/Auckland"),
    (21.31, -157.85, "Pacific/Honolulu"),
    (1.87, -157.43, "Pacific/Kiritimati"),
])
def test_sunrise_and_sunset_belong_to_local_day_across_date_line(latitude, longitude, timezone):
    data = sun_map(latitude, longitude, timezone, datetime(2026, 6, 21, 11, tzinfo=UTC))
    for name in ("sunrise", "sunset"):
        event = datetime.fromisoformat(data[name]["at"])
        local = event.astimezone(ZoneInfo(timezone))
        assert local.date().isoformat() == data["local_date"]
        assert (5 <= local.hour <= 9) if name == "sunrise" else (16 <= local.hour <= 20)


@pytest.mark.parametrize("month,daylight", [(6, True), (12, False)])
def test_polar_day_and_night_have_no_invented_sunrise_or_sunset(month, daylight):
    data = sun_map(69.65, 18.96, "Europe/Oslo", datetime(2026, month, 21, 10, tzinfo=UTC))
    assert data["sunrise"] is None
    assert data["sunset"] is None
    assert data["current"]["is_daylight"] is daylight
    assert all(point["is_daylight"] is daylight for point in data["path"])


@pytest.mark.parametrize("latitude,longitude", [(None, 0), (0, None), (None, None)])
def test_missing_coordinates_are_not_treated_as_zero(latitude, longitude):
    data = sun_map(latitude, longitude, "UTC", datetime(2026, 3, 20, 12, tzinfo=UTC))
    assert data["status"] == "missing_location"
    assert data["current"] is None
    assert data["location"] is None
    assert data["path"] == []


def test_zero_coordinates_are_valid_and_coordinate_changes_update_path():
    now = datetime(2026, 3, 20, 12, tzinfo=UTC)
    equator = sun_map(0, 0, "UTC", now)
    other = sun_map(44.43, 26.1, "UTC", now)
    assert equator["status"] == "ok"
    assert equator["current"]["elevation"] > 85
    assert equator["path"] != other["path"]
    assert equator["current"] != other["current"]


@pytest.mark.parametrize("latitude,longitude", [(float("nan"), 0), (0, float("inf")), (91, 0), (0, -181)])
def test_invalid_coordinates_are_explicit(latitude, longitude):
    assert sun_map(latitude, longitude, "UTC", datetime.now(UTC))["status"] == "invalid_location"


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        sun_map(0, 0, "UTC", datetime(2026, 1, 1))
