from __future__ import annotations

import subprocess
from pathlib import Path


def test_dashboard_flow_model_signs_deadband_null_and_stale():
    dashboard_js = Path("app/web/static/js/dashboard.js").read_text()
    script = f"""
const assert = require("assert");
const vm = require("vm");
const context = {{
  window: {{}},
  console,
  Set,
}};
vm.createContext(context);
vm.runInContext({dashboard_js!r}, context);
const build = context.window.emsBuildFlowState;

let state = build({{
  data_quality: "measured",
  pv_power_kw: 2,
  load_power_kw: 1.4,
  battery_power_kw: -0.8,
  grid_power_kw: 0.7,
  ev_power_kw: null,
}});
assert.strictEqual(state.values.pv_bus.state, "active");
assert.strictEqual(state.values.bus_load.state, "active");
assert.strictEqual(state.values.battery_bus.state, "active");
assert.strictEqual(state.values.bus_battery.state, "idle");
assert.strictEqual(state.values.grid_bus.state, "active");
assert.strictEqual(state.values.bus_grid.state, "idle");
assert.strictEqual(state.values.bus_ev.state, "missing");

state = build({{
  data_quality: "measured",
  pv_power_kw: 0.03,
  load_power_kw: 0,
  battery_power_kw: 0.8,
  grid_power_kw: -1.2,
  ev_power_kw: 0.04,
}});
assert.strictEqual(state.values.pv_bus.state, "idle");
assert.strictEqual(state.values.bus_battery.state, "active");
assert.strictEqual(state.values.battery_bus.state, "idle");
assert.strictEqual(state.values.bus_grid.state, "active");
assert.strictEqual(state.values.grid_bus.state, "idle");
assert.strictEqual(state.values.bus_ev.state, "idle");

state = build({{
  data_quality: "stale",
  pv_power_kw: 1,
  load_power_kw: 1,
  battery_power_kw: null,
  grid_power_kw: null,
  ev_power_kw: null,
}});
assert.strictEqual(state.values.pv_bus.state, "stale");
assert.strictEqual(state.values.bus_load.state, "stale");
assert.strictEqual(state.values.battery_bus.label, "-");
"""
    subprocess.run(["node", "-e", script], check=True)


def test_flow_diagram_template_is_semantic_bus_layout():
    html = Path("app/web/templates/dashboard/_flow_diagram.html").read_text()

    assert 'id="energy-flow-diagram"' in html
    assert 'role="img"' in html
    assert 'aria-labelledby="energy-flow-title energy-flow-summary"' in html
    assert 'id="energy-flow-summary"' in html
    assert 'aria-live="polite"' in html
    assert "Bus" in html
    assert ">Consum</text>" in html
    assert ">casa</text>" in html
    assert '<text x="281" y="119" text-anchor="middle" class="fill-gray-800 dark:fill-gray-100">Bus</text>' in html
