# HACS outbound bridge

Implements the Home Assistant/backend part of #216 independently from MQTT #189.
Companion integration: https://github.com/bbogdan59/EMS-home-assistant.

Apply Alembic migration `a216b40c912e`, enable `HOME_ASSISTANT_BRIDGE_ENABLED=true`
on web/worker/beat, and serve EMS over HTTPS. No HA URL, long-lived access token,
public HA listener, or MQTT broker is needed. Default is disabled; core EMS works
without it. The new table holds ephemeral context separately from Deye telemetry.

Station dashboard → Conecteaza Home Assistant opens the native pairing/status UI.
Organization administrators can generate a 10-minute, one-time 96-bit opaque code.
A new code revokes the previous station bridge and deletes its context. HA redeems
the code outbound, confirms the instance/station, reviews a local allowlist and
consent, then sends a test batch. Unconfirmed bearer tokens expire in 10 minutes;
confirmed tokens expire in 90 days and are renewed by re-pairing. Codes and tokens
are hashed at rest, never echoed by status/diagnostics. HA stores its own token.

API contract and HA install/development instructions:
[canonical HTTPS bridge v1](../contracts/home_assistant/README.md).

| Endpoint under `/api/v1` | Auth / effect |
| --- | --- |
| POST `/stations/{id}/home-assistant/pairing` | Station admin + CSRF; create code, revoke previous connection |
| GET `/stations/{id}/home-assistant` | Station viewer; no-store context/freshness |
| DELETE `/stations/{id}/home-assistant` | Station admin + CSRF; revoke and erase |
| POST `/home-assistant/pairing/redeem` | Rate-limited code exchange; no session cookie |
| PUT `/home-assistant/bridge/mappings` | Station-bound bearer, expected version and explicit consent |
| POST `/home-assistant/bridge/samples` | Bearer, mapping version, at most 20 samples/32 KiB |
| DELETE `/home-assistant/bridge` | Bearer; revoke self and erase |

Cookie endpoints use existing StationAccess/RBAC and CSRF. Mobile authentication
must integrate these operations with #199's authenticated API; this change does
not add a second mobile authentication mechanism or implement mobile screens.
Connect/disconnect responses and status use no-store. An ingress proxy must preserve
trusted client addresses for the pairing rate limit and must not log authorization
headers or request bodies. Deploy with TLS termination; the HA client requires
verified HTTPS and refuses redirects.

PostgreSQL locks serialize code redemption, reconfiguration, ingestion and revoke.
Concurrent redemptions have one winner. A bridge token cannot choose a station or
tenant; ownership, organization state, issuing user's activity/admin role and expiry
are checked on each use. A restored HA backup or reinstall cannot reuse an old
code; re-pairing revokes its prior credential.

Entities: power, energy, indoor temperature, a single separately consented aggregate
occupancy sensor, boiler/HVAC/EVSE status and declared flexible power. Units,
domains, device classes, state classes, bounds, quality and timestamps are validated.
Only allowlisted samples are accepted; batches reject atomically. Numeric values
use Decimal and are normalized to kW/kWh/°C. Unknown/unavailable stays null; zero
and false remain values. Context keeps home_assistant provenance and never replaces
inverter measurements or creates hardware commands.

Timestamps/sample IDs are source-owned and stable across retries. Duplicate and
out-of-order data is ignored; timestamps >30 seconds in the future are rejected.
Stale values are hidden at read time. The hourly retention task removes values
older than 24 hours, including when the feature is disabled; watermarks remain for
replay protection. A disconnect deletes context, mapping, code and bearer hashes
immediately. No time-series or raw HA attributes are retained.

Health: connecting, connected, stale (heartbeat >90 seconds or a missing/stale
source), offline (>300 seconds), reauth_required, disconnected. The station overview
shows a compact Casa inteligenta card only while a bridge is active. Contextual
insights/notifications require the recorded opt-in; downstream processing and mobile
overview remain separate #206/#202 deliverables.

Validation: `pytest tests/integration/test_home_assistant_bridge.py -q` uses
PostgreSQL (including real replay races), RBAC/CSRF, expiry, reinstall, versioning,
atomic rejection, null/zero, retention and migration coverage. The HACS repository
also provides a separate real-HA/real-HTTPS smoke script. Hardware is not certified.
