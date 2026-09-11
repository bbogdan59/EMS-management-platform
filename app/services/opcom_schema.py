"""Schema CONFIGURABILA pentru CSV-ul OPCOM PZU (raport PIP, export CSV).

Verificata impotriva unui export real descarcat de pe opcom.ro (rezolutie de
15 minute, "rezultatePZU_PT15M_..."): fisierul are un titlu + un tabel
sumar (medii Base/Peak/Off-Peak) inaintea tabelului detaliat pe intervale,
separate cu delimitatorul virgula, fiecare camp incadrat in ghilimele duble
(CSV standard RFC4180) -- de exemplu:

  "Zona de tranzactionare","Interval","Pret de Inchidere a Pietei [lei/MWh]",...
  "Romania","1","1233.84","1459.4","852.2","1459.4","PT15M"

Coloana de pret reala se numeste "Pret de Inchidere a Pietei [lei/MWh]", nu
doar "Pret" -- de aceea maparea din `opcom_service._find_header` cauta
aliasurile de mai jos ca SUBSIR (nu potrivire exacta) in numele coloanei,
insensibil la majuscule/diacritice normalizate. Parserul valideaza structural
ce gaseste (nu presupune orbeste) si raporteaza explicit eroarea + header-ul
real gasit daca maparea tot nu se potriveste (ex. un format viitor diferit).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OpcomCsvSchema:
    delimiter: str = ","
    encoding_candidates: tuple[str, ...] = ("utf-8-sig", "cp1250", "cp1252", "latin-1")
    interval_column: str = "Interval"
    price_column: str = "Pret"
    currency_column: str = "Moneda"
    header_search_rows: int = 15
    interval_aliases: tuple[str, ...] = field(
        default=("interval", "ora", "hour", "interval orar", "nr. interval")
    )
    price_aliases: tuple[str, ...] = field(
        default=("pret", "preț", "price", "pret[ron/mwh]", "pret ron/mwh")
    )
    currency_aliases: tuple[str, ...] = field(default=("moneda", "currency", "valuta"))


DEFAULT_SCHEMA = OpcomCsvSchema()
