# ADR 0004: HACS pairing over outbound HTTPS

Status: implemented for #216's HACS and backend scope.

The MQTT proposal in #189 supports an explicit package and operator-managed broker.
The HACS/mobile onboarding path needs one-time pairing without exposing HA or
asking users to manage broker credentials. We add an independent outbound HTTPS
bridge; the existing MQTT proposal and telemetry contracts are unchanged.

Alternatives: cloud-direct HA requires routability and broad HA tokens; MQTT remains
useful for managed deployments but does not itself issue tenant-bound pairing codes;
a dedicated HACS component provides the entity picker and validation inside HA.

We choose HACS → verified HTTPS → EMS. EMS issues opaque one-time station-scoped
codes; HA exchanges one for a narrowly scoped revocable credential. A PostgreSQL
row lock protects code redemption and all subsequent bridge mutations. Only hashes
are stored server-side. The client confirms the station and reviews entity sharing
before ingestion. There is no cloud-to-HA service-call channel.

The tradeoff is a separately versioned HTTPS context contract and 30-second batch
latency. We keep the implementation independent of the existing MQTT integration, with
its own table and retention task. Both transports remain read-only contextual data
and cannot replace the canonical inverter meter or bypass capability/policy checks.

Consent is granular: selected entity sharing, aggregate occupancy, and future
contextual insights. Numeric and boolean null semantics are preserved. A mapping
generation fences stale clients; reconfiguration erases old values. Expiring tokens
require re-pairing every 90 days. Removal while offline requires server-side revoke
or a retry to confirm remote deletion. Local disconnect persists across restart.

See [implementation and deployment](../HOME_ASSISTANT_BRIDGE.md) for the endpoint,
retention and validation contract. Mobile screens and mobile authentication remain
separate dependencies and #216 is not closed by these backend/HA changes alone.
