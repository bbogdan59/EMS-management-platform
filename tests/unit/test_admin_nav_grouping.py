"""Cerere client: meniul de admin avea 12 intrari plate, fara nicio
structura -- regrupate in 3 categorii explicite (ems-device, administrare
platforma: audit/schedulers/catalog, organizatii+utilizatori)."""
from __future__ import annotations

from pathlib import Path


def test_admin_nav_has_three_explicit_categories():
    tabs = Path("app/web/templates/admin/_tabs.html").read_text()

    assert "Organizatii &amp; utilizatori" in tabs
    assert "Flota EMS-device" in tabs
    assert "Administrare platforma" in tabs
    assert tabs.index("Organizatii &amp; utilizatori") < tabs.index("Flota EMS-device")
    assert tabs.index("Flota EMS-device") < tabs.index("Administrare platforma")


def test_admin_nav_platform_administration_group_covers_audit_schedulers_catalog():
    """Grupare ceruta explicit de client: 'ce tine de platform
    administration(audit, schedulers, catalog)' in aceeasi categorie."""
    tabs = Path("app/web/templates/admin/_tabs.html").read_text()
    group_start = tabs.index("Administrare platforma")
    group = tabs[group_start:]

    assert 'href="/admin/audit"' in group
    assert 'href="/admin/task-audit"' in group
    assert 'href="/admin/catalog"' in group
    assert 'href="/admin/operations"' in group


def test_admin_nav_ems_device_group_covers_devices_and_firmware():
    tabs = Path("app/web/templates/admin/_tabs.html").read_text()
    group_start = tabs.index("Flota EMS-device")
    group_end = tabs.index("Administrare platforma")
    group = tabs[group_start:group_end]

    for href in (
        "/admin/devices", "/admin/devices/pending", "/admin/devices/assigned",
        "/admin/firmware/releases", "/admin/firmware/rollouts",
    ):
        assert f'href="{href}"' in group


def test_admin_nav_all_original_routes_still_present():
    """Pur reorganizare vizuala -- niciun link existent nu trebuie sa
    dispara, doar sa fie grupat."""
    tabs = Path("app/web/templates/admin/_tabs.html").read_text()
    original_routes = [
        "/admin", "/admin/organizations", "/admin/users", "/admin/operations",
        "/admin/task-audit", "/admin/catalog", "/admin/devices", "/admin/devices/pending",
        "/admin/devices/assigned", "/admin/firmware/releases", "/admin/firmware/rollouts", "/admin/audit",
    ]
    for href in original_routes:
        assert f'href="{href}"' in tabs
