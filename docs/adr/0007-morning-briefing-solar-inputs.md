# ADR 0007: Morning briefing and per-input solar diagnostics

Status: accepted for platform implementation. Umbrella issues: [#220](https://github.com/bbogdan59/EMS-management-platform/issues/220), [#221](https://github.com/bbogdan59/EMS-management-platform/issues/221).

## Ownership and release order

The platform owns forecasting facts, notification policy, normalized diagnostics,
history and web presentation. Contracts below land before consumer changes.
Physical read-only collection/model verification remains in
[EMS-device-code#1](https://github.com/bbogdan59/EMS-device-code/issues/1).
Native push tokens/delivery/deep-link routing remain coordinated with
[EMS-mobile-app#14](https://github.com/bbogdan59/EMS-mobile-app/issues/14);
forecast presentation with [EMS-mobile-app#12](https://github.com/bbogdan59/EMS-mobile-app/issues/12).
This release uses the existing email/web-push outbox; it does not claim native
push or any newly verified hardware profile. Consumers can keep sending the
existing v1 telemetry without new optional fields.

## Briefing contract and policy

Notification `source_key=morning:YYYY-MM-DD`, `category=briefing`,
`payload.kind=briefing`, `payload.version=morning-energy/v1`. Unique identity is
user/station/station-local day. Stored payload is immutable, even after forecast
refresh or retries, and includes Decimal strings for expected/display kWh,
optional p10/p90, confidence, forecast/weather IDs, model versions, issue instant,
day bounds/timezone, baseline evidence, suggested hour, optional published plan
ID/version and expiry. Title/body use these stored facts. No LLM, hourly price
advice, assumed battery-full time, or fabricated savings are used.

Only the newest expected forecast batch issued within six hours qualifies.
Every contributing PV and weather segment must have nominal/medium/high
confidence, be nonsynthetic, and have fresh source weather. Missing weather,
overlap and daytime gaps suppress generation. Missing intervals entirely before
astronomical sunrise or after sunset are permitted; without coordinates a full
day is required. Energy is the integral over the actual station day (23/24/25
hours), including observed forecast night intervals. The range is shown only if
both scenarios satisfy the same checks and bracket the expected total.

Baseline: median of at least seven complete measured PV days in the preceding
three years within 21 calendar days of the target season, wrapping across New
Year and using leap year 2000 for month/day distance. Partial and synthetic days
are excluded. Above 125% / below 75% selects a high/low headline; otherwise a
neutral headline. This historical comparison is not normalized for subsequent
changes in installed capacity. No adequate baseline means no comparative claim.

Opt-in defaults off. Preferences select station or user clock and a start/end
hour; quiet hours always use the user clock. The minute worker creates canonical
in-app notices. Enabled email/push channels coalesce for ten minutes into at most
one external digest per user/channel/UTC day, across stations/organizations.
Stations qualifying after delivery still receive their own in-app notice.
The UTC digest boundary prevents a user travelling across timezones from
receiving one push per station; the canonical station-local identity is unchanged.
Both existing matrix modes `immediate` and `daily` opt into this morning digest.

TTL is bounded by the local day end, six hours, delivery window and quiet hours.
Each attempt rechecks membership, active station, opt-in, channel, forecast age
and TTL. Existing outbox retries cap at five attempts (2/4/8/16/32 minutes);
expired messages are suppressed, not sent late. The anchor organization's
revocation suppresses its whole cross-organization delivery. PostgreSQL unique
keys and row locks serialize concurrent generators/outbox delivery. Logs/metrics
expose counts/status/failure codes only. As with any external sender, transport
success followed by a database crash cannot guarantee exactly-once email delivery.

Authenticated deep link `/stations/{station_id}/briefing/{day}` rechecks current
membership and notice ownership, displays original facts and links to the live
forecast/day plan. Expired notices remain readable and are marked expired.
Native clients should route the canonical notice identity, fetch authorized
facts, tolerate new payload keys/kinds, and implement token lifecycle in #14.

## Solar contract and storage

See [solar v1](../../contracts/solar/README.md). Snapshot/histories are scoped by
station access; tracker UUIDs and Deye serials never authorize access. Operator
configuration is CSRF-protected, audited and guarded by a revision plus row lock.
Configuration describes topology, not hardware settings or acknowledged writes.

Inverter identity includes station, device, source and source key; trackers use
source-reported indices. Optional physical strings are user-declared identifiers,
labels and module counts under tracker configuration. No electrical readings
are attributed to individual physical strings without source measurements.

Samples are deduplicated by tracker and measured UTC instant, retaining first
accepted values. Late samples do not roll back latest capability metadata.
All V/A/W remain nullable Decimal. Power is never inferred from V×A. Explicit
zero is measured zero. AC output is an independent, signed source observation.
An omitted previously observed input is marked stale. Ten-minute old/future
samples are also marked stale. DC sum requires power for every known input at
the current inverter observation; provenance remains explicit.

Separate 15-minute/hourly diagnostic aggregates use time-weighted means and
integrated power with a five-minute maximum hold. A NULL sample interrupts the
hold. Coverage is per metric and flags include carry-in contributions. Missing
history buckets remain NULL so charts do not bridge gaps. These tables never
contribute to station production/billing and are never summed across sources.
They use the existing raw/aggregate retention settings, pending-backfill guard
and aggregation worker. History selects 15m for 24h, 1h for 7/30d; bounded to
97/169/721 slots, loaded independently per chart.

Warnings require an explicitly named comparison group, known positive kWp,
equal configured azimuth/tilt, fresh measured power and at least 100 W/kWp peak.
The spread of normalized W/kWp must exceed the most conservative configured
threshold in the group. No inferred orientation or diagnosis of a physical fault.

## Migration and rollback

Additive migration `b320c221d901` creates isolated diagnostic tables and opt-in
fields. Merge revision `b321c221d902` joins it with the independently released
mobile authentication migration without changing either branch's data.
Existing station energy, raw telemetry and notifications are retained.
New ingestion populates diagnostics from deployment onward; older raw diagnostics
can be replayed with `python -m scripts.backfill_solar_inputs --station UUID --days 7`.
Replay is bounded by raw retention and upserts the same diagnostic generation.
It cannot reconstruct unavailable provider history.

Downgrade renames solar tables to `legacy_solar_*`; upgrade restores their exact
NULL/zero/value history. Preferences added by this release reset to opt-out on
rollback/re-upgrade; canonical notification facts/read state are retained.
Do not manually delete legacy tables while a rollback recovery is needed.
