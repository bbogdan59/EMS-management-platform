"""Rasarit/apus si "ore utile de soare" reale pentru o statie -- issue #53,
criteriul "rasarit/apus/ore utile de soare derivate clar".

Foloseste algoritmul SPA (Solar Position Algorithm, NREL) din pvlib, deja o
dependinta a platformei (vezi `pv_forecast_service.py`), NU un tabel de
aproximare sau un offset fix. Rasaritul/apusul unei zile sunt calculate
intotdeauna in UTC, folosind coordonatele REALE (latitudine/longitudine) ale
statiei -- momentul fizic al rasaritului nu depinde de convenția orei de
vara. Doar REPREZENTAREA lui in ora locala a statiei se schimba la trecerea
DST, iar aceasta conversie foloseste `zoneinfo` (biblioteca standard,
gestioneaza corect regulile DST reale ale fusului orar), niciodata un offset
calculat manual.

Nu introduce nicio migratie de schema: rasaritul/apusul sunt calculabile
oricand, determinist, direct din latitudine/longitudine/data -- nu au nevoie
sa fie persistate ca sa fie "versionate" (spre deosebire de prognoza meteo
propriu-zisa, care depinde de un provider extern si TREBUIE versionata cu
`issued_at`)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
from pvlib.solarposition import sun_rise_set_transit_spa


@dataclass(frozen=True)
class SunWindow:
    """Fereastra de lumina reala pentru o statie, intr-o zi data.

    `sunrise_utc`/`sunset_utc` sunt intotdeauna timezone-aware (UTC).
    `daylight_hours` e durata fizica a zilei (independenta de DST -- diferenta
    a doua momente UTC).
    """

    target_date: date
    latitude: float
    longitude: float
    sunrise_utc: datetime
    sunset_utc: datetime
    daylight_hours: float

    def sunrise_local(self, tz: ZoneInfo) -> datetime:
        return self.sunrise_utc.astimezone(tz)

    def sunset_local(self, tz: ZoneInfo) -> datetime:
        return self.sunset_utc.astimezone(tz)

    def is_daylight(self, moment_utc: datetime) -> bool:
        """True daca `moment_utc` (aware) cade in intervalul [rasarit, apus)
        al ACESTEI zile. Apelantul e responsabil sa aleaga `SunWindow`-ul
        pentru ziua locala corecta a lui `moment_utc` (vezi
        `sun_window_for_local_date`) -- verificarea aici e strict pe
        momentul UTC dat, fara sa presupuna la ce zi calendaristica apartine.
        """
        return self.sunrise_utc <= moment_utc < self.sunset_utc


def compute_sun_window(latitude: float, longitude: float, target_date: date) -> SunWindow:
    """Rasarit/apus calculat cu SPA la pranz UTC al zilei date -- suficient
    de departe de miezul noptii pentru orice longitudine rezonabila, ca sa
    prindem rasaritul/apusul corecte ale acelei zile calendaristice UTC."""
    noon_utc = pd.Timestamp(datetime(target_date.year, target_date.month, target_date.day, 12, 0, tzinfo=UTC))
    result = sun_rise_set_transit_spa(pd.DatetimeIndex([noon_utc]), latitude, longitude)
    # `.round("s")` inainte de conversie -- SPA produce nanosecunde, iar
    # `datetime` standard nu le suporta (ar arunca un avertisment de trunchiere).
    sunrise = result["sunrise"].iloc[0].round("s").to_pydatetime()
    sunset = result["sunset"].iloc[0].round("s").to_pydatetime()
    daylight_hours = round((sunset - sunrise).total_seconds() / 3600, 4)
    return SunWindow(
        target_date=target_date,
        latitude=latitude,
        longitude=longitude,
        sunrise_utc=sunrise,
        sunset_utc=sunset,
        daylight_hours=daylight_hours,
    )


def sun_window_for_local_date(latitude: float, longitude: float, tz: ZoneInfo, local_date: date) -> SunWindow:
    """Ca `compute_sun_window`, dar pornind de la o zi calendaristica LOCALA
    (a statiei) in loc de o zi UTC -- foloseste pranzul local al acelei zile,
    convertit corect in UTC prin `zoneinfo` (gestioneaza DST automat)."""
    noon_local = datetime(local_date.year, local_date.month, local_date.day, 12, 0, tzinfo=tz)
    return compute_sun_window(latitude, longitude, noon_local.astimezone(UTC).date())
