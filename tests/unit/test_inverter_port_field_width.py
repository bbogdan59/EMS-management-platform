"""Campul de port serial trebuie sa foloseasca design system-ul aplicatiei
(`class="input"`) si sa fie suficient de lat pentru un device path real
(`/dev/serial/by-id/usb-...`, ~60 caractere) -- vezi issue #121: fara
`class="input"` campul se randa la latimea implicita a browserului
(~20 caractere), fara sa poata fi verificat vizual inainte de salvare."""
from __future__ import annotations

from pathlib import Path


def test_inverter_port_field_uses_design_system_and_is_wide_enough():
    template = Path("app/web/templates/stations/inverter_config.html").read_text()

    assert 'name="port" required' in template
    assert 'class="input w-full" name="port"' in template
