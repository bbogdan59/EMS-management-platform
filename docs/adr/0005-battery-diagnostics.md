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
