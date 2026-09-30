# Battery diagnostics: source-scoped observations and throughput (#218)

Status: accepted for the platform implementation. Companion hardware telemetry:
[EMS-device-code #1](https://github.com/bbogdan59/EMS-device-code/issues/1).

The platform owns the read model, calculations and OpenAPI contract. The agent
owns BMS/model compatibility and collection. No new register map, hardware write
or mobile implementation is introduced here. Existing telemetry v1/v2 stays
valid; optional typed battery metadata and `battery_packs` extend it additively.

A battery is identified within an authorized station by `(device_id, pack_id)`.
The legacy inverter-level reading is explicitly a bank reported by that source,
not a sum of individual packs. Different sources/packs are never added together
or silently substituted. A physical replacement must receive a new pack ID.
Station configuration may supply declared capacity only for an unambiguous
single bank, never distribute that capacity across packs.

SOH is a separately reported percentage, with measured/estimated/unavailable
kind, method/version and confidence. SOC and configured usable capacity do not
generate SOH. A legacy typed SOH with measured quality is labelled reported by
the source, not manufacturer-certified. Estimated SOH cannot activate a rule
requiring measured SOH.

For the selected source, integrate signed DC power with the existing five-minute
hold limit, in Decimal. Positive power charges; negative power discharges.
Observed EFC = (charged kWh + discharged kWh) / (2 × nominal kWh). Split at
changes in capacity and sum segment EFC; do not apply today's capacity to old
throughput. Missing/zero capacity prevents an EFC result. Missing spans remain
missing and reduce coverage; partial observed EFC is not a lifetime cycle count.
Source-reported lifetime cycles are a separate optional observation.

Reuse validated raw telemetry, retention, station configuration history and
health incidents. Return bounded hourly/daily aggregates, not raw samples.
This initial source-specific history is limited to 31 calendar days and retained
raw evidence; it does not relabel the older station-wide rollups as pack history.
Long-term hardware cycles/SOH can be displayed when reported; a lifetime EFC
cannot be reconstructed across pruned samples, unknown replacements or unknown
capacity. No database history is rewritten and no migration is needed.

The API uses existing human station authorization and publishes typed response
models for future mobile clients. Native session authentication remains #200;
device credentials do not grant access to human analytics endpoints. Deploy the
additive platform contract first, then opt-in agent support under the companion
issue, with deterministic fixtures and separate hardware verification.

## Conditional charging projection

The platform also owns the read-only projection at
`GET /api/v1/stations/{station_id}/battery-health/projection?battery_id=...`.
Its additive OpenAPI model is `BatteryChargeProjection`; no telemetry, mobile or
Home Assistant contract is changed. The dashboard summary and battery detail
both consume this endpoint. It always projects **now**, independently of the
historical day selected elsewhere on the battery page, and refreshes each minute.

The constant-rate reference uses fresh SOC, positive measured DC charging power
and declared usable capacity (nominal only when usable is absent, flagged).
Energy to the target is `capacity * max(target_SOC - SOC, 0) / 100`. Divide by
current DC power for an ETA; do not apply AC conversion efficiency a second time.
References beyond 48 hours are labelled rather than extrapolated indefinitely.
Zero charging power means no current-rate ETA; zero capacity is unusable and
must not trigger nominal fallback. The target is the station's configured
maximum normal SOC for an unambiguous bank, otherwise 100% for the selected pack.
These declared preferences do not assert that hardware has applied them.

The solar scenario integrates the latest expected PV and consumption forecast
generations from now until the next station-local midnight in Decimal, in steps
no longer than 15 minutes and split at forecast boundaries. Subtract base, EV
and flexible consumption from forecast PV. For positive surplus, apply the
configured AC charging limit and charging efficiency; for a deficit, apply the
discharging limit and efficiency. Reported current DC power is blended linearly
into this forecast over the first 15 minutes (the interval-average weight is
used). Respect the declared SOC ceiling and reserve without clamping away an
initial observed SOC outside the band. The first crossing gives a conditional
ETA; the curve can subsequently fall with evening demand.

Station forecasts cannot be allocated reliably to multiple banks/packs; only
the current-rate reference is offered for those source-specific targets. No
scheduled grid charging, optimization dispatch or unverified inverter commands
are assumed. Taper near full charge is not modelled, so actual completion can be
later. This is an estimate, not a command, charging guarantee or measured fact.

Freshness is ten minutes for the battery state and six hours for forecasts and
their linked, same-station weather evidence. Validate the latest issue before
using it; never stitch older runs into gaps. Synthetic/untrusted forecasts or
initial battery state cannot produce a solar ETA. Quality validation includes
the interval carrying into `now` and the underlying weather. Missing or
overlapping intervals stop the curve and leave the end SOC unknown; a target
crossing before the gap remains explicitly partial. If no consumption generation
exists, fresh measured household load can be held constant as a named,
low-confidence assumption. Missing load is never zero. Cold-start/low-confidence
forecasts are labelled. All contributing flags are retained.

The API preserves measurement/receipt timestamps and source on input metrics,
forecast issue timestamps and configuration/preference versions. API reads
require station viewer access and use `Cache-Control: no-store`. There are no
provider calls, writes, migrations or changes to historical energy accounting.
