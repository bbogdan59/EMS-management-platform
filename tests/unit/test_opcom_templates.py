from __future__ import annotations

from pathlib import Path


def test_opcom_unchanged_import_status_is_rendered_as_ok():
    market_template = Path("app/web/templates/market/prices.html").read_text()
    operations_template = Path("app/web/templates/admin/operations.html").read_text()

    assert 'status.today.status in ("succeeded", "unchanged")' in market_template
    assert 'status.tomorrow.status in ("succeeded", "unchanged")' in market_template
    assert 'r.status == "unchanged"' in operations_template
    assert "neschimbat" in operations_template
