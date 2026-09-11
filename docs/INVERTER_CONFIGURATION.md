# Inverter configuration contract (issue #17)

This is the web half of the protocol. EMS-device-code v0.1 remains read-only and does not yet call these new endpoints. Do not enable physical execution until the device implements its executor and the exact DEYE model/firmware has been validated. No production register map is bundled or invented here.

## Records and ownership

- The existing `/api/v1/config` is unchanged and remains the station's energy policy.
- `InverterProfile` is an immutable, platform-admin-approved definition: exact DEYE model, supported firmware strings, document source/revision, word order, register descriptors and numeric bounds. Its SHA-256 hashes canonical JSON; delivery verifies stored content integrity. The hash detects corruption, not manufacturer authenticity; profile approval is a privileged engineering action based on the actual manual and hardware validation.
- `InverterDesired` is an immutable per-device version: approved profile, connection, desired numeric settings, reason, author, hash and optional command.
- `InverterReport` is an immutable per-device snapshot version, including model, firmware, observation time, outcome and content hash. Deltas are materialized into complete snapshots. Missing/null means unknown.

No bootstrap server URL, credential, tenant ID or arbitrary serial path can be passed through connection configuration. Ports use `/dev/serial/by-id/`, address is 1..247, and rate/serial parameters are bounded. Settings are allowlisted against the approved profile. Desired values must fit writable register limits, scale and integer encoding. Observed values may be outside desired limits: they are evidence, not commands.

## API

User mutations use the existing session plus CSRF header/form token. Device mutations use the existing device bearer authentication; request-supplied station IDs cannot override device ownership.

| Endpoint | Authorization | Result |
|---|---|---|
| POST `/admin/inverter-profiles` | platform admin + CSRF | approve immutable profile; returns profile_id/hash |
| GET `/stations/{station_id}/devices/{device_id}/configuration` | station viewer | reconciliation UI, observed values, author/reason/history |
| POST same URL | station organization admin + CSRF | versioned desired JSON request |
| POST same URL + `/form` | same | HTML form adapter |
| GET `/api/v1/inverter/config` | device bearer | separate desired/reported sections and profile definition/hash |
| POST `/api/v1/inverter/reported` | device bearer | idempotent snapshot/delta; returns report_id/version/hash |

See generated OpenAPI for exact schema types and bounds. Numeric values in persisted JSON are canonical decimal strings; hashing sorts keys and uses compact UTF-8 JSON.

### Bootstrap and first snapshot

1. Admin approves the documented profile via the profile endpoint. No profile means no supported configuration; the page explains this.
2. Organization admin selects it and publishes the connection with empty `settings`, `expected_version=0`, a fresh UUID `request_id`, and a reason.
3. Device fetches the configuration, verifies hash/model compatibility and connects read-only. It reports `kind=snapshot`, `base_version=0`, `desired_version=1`, `profile_hash`, exact `model`/`firmware`, timezone-aware `measured_at`, and known numeric `settings`.
4. Each next report supplies the latest `base_version`. Deltas require an initial snapshot on the same profile. Repeated request IDs with identical payload return the original receipt; changed payloads return 409. Stale versions/timestamps and mismatched firmware/model/hash return 409.
5. Admin can now publish desired settings, based on the latest version and a snapshot less than five minutes old. Unknown settings are not sent as zero.

### Execution and reconciliation

Saving never acknowledges application. A command is created only for a non-demo live station and an active, recently online device that explicitly reports **both** `inverter_write=true` and `inverter_settings_v1=true`. Otherwise the desired configuration is available but pending; a later explicit publish can create a command after capabilities are available.

Command type `apply_inverter_settings` uses the existing pending/ACK/result routes. Its parameters contain desired version/hash, profile hash, expected report version and allowlisted settings; validity is 30..900 seconds (default 300). The device must recheck all versions, expiry, physical limits and its local allowlist immediately before writing. Any newer desired version supersedes queued/accepted configuration commands. Delivery and ACK recheck capabilities, station mode and the expected snapshot; a changed snapshot requires republishing instead of applying against stale assumptions.

To report `executed`, first POST a readback snapshot and then reference its returned `report_id` in the existing command result `details`. The server verifies device/profile/version, exact desired values, observed outcome and observation time within the command's accepted/expiry window. A bare `{applied:true}` is rejected for this command type. Device-side electrical protections are still required and are not replaced by server validation.

The page shows pending, applied (matching fresh device observations), drift, rejected/failed and stale states; command expiry and rejection are also visible. An ACK or cached desired configuration is never evidence of application.

## Concurrency and migration

Mutations serialize on the device row with PostgreSQL `FOR UPDATE`, then compare expected versions; database uniqueness additionally protects per-device versions and request IDs. Tests include two concurrent desired edits with exactly one winner. Profile duplicate approval has a unique hash constraint; concurrent administrative approvals may require retry after a uniqueness conflict.

Alembic `d17a2c9f0311` follows the merged enrollment/telemetry head `c91a17e4b208`, adds three tables, and leaves existing device/API records unchanged. Downgrading this feature drops those three new tables and their configuration history; export history before a deliberate rollback. Existing command rows remain as audit history and must not be executed by older code; disable/revoke configuration-capable devices before rollback.

## Validation scope

Tests cover PostgreSQL-backed snapshots/deltas/retries, stale versions, model/firmware/hash rejection, bounds, read-only behavior, readback execution and expiry, concurrent edits, web RBAC/CSRF and device HTTP authentication. Fixtures use deliberately artificial `TEST ONLY` maps. Hardware, firmware upgrade compatibility and physical write safety must be validated in EMS-device-code issues #1/#2; this PR does not claim hardware support.
