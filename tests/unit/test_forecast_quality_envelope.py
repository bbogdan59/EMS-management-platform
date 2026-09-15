from __future__ import annotations

from pathlib import Path


def test_forecast_vs_actual_exposes_quality_and_source_envelope():
    service = Path("app/services/dashboard_service.py").read_text()

    for field in [
        '"forecast_confidence": f.confidence',
        '"forecast_source": f.source',
        '"forecast_source_version": f.source_version',
        '"forecast_issued_at": f.issued_at.isoformat()',
    ]:
        assert field in service
