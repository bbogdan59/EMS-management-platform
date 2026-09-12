"""Contract explicit de unitati pentru preturile de energie (issue #51).

1 MWh = 1000 kWh, intotdeauna -- dar directia conversiei conteaza: a
inmulti/imparti orbeste cu 1000 fara sa stii daca valoarea porneste din
lei/MWh sau lei/kWh produce un pret gresit de 1.000.000x. Foloseste
`KWH_PER_MWH`/functiile de mai jos peste tot unde se converteste intre cele
doua unitati, in loc de un `*1000`/`/1000` scris ad-hoc la locul apelului --
un singur loc de adevar pentru factorul de conversie."""
from __future__ import annotations

from decimal import Decimal

KWH_PER_MWH = Decimal(1000)


def mwh_to_kwh(price_lei_per_mwh: Decimal) -> Decimal:
    """lei/MWh -> lei/kWh (imparte la 1000)."""
    return price_lei_per_mwh / KWH_PER_MWH


def kwh_to_mwh(price_lei_per_kwh: Decimal) -> Decimal:
    """lei/kWh -> lei/MWh (inmulteste cu 1000)."""
    return price_lei_per_kwh * KWH_PER_MWH
