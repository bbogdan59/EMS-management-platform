# Control, EV sessions and recommendations

The Control, EV and Recommendations pages belong to a station. Viewers can read;
operators can approve plans, change modes, manage departure requirements and act
on recommendations; organization administrators configure policies, EVSE inventory
and privacy. Cookie-authenticated writes require CSRF, and changes are audited.
Device observations require the credential of the explicitly bound device.

## Closed-loop execution

Plans are published in Shadow. An Assisted approval freezes the complete input
snapshot, configuration/preference/policy versions, intervals, targets and expiry.
The station row lock serializes approvals, mode changes, dispatch and device reports;
unique database constraints retain retry identity across processes. Editing a plan
or replacing its configuration invalidates dispatch. Approval alone sends nothing.

Execution requires `CLOSED_LOOP_EXECUTION_ENABLED=true`, an unexpired policy,
a recent device heartbeat and measured SOC/power, complete trustworthy forecast
inputs, and an exact hardware profile in `CLOSED_LOOP_VERIFIED_PROFILES`. There
are **no shipped hardware profiles**. The current EMS-device-code v0.1 reports
`inverter_write=false` and cannot execute this extension. No DEYE registers are
introduced. A future qualified profile has this deployment-owned shape:

```json
{
  "qualification-reference": {
    "hardware_verified": true,
    "model": "exact-tested-model",
    "agent_versions": ["exact-tested-agent"],
    "inverter_firmware_versions": ["exact-tested-inverter-firmware"]
  }
}
```

This is a schema example, not a compatible product. Enabling a profile is a
separate hardware acceptance decision. The agent must independently enforce all
electrical limits, lease expiry and command identity before a write. Platform
validation runs before authorization, dispatch, delivery and acceptance, but is
not an electrical interlock. No web handler performs a physical write.

Policies contain SOC bands, charge/discharge and ramp limits, managed energy,
daily/monthly discharge EFC budgets, local operating windows and expiry (at most
30 days). Zero is a valid limit. Technical and preference limits remain binding.
EFC history requires measured aggregate coverage; the current partial interval
uses a conservative physical upper bound. Missing history blocks execution.
Day/month boundaries use the station timezone, including DST. EV setpoints block
execution until a separately verified EV executor exists.

Automatic also requires `CLOSED_LOOP_AUTOMATIC_ENABLED=true`. The worker may
approve a policy-compliant plan; Assisted always requires an explicit preview
confirmation. Saving a new policy suspends control. Suspended/emergency stop
withdraws pending commands and revokes existing approval revisions. It blocks
future execution; it **does not claim the inverter physically stopped**.

Stages are separate: created, delivered, ACK accepted, reported applied, read-back
verified, outcome verified. The device result records application; a separate
read-back binds measured settings to command version, idempotency key and time.
Contradictory retries fail. Failures, rejection, expiry or a missing read-back
after two minutes suspend execution. Reconnect never renews an old command.
The worker compares time-weighted measured battery/grid power with the plan after
an interval ends. Each metric carries coverage and carry-in provenance. Outcome
verification requires at least 90% coverage, no stale/simulated contributor,
matching read-back and deviation within 0.5 kW. Incomplete evidence may arrive
for 24 hours, after which an immutable partial result is retained.

## EV data and accounting

EVSE inventory contains explicitly declared capabilities, optional device binding
and one initial connector. The data model supports multiple connectors and EVSEs.
Vehicle identity is optional; a vehicle alias, capacity and configured consumption
can be shared within its organization after consent. No VIN is requested. An EVSE
capability declaration never authorizes OCPP, inverter writes or V2G.

Observations use a source event ID and UTC-aware timestamp. Identical retries
return the existing record; conflicting reuse fails. Out-of-order observations
are retained with `applied=false` and cannot rewind state or session accounting.
Connected/charging/paused starts a session; completed/disconnected ends it;
faulted preserves an open session for recovery. Unknown SOC remains NULL.

Energy comes from monotonic meter deltas with the same meter epoch. A reset
introduces an unknown segment. When counters are missing, an available power
sample can estimate at most five minutes. Complete session totals require every
segment; known partial energy and coverage remain available separately. Stale
or simulated contributions taint the session and never generate financial claims.

Cost is a **grid-equivalent estimate**: EV energy multiplied by the historical
effective import tariff, split at tariff/market boundaries. Meter deltas are
allocated uniformly in time. Missing prices produce partial priced energy and
an unknown complete cost. Fixed fees, wear and PV offsets are excluded. PV,
battery and grid source shares are separate low-confidence estimates using
proportional AC-bus supplies, with their own coverage. They are not physical
tracing. Cost/100 km requires configured vehicle consumption. The immediate-charge
baseline uses the same energy at declared maximum power and historical tariffs;
its timing difference is not attributed to optimization without verified EV
execution. Financial calculations use Decimal.

One-off departures override recurring schedules for that local day. Weekly
schedules use station-local weekdays/time; spring-forward missing times are
skipped and repeated autumn times default to the second occurrence. Vacation
suppresses upcoming requirements. EV health rules cover offline, fault,
impossible targets, unplugged/interrupted charging and estimated cost limits;
missing evidence yields unknown. Historical schedules are not reconstructed
from current settings. A past completed session never satisfies a future target.

Revoking EVSE consent detaches vehicle references and erases stored vehicle SOC
and SOC/range targets for its connectors. Organization vehicle aliases remain
independent inventory and may be used by another consenting EVSE. Session retention
is configurable from 7 to 3650 days; the retention worker deletes expired completed
sessions and their observations, plus old unassociated events. Active sessions
remain open. CSV exports exclude vehicle aliases/SOC and require a bounded,
timezone-aware date range (up to 366 days and 1000 sessions).

## Recommendations

New optimization plans create deduplicated cards with reason, estimated impact,
coverage, confidence and expiry. Replaced plan cards become superseded. Apply
uses the same Assisted approval contract and immutable preview as Control.
Dismiss, Snooze and Not relevant store audited feedback; recent negative feedback
lowers ranking without changing historical savings or telemetry.

Version 1 presets are Economy, Autonomy, Battery protection, Backup, Vacation
and Manual. Preview shows each changed preference and retained limits. Apply
creates a new preference version and invalidates pending dispatch. The next plan
needs a new approval; Manual suspends control for 24 hours. Impact is unknown
until recalculation. Backup/Vacation do not reduce an existing larger reserve;
Battery protection does not relax an existing lower cycle budget.

The scenario comparison runs the existing HiGHS solver on the same forecast
snapshot for current preferences, proposed reserve/strategy, and a hypothetical
fixed tariff. It rejects superseded or incomplete snapshots and writes no plan
or preference. Its forecast cost is not historical savings.

## Migration and verification

Migration `b05f7d23a6c1` adds canonical control/EV entities. Legacy EV settings
are copied into declared inventory and inactive imported requirements, preserving
NULL and explicit zero, without inventing vehicles, observations or live support.
Legacy live stations are suspended pending policy review. Downgrade retains the
new tables as `legacy_*`; re-upgrade restores them. It does not re-enable legacy
live control. Back up the database before an operational rollback; do not sum
legacy and current histories.

PostgreSQL regressions cover approvals, concurrent retries/dispatch, expiry,
read-back, quality, money, DST, consent and retained migration values. Browser
checks exercise onboarding, weekly departures, feedback and the read-only gate.
These are software checks; no real charger, inverter, V2H or V2G was verified.
See [ADR-0192](ADR-0192-BIDIRECTIONAL-READINESS.md) for the separate research decision.
