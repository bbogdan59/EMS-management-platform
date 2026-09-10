"""Schema CONFIGURABILA pentru CSV-ul OPCOM PZU.

LIMITARE DOCUMENTATA (vezi docs/LIMITATIONS.md): mediul de dezvoltare/CI in
care a fost construita aceasta platforma nu a putut accesa opcom.ro (blocat
de politica de retea a organizatiei), deci schema exacta a coloanelor NU a
putut fi verificata direct impotriva unui CSV real. Valorile de mai jos sunt
cea mai buna aproximare rezonabila (denumiri de coloane si separator uzuale
pentru rapoarte CSV romanesti), configurabile prin variabile de mediu, ca un
administrator sa le poata corecta fara redeploy de cod daca formatul real
difera. Parserul VALIDEAZA structural ce gaseste (nu presupune orbeste) si
raporteaza explicit eroarea + header-ul real gasit daca maparea nu se
potriveste.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OpcomCsvSchema:
    delimiter: str = ";"
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
