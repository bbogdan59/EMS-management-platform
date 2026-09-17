"""issue #117: `arbitrage_min_benefit_lei` era acceptat/validat/afisat, dar
`optimization_service.py` nu il citea niciodata -- un operator care il seta
credea gresit ca gateaza deciziile de arbitraj ale optimizatorului. Rezolvat
non-distructiv (fara migrare de coloana): UI-ul marcheaza explicit campul ca
informativ/neaplicat, iar ambiguitatea de unitate (numele campului sugereaza
lei totali, dar valoarea e de fapt lei/kWh) e clarificata prin comentarii pe
model/schema, nu printr-o redenumire de coloana."""
from __future__ import annotations

from pathlib import Path

TEMPLATE = Path("app/web/templates/stations/preferences.html").read_text()


def test_arbitrage_field_label_marks_itself_as_informational():
    assert "Prag beneficiu economic arbitraj (informativ)" in TEMPLATE
    assert '"Prag beneficiu economic arbitraj"' not in TEMPLATE  # vechea eticheta, fara disclaimer


def test_arbitrage_field_help_text_explains_it_is_not_enforced_yet():
    assert "NU o aplica inca drept constrangere" in TEMPLATE


def test_arbitrage_field_keeps_lei_per_kwh_unit():
    assert 'unit="lei/kWh"' in TEMPLATE


def test_preference_model_documents_the_unit_is_per_kwh_not_total():
    model_src = Path("app/models/preference.py").read_text()
    assert "lei/kWh" in model_src
    assert "issue #117" in model_src


def test_preference_schema_documents_the_unit_is_per_kwh_not_total():
    schema_src = Path("app/schemas/station_forms.py").read_text()
    assert "issue #117" in schema_src
