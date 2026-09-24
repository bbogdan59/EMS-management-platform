# Health, notifications and diagnostics

This delivery covers #18, #180, #186, #188 and #190. The application navigation
contains **Stare si alerte**, **Starea statiilor** and **Notificari**. All device
control remains in the existing control services. Diagnostic grants do not confer
station configuration, command, assistant or alert mutation permission.

## Telemetry contract (#18)

`GET /api/v1/telemetry/contract` retains `schema_version=1` for existing agents and
advertises supported versions 1 and 2. Both accept the typed MPPT, phase, battery,
inverter diagnostics, status and counter groups. A phase is identified by
`(circuit, phase)`; circuit is `grid` or `load` (default grid). The battery supports
optional SOH %. Raw inverter status numbers are stored without interpreting their
meaning. No DEYE register map or hardware compatibility is inferred.

The actual companion agent's `Agent.sample()` payload uses flat fields from
`ems_device.readers.FIELDS`. The compatibility normalizer maps PV1/PV2 electrical
readings, grid/load phase power and voltage, battery V/A/temperature, inverter
AC/DC temperature, raw status code and cumulative kWh counters into the typed
contract. Mixing a flat and nested representation of the same group is rejected
rather than silently choosing a value. A sanitized fixture in
`test_existing_agent_extended_payload_is_typed_without_inventing_faults` exercises
that shape. This is platform-side contract verification; it is not a claim of an
agent/hardware deployment test.

Both `quality_flags.simulated` (the real agent flag) and `raw_payload.simulated`
(the older simulator flag) taint canonical readings. Stale and derived flags
propagate through dashboard/export, carry-in aggregation and forecast quality.
Live optimization requires measured, fresh SOC and excludes synthetic/untrusted
forecasts. Shadow forecasts preserve their untrusted source marker. Unknown
values and explicit zero retain their distinct meanings.

Validated extensions are stored in `telemetry_raw.diagnostics`, with a legacy
copy in `raw_payload.extended`. Arbitrary raw JSON is never promoted into that
validated column, including during migration. Previous raw diagnostics remain
available as history, but require a new typed sample before health rules trust
them. Fault messages and arbitrary reset identifiers are omitted from installer
exports.

Each counter uses the sample's `measured_at` and its own quality. Counter
comparison returns a derived Decimal delta only for one device, increasing
measurement times, unchanged reset epoch and suitable quality. A decrease is
unknown unless the same explicit `rollover_kwh` modulus is supplied at both
endpoints. Changed reset IDs establish a new baseline. Counter deltas are never
added to the power-integrated energy aggregates (that would double count).

ACK results preserve request order and `(boot_id, sequence)`. Unsupported schema,
duplicate metric identifiers, out-of-storage-range powers and inconsistent
counter modulus are permanent per-item failures. Non-finite/structurally invalid
JSON is a schema-level 422. Future timestamps remain retryable. Legacy aggregate
ACK counts are unchanged.

Late data older than the routine two-hour aggregation window queues affected UTC
hours plus adjacent hours in the same database transaction as ingestion. A worker
rebuilds the existing 15m/hour/local-day/local-month aggregates under a station
lock. The queue is durable and deduplicated across retries; retention protects its
carry-in until processing completes. Data outside the configured raw retention
window, with a three-hour guard for complete adjacent buckets, is permanently
rejected (`retention_window_expired`). This prevents overwriting retained energy
history from already-pruned raw inputs. The separate absolute protocol age limit
remains 400 days. Historical imports beyond retention require a separately
reviewed full raw-data restoration; do not sum another overlapping generation.

## Deterministic health rules (#180)

The versioned catalog is in `health_service.RULES` and visible at
`/health/runbooks`. It contains offline and stale telemetry, metric coverage,
inverter faults, weather-adjusted PV underperformance, unusual nighttime load,
battery temperature/SOC/SOH, grid limits, unconfirmed/failed commands, failed
optimization, stale weather/market sources, and failed/rolled-back OTA checks.
Every rule describes required inputs, threshold/recovery boundary, window,
severity, category, confidence, reason and suggested next steps. Optional sensor
rules report unknown until typed, fresh measured inputs exist.

`alerts_task` evaluates active stations every five minutes. A station row lock
and unique `(station, rule, subject, version, window)` evaluation key make
concurrent jobs/retries idempotent. Lifecycle events are append-only:
`detected -> active -> acknowledged -> resolving -> resolved`, with `suppressed`,
`expired`, and `false_positive` terminal alternatives. Acknowledgement is not
resolution. Recovery requires two consecutive healthy five-minute windows and
numeric hysteresis. Resolved incidents cool down 30 minutes; suppression and
false-positive feedback cool down 24 hours for that station/rule/subject only.
Legacy offline incidents are adopted without discarding their history.

`evaluate_station(..., historical=True)` records completed historical windows
without changing live incidents or creating notifications. Stored raw samples,
forecast timestamps, versioned configuration and event history provide evidence;
historical heartbeats are explicitly unknown because a device only retains its
latest heartbeat. It does not reconstruct facts that were never retained.

## Notification policy and outbox (#186)

In-app notifications are canonical. They are uniquely keyed by recipient and
incident event, regardless of external delivery success. Users configure a
category × severity × channel matrix per organization. Email and browser push
are off by default. Immediate delivery, daily digests, a separate Monday weekly
report, local quiet hours and delayed critical re-alerts are supported. Critical
immediate notifications bypass quiet hours; warning/digest traffic does not.
Scheduling walks UTC instants to handle DST gaps/folds and leap days.

Email requires `NOTIFICATIONS_EMAIL_ENABLED=true`, `EMAIL_BACKEND=smtp` and
`SMTP_HOST`; startup validates the combination. A short-lived code verifies the
current account email before incident delivery. Verification mail is also queued,
not sent inside the request handler. Codes are hashed in preferences and encrypted
in the short-lived delivery payload, then erased after delivery. Changing account
email invalidates its prior verification.

Push requires `NOTIFICATIONS_PUSH_ENABLED=true` and all three
`NOTIFICATIONS_VAPID_*` values. Subscription enrollment uses browser consent and
CSRF, and stores the subscription encrypted with the existing secret-encryption
mechanism. Only HTTPS endpoints at FCM, Mozilla Push and Apple Web Push are
accepted. Redirects are disabled. The adapter uses
[pywebpush's documented VAPID interface](https://github.com/web-push-libs/pywebpush).
Rotation of `SECRET_KEY` requires resubscribing, as for existing encrypted
integrations. Private VAPID keys never enter the database or browser.

`notifications_task` materializes records, routes digests and sends up to 50
queued deliveries per run, using separate transactions and `FOR UPDATE SKIP
LOCKED`. Delivery has pending/delivered/failed/suppressed states, bounded
exponential retries (five attempts), destination and current membership checks,
and sanitized failure codes. Subscription URLs, provider exception text, raw
logs and user questions are never logged. Delivery links lead to authenticated
application pages and carry no credentials.

Unsubscribe disables external channels, not in-app incidents. Revoked memberships
and archived organizations cannot receive queued incident data. A late retry
rechecks quiet hours and consent. The database queue is idempotent; SMTP itself
cannot promise exactly-once delivery if a worker crashes after the remote server
accepts mail but before committing. Browser push uses a stable Topic for retry
replacement. No external email or push was sent during implementation/tests.

## Fleet and installer access (#188)

The fleet page filters data freshness, severity, firmware, fault, data quality
and update state. Health score = healthy known components / all known components;
unknown inputs are shown separately, and no score is displayed without evidence.
Thresholds and component verdicts remain inspectable.

Organization admins can grant a named existing user read-only diagnosis of one
station for up to 30 days, or revoke it. The grant is audited, checked for each
read/export and cannot bypass the existing station RBAC on any other endpoint.
Normal active organization memberships retain their existing role privileges.

A diagnostic export takes timezone-aware `start`/`end` (maximum 31 days). It
contains version identifiers, telemetry values with NULL/provenance, incident
reason codes, config/command/firmware timeline and sanitized log categories.
Names, addresses, coordinates, device serials, arbitrary messages, secrets and
signed URLs are excluded. Each timeline source is capped at 1,000 entries and
reports truncation. Fixed inputs/database state produce deterministic JSON.
Acknowledgement delay is explicitly labelled a proxy for time to understand;
resolution time and feedback precision include sample counts, not invented
operational performance claims.

## Grounded read-only assistant prototype (#190)

`ENERGY_ASSISTANT_ENABLED=false` by default. This first prototype is deterministic:
there is no external model, model key, token charge or arbitrary tool executor.
Natural-language intent selects a bounded read-only service. The supported
intents cover import, consumption, PV, battery charging, historical cost,
recorded alerts and published plans. Preference-change questions return an
expiring recommendation draft with `applied=false`; they never enqueue commands.
EV feasibility and counterfactual financial effects are not invented.

Every request resolves the authenticated user's station membership before reads,
uses one explicit local calendar day (default yesterday), and returns metric,
formula, period, source row IDs and coverage. Energy sums use Decimal. Existing
financial calculations are reused; historical tariff gaps, synthetic inputs,
incomplete days and coverage below 90% refuse a numerical conclusion. It does
not infer a physical cause from a correlation or claim ACK means execution.
Provider messages/log text never enter intent processing. Responses use safe DOM
text, not HTML interpretation. Audit stores intent, authorized scope and outcome,
not raw question text. Limits are 20 requests/user/hour, five-second SQL statement
timeout and ten-second browser timeout, with a deterministic unavailable response.
The UI labels generated answers and their limits.

## Operations, migration and verification

Run Alembic migrations before starting updated workers. The two additive
migrations add validated telemetry storage, incident/evaluation/grant tables,
notification preferences/outbox and the backfill queue. They do not rewrite
canonical energy or monetary columns. Downgrade preserves new feature tables
under `legacy_*` names and copies validated diagnostics into legacy raw payloads;
re-upgrade restores the feature tables. Do not delete these preserved tables as
part of rollback. Historical diagnostics are not implicitly trusted after
re-upgrade; ingest a new validated observation.

Local coverage includes PostgreSQL migrations with populated mixed NULL/known
telemetry, concurrent evaluation/outbox workers, replay/recovery/cooldown,
per-item ACKs and actual-agent-shaped fixtures, DST/leap days, redaction,
revoked access, numerical assistant answers and adversarial questions. Browser
coverage exercises fleet navigation, acknowledgement, assistant refusal with
missing data, download, preferences and mobile overflow. CI separately runs the
full unit/integration suite and Docker build/migration/HTTP smoke test. Physical
Pi/DEYE, real SMTP and real browser push delivery are separate operator checks;
no hardware validation or electrical compatibility is claimed here.
