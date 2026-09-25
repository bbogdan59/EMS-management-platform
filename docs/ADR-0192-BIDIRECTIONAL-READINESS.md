# ADR 0192: demand response and bidirectional charging readiness

Status: accepted research direction; **no-go for live V2H/V2G or flexibility dispatch**.
Reviewed: 2026-09-25. Scope: #192. No equipment was purchased, certified or tested
on a live electrical installation for this report.

## Decision and boundaries

Keep managed unidirectional charging, V2H and V2G as distinct capabilities.
Managed charging changes EV consumption without exporting vehicle energy. V2H
supplies a home's loads; backup operation additionally needs a certified isolation
and transfer system. V2G exports to the public network and adds connection,
metering, commercial and settlement obligations. Demand response can change
consumption without any reverse power flow.

The platform's canonical EV model stores declared capabilities and observations.
It does not implement OCPP or bidirectional commands. A declaration such as
`v2g=true` is inventory metadata, never permission to dispatch. The current
companion EMS agent explicitly reports `inverter_write=false`; its audited
Modbus profiles are for reading. See the companion repository's
`README.md`, `docs/PROTOCOL.md` and `docs/VALIDATION_SG04LP3.md`.

No EV/EVSE pilot model, production firmware, point-of-connection meter, islanding
device or V2G contract has been identified in this workspace. Their readiness is
**unknown**, not inferred from a connector type or a protocol version.

## Protocol and equipment matrix

| Item | Officially documented scope | What EMS can claim today | Required evidence for a pilot |
|---|---|---|---|
| OCPP 1.6J | Charging-station/CSMS protocol; Smart Charging exists. ISO 15118 integration uses a separate OCA whitepaper rather than 2.x message parity. | No adapter implemented in this delivery. | Exact implemented messages, security profile, charger firmware and contract tests. |
| OCPP 2.0.1 | Device model, transactions, security and ISO 15118 support. Core certification does not imply optional Smart Charging, Advanced Security or ISO 15118 certification. | No claim that every 2.0.1 charger supports those profiles or bidirectionality. | Certificate identifier, PICS and optional-profile coverage for the exact product. |
| OCPP 2.1 | Adds ISO 15118-20 bidirectional transfer, bidirectional charging and DER-control functionality. | A protocol candidate for a future adapter, not installed hardware support. | Message subset, device model variables, version negotiation and tested fallback. |
| ISO 15118-20:2022 | EV-to-EVSE communication messages and sequences for bidirectional power transfer. | A communication standard, not evidence of electrical certification or a compatible vehicle. | EV and EVSE conformance, negotiated transfer mode, PKI interoperability and electrical installation approval. |
| Current EMS Pi / DEYE read-only profile | Existing repository documents read-only observations. | Telemetry only; battery/EV physical execution is disabled by default. | Separately audited executor, exact model and inverter/agent firmware, local interlocks and measured read-back. |
| Unspecified pilot EV + EVSE + site meter | No official model/firmware evidence supplied. | Unknown for SOC, start/stop, limits, scheduling, V2H and V2G. | Full bill of materials, firmware versions, protocol certificates, phase limits, signed meter convention and installation diagram. |
| Volkswagen ID. manufacturer example | Volkswagen's 2023 release describes 77-kWh ID. vehicles with software 3.5+, CCS DC, and first-version compatibility with HagerEnergy S10 E COMPACT, with a 20% mobility reserve. | Historical manufacturer example of a constrained V2H system; **no EMS/DEYE compatibility claim**. | Current regional availability, exact vehicle eligibility, warranty and complete supported equipment combination. |
| Renault manufacturer example | Renault's French product page requires a compatible bidirectional vehicle, its bidirectional PowerBox and the Mobilize power electricity contract. | A region/contract-specific V2G example; no inference of open third-party control or Romanian eligibility. | Written operator access, product/firmware eligibility and applicable regional commercial agreement. |

Protocol facts: [OCA FAQ](https://openchargealliance.org/faq/),
[OCA certification profiles](https://openchargealliance.org/certificationocpp/certification-ocpp-2-0-1/),
[OCA 2.1 release](https://openchargealliance.org/ocpp-2-1-is-now-available/),
[ISO 15118-20 abstract](https://www.iso.org/standard/77845.html).
The current [OCA download catalog](https://openchargealliance.org/my-oca/ocpp/)
lists 2.1 Edition 2 and 2.0.1 Edition 4 with June 2026 errata; a future pilot must
pin the actual specification and errata, not a floating “OCPP 2” label.

Equipment evidence: [Volkswagen release, 2023-12-06](https://www.volkswagen-newsroom.com/en/press-releases/cleverly-manage-your-own-electricity-first-id-models-support-bidirectional-charging-17949),
[Renault French V2G eligibility](https://www.renault.fr/solutions-de-recharge/vehicle-to-grid.html).
These sources establish manufacturer claims, not an independent hardware test.

## PKI and threat model

OCPP secures the charger/CSMS boundary; ISO 15118 and Plug & Charge concern the
vehicle/charger interaction and associated identity/certificate ecosystem. Plug &
Charge authorization does not itself authorize export, establish a settlement
contract or prove support for bidirectional power. The following are proposed EMS
pilot controls, not a claim that the cited standard mandates this exact design.

| Threat / boundary | Required control | Failure behavior and test |
|---|---|---|
| Stolen charger credentials / false tenant claim | Bind authenticated device identity to one approved tenant/site; inventory serial cannot grant access. Rotate credentials and revoke old sessions. | Cross-tenant boot/meter/command requests fail; reconnect with revoked key remains blocked. |
| Compromised or expired CA chain | Separate trust stores for CSMS TLS and vehicle contract certificates; explicit permitted roots, bounded certificate lifetimes and rotation overlap. Protect private keys in suitable hardware where available. | Test untrusted root, expiry, invalid signature and rotation rollback; do not silently accept a new root. |
| Certificate status/clock unavailable offline | Define maximum offline authorization age and trustworthy clock behavior per pilot. Record last successful validation. | Loss of trustworthy time or revocation freshness blocks new export authorization; local safety remains independent. |
| Replayed remote command | Immutable device/connector/plan/version binding, durable idempotency key, bounded validity window and persistent last-applied state. | Duplicate has no new physical effect; expired command cannot run after reboot/reconnect. |
| Compromised charger falsifies SOC/meter | Plausibility checks against independent point-of-connection meter, signed/attested measurements where supported, and energy residual monitoring. | Mark discrepancies unknown/untrusted, withhold settlement and suspend automatic actions. |
| CSMS/account takeover | RBAC, explicit opt-in, least-privilege command allowlist and per-site power/energy/SOC/cycle caps enforced again locally. | A cloud request cannot override thermal, electrical, mobility or anti-islanding limits. |
| Out-of-order messages or clock drift | Store observation and receive times separately; reject identity reuse with different payload; never rewind live state on late messages. | Reorder/reboot fixtures preserve state and accounting; invalid time is not zero usage. |
| Message flood / exhausted worker | Bounded frames, per-device queues/quotas, separate persistent-connection workers and bounded retries. | Overload cannot starve emergency local logic or regular web requests. |
| Firmware supply-chain compromise | Signed firmware manifests, exact model/firmware allowlist, staged rollout and independent rollback path. | An update clears prior compatibility approval until its required checks pass. |
| Privacy leakage | Opt-in aggregate occupancy only if needed; no VIN, individual movement history or raw certificate identifiers in ordinary exports/logs. | Tenant-scoped export, retention and revocation tests; redacted support bundle. |

Local emergency override has priority over every cloud command. Revoking opt-in
stops new dispatch immediately; stopping an already applied action requires a
verified local mechanism. A platform “suspended” badge must never imply that a
contactor opened or grid export ceased.

## Meter boundaries and settlement model

Design proposal: retain separate cumulative import and export registers at the
grid connection, separate bidirectional EVSE registers, PV production, stationary
battery charge/discharge and house consumption. Store units, AC/DC boundary,
meter identity, calibration, resolution, reset epoch, quality and UTC timestamps.
Do not infer one load from another when the residual has unknown inputs.

For a common AC boundary and interval, the reconciliation equation is:

`PV + grid_import + stationary_discharge + EV_discharge`
`= house_load + EV_charge + stationary_charge + grid_export + residual`.

Illustrative ledger: 6 + 10 + 1 + 1 = 10 + 4 + 2 + 2 kWh. These are separate
directional totals over the interval, not simultaneous power claims. An AC/DC
conversion requires measured loss data or an explicitly estimated efficiency;
unknown efficiency cannot produce a measured DC cycle count. Cumulative deltas
and integrated power are alternative evidence, never additive generations.

Maintain three separate monetary ledgers:

1. Retail energy: interval import cost and contract-qualified export revenue,
   historical tariff versions, taxes/fees and settlement calendar.
2. Flexibility: activation identifier, contracted baseline/method version,
   requested versus delivered response, valid metering coverage, availability
   payment, performance payment, fees, penalties and imbalance responsibility.
3. Asset cost: documented vehicle/stationary battery degradation allowance,
   warranty throughput and owner compensation. Unknown values remain unknown.

A hypothetical 5-kW baseline versus 3-kW import for 15 minutes gives 0.5 kWh of
load reduction. At a hypothetical 0.80 lei/kWh activation payment, gross service
revenue is 0.40 lei. That does not by itself establish eligibility or net savings.
Retail tariff savings and flexibility revenue must not both claim the same
payment. Baseline adjustments and rebound energy remain auditable. No revenue is
recognized from a command ACK or an estimated EV/PV source allocation.

## Commercial, regulatory and owner consent gates

EU demand-response aggregation rules provide context for market participation,
including imbalance responsibility; they do not grant this installation an
automatic right to export or settle a flexibility product. See the consolidated
[Directive 2019/944, Article 17](https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX%3A02019L0944-20251012).
For Romania, ANRE describes the distribution operator's connection, certification
and metering role in its [prosumator guidance](https://anre.ro/consumatori/energie-electrica/cum-devin-prosumator/),
and lists the aggregation licensing framework in its
[electricity legislation catalog](https://anre.ro/participanti-la-piata-de-energie/persoane-juridice/energie-electrica/).
Those documents are starting points; PV prosumer eligibility must not be assumed
to cover energy re-exported from an EV battery.

Before a Romanian pilot, obtain written determinations for the exact installation
from the DSO and proposed aggregator/supplier: permitted export/islanding mode,
connection/protection requirements, applicable licenses and market role, metering
acceptance, imbalance allocation, tax/billing treatment and consumer opt-out.
Record the contract and decision versions. None is represented as approved here.

Owner opt-in must specify separately: V1G/V2H/V2G, departure reserve in kWh and
SOC only when available, latest ready-by time, maximum dispatch power, daily and
lifetime throughput/cycle budgets, financial floor, consent expiry and revocation.
Require written vehicle/EVSE warranty coverage for the exact use and firmware;
do not extrapolate a manufacturer's promotional lifetime or savings figure.
Emergency override and mobility reserve win over a revenue opportunity.

## Proposed disconnected sandbox

Use an isolated software EV/EVSE/CSMS simulator with no route to production
devices, no real device credentials and no electrical output. All samples carry
`simulated` provenance. Model capacity, losses, resettable directional counters,
connection windows, certificate expiry and network faults. The existing EV and
control contracts are test boundaries; do not add negative EV power to the
current unidirectional API without a separately versioned contract.

Replay deterministic scenarios: midnight/DST/leap day, lost ACK followed by
reconnect, duplicated command, certificate rotation/revocation, low SOC,
thermally limited charger, phase overload, meter reset, contradictory read-back,
retail/flexibility overlap and sudden consent revocation. Assert energy residual,
no duplicate settlement, local override precedence, expiry and tenant isolation.
Progress next to a professionally supervised isolated lab bench, never by
connecting the simulator to the public grid.

## Go/no-go checklist and accountable evidence

| Gate | Evidence required | Current decision |
|---|---|---|
| Protocol | Exact EVSE model/firmware, OCPP version/PICS, optional profiles and fixtures | No-go: pilot unspecified |
| Electrical | Installation design, isolation/anti-islanding protections, local limits, meter calibration and qualified sign-off | No-go: no installation evidence |
| Hardware behavior | Independent read-back, expiry across reboot, thermal/phase limits and emergency override demonstrated on the selected hardware | No-go: no hardware validation |
| Identity and PKI | Trust-chain inventory, rotation/revocation rehearsal and ownership/tenant binding | No-go: no pilot PKI |
| Vehicle/warranty | Exact vehicle eligibility, SOC semantics, reserve guarantee and written bidirectional warranty terms | No-go: vehicle unspecified |
| Commercial/regulatory | Written DSO, supplier/aggregator and owner agreements for the specified service | No-go: no agreements supplied |
| Measurement/settlement | Reconciled independent meters, accepted baseline, residual threshold, immutable ledger and dispute workflow | No-go: no accepted settlement evidence |
| Release | Threat tests, simulated replay, isolated bench results and scoped feature flag reviewed together | Software research only |

Passing protocol certification alone cannot clear the electrical, contractual or
whole-system interoperability gates. #183 remains the separate charger-adapter
pilot; #184 remains the separate EV control/optimizer extension. This ADR closes
the research question with explicit prerequisites, not a live-control promise.
