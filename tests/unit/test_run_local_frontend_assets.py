from __future__ import annotations

from pathlib import Path


def test_run_local_always_rebuilds_frontend_assets_when_installing():
    script = Path("run_local.sh").read_text()

    assert 'log "Compilez asset-urile frontend (Tailwind CSS + vendorizare htmx/echarts) ..."' in script
    assert "npm run build" in script
    assert '[ ! -f "$ROOT_DIR/app/web/static/css/app.css" ]' not in script
    assert "Asset-urile frontend exista deja" not in script


def test_committed_css_contains_responsive_tariff_layout_classes():
    css = Path("app/web/static/css/app.css").read_text()

    assert "@media (min-width:768px)" in css
    assert r".md\:grid-cols-\[minmax\(0\2c 1fr\)_minmax\(280px\2c 360px\)\]" in css
    assert ".input-invalid" in css
