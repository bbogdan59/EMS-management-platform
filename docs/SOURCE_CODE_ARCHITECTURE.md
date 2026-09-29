# EMS source-code architecture and repository map

This document is the canonical map for the EMS codebase. Every human or AI contributor must read it before making cross-repository changes.

## Repository ownership

| Repository | Owns | Runtime / distribution |
|---|---|---|
| `bbogdan59/EMS-management-platform` | FastAPI API, web UI, workers, persistence, optimization, forecasts, billing, insights, notifications and canonical integration data | Railway |
| `bbogdan59/EMS-device-code` | Raspberry Pi edge agent, identity/enrollment, RS485/Modbus, DEYE profiles, local safety, telemetry outbox and OTA | Raspberry Pi / Linux |
| `bbogdan59/EMS-mobile-app` | React Native/Expo iOS and Android application | App Store / Google Play |
| `bbogdan59/EMS-home-assistant` | Home Assistant custom integration, pairing, entity allowlist/mapping and outbound synchronization | HACS/manual; potentially Home Assistant Core later |
| `EMS-contracts` — future only | Language-neutral OpenAPI/JSON Schema/fixtures if extraction becomes justified | GitHub Releases/Packages |

Do not create `EMS-contracts` yet. Contracts remain in the management platform until at least mobile and Home Assistant consume them stably.

## System boundaries

### EMS management platform

The platform is the only source of business logic for:

- tenant/RBAC and memberships;
- canonical station/device/integration models;
- financial calculations and bill projections;
- OPCOM/tariffs;
- PV/weather forecasts and calibration;
- energy aggregation;
- recommendations, alerts and notification policy;
- optimization and command policy;
- Deye Cloud and Home Assistant normalized data;
- versioned public/mobile/device contracts.

Mobile, edge and Home Assistant clients must not duplicate these calculations.

Suggested ownership:

```text
app/
├── api/
│   ├── v1/
│   └── mobile/
├── integrations/
│   ├── deye_cloud/
│   └── home_assistant/
├── services/
│   ├── forecast/
│   ├── billing/
│   ├── insights/
│   └── notifications/
└── workers/

contracts/
├── openapi/
├── telemetry/
├── events/
└── fixtures/

docs/
└── adr/
```

### EMS device code

The edge agent owns:

- device identity, enrollment and assignment;
- serial/installation credentials and local state;
- RS485/Modbus transport;
- model/firmware-specific DEYE register profiles;
- collection and durable offline buffering;
- local safety limits and command validation;
- write/read-back verification;
- heartbeat/health and signed OTA with rollback.

The cloud must never invent hardware support. A platform ACK is not proof of physical application; the edge agent must read back and report the actual result.

### EMS mobile application

The mobile app uses React Native + Expo + TypeScript and owns presentation and native-device concerns only:

- routing and mobile UI;
- secure EMS session storage;
- Deye and Home Assistant onboarding orchestration;
- charts and cached presentation data;
- push token lifecycle and deep links;
- local accessibility, localization and offline state;
- store build/release configuration.

Suggested structure:

```text
app/
├── (auth)/
├── (tabs)/
│   ├── overview/
│   ├── insights/
│   ├── notifications/
│   └── settings/
└── integrations/
    ├── deye/
    └── home-assistant/

src/
├── api/generated/
├── features/
├── components/
├── auth/
├── storage/
├── notifications/
├── analytics/
├── theme/
└── i18n/
```

Rules:

- generate API types from the platform OpenAPI document;
- do not hand-copy DTO definitions when generation is possible;
- keep EMS tokens only in SecureStore/Keychain/Keystore;
- never store Deye or Home Assistant secrets in AsyncStorage, logs, analytics or deep links;
- do not calculate forecasts, billing or canonical KPIs in the client;
- keep development, staging and production environments separate.

### EMS Home Assistant integration

The Home Assistant repository owns the custom integration only. It is not a generic Lovelace client and does not own platform business rules.

Suggested structure:

```text
custom_components/
└── ems_energy/
    ├── __init__.py
    ├── manifest.json
    ├── config_flow.py
    ├── options_flow.py
    ├── const.py
    ├── api.py
    ├── coordinator.py
    ├── sensor.py
    ├── binary_sensor.py
    ├── diagnostics.py
    ├── strings.json
    └── translations/
        ├── en.json
        └── ro.json

tests/
docs/
hacs.json
pyproject.toml
README.md
AGENTS.md
```

V1 behavior:

- the integration connects outbound to the EMS platform;
- pairing uses an opaque, one-time, expiring and tenant-scoped code;
- users explicitly select an allowlist of energy-relevant entities;
- unknown/unavailable/null values never become zero;
- Home Assistant data retains distinct source, quality and freshness;
- disconnect revokes credentials and stops ingestion;
- no port forwarding is required;
- no arbitrary light/lock/alarm/camera/person control;
- occupancy and sensitive context are opt-in;
- no general remote shell or arbitrary service execution.

If the integration is later proposed for Home Assistant Core, extract the platform communication layer into a separately versioned Python client library.

## Canonical contracts

Contracts start in `EMS-management-platform/contracts/`.

The platform CI should:

1. generate and validate OpenAPI/JSON Schema;
2. detect breaking changes;
3. publish a versioned artifact;
4. generate or validate the TypeScript mobile client;
5. run contract fixtures against device and Home Assistant consumers.

Contract rules:

- contract/API versioning is independent of application versions;
- changes are backward-compatible by default;
- unknown fields must be tolerated where the contract allows evolution;
- unit, sign, timezone, source, quality, measured time and received time are explicit;
- missing values remain null/unknown;
- shared fixtures are sanitized and deterministic.

Extract an `EMS-contracts` repository only when at least two external consumers use the contracts stably and independent release/versioning provides a concrete benefit.

## Cross-repository workflow

For every feature spanning repositories:

1. create an architecture decision record in `EMS-management-platform/docs/adr/`;
2. define the canonical contract before consumer implementations;
3. create an umbrella issue in `EMS-management-platform`;
4. create implementation issues in the repository that owns each change;
5. land a backward-compatible backend/contract first;
6. update mobile/device/Home Assistant consumers;
7. run cross-repository contract and staging tests;
8. remove old behavior only after the supported-client window expires.

Never couple deployment order to an unannounced breaking change.

## Versioning and releases

Each repository versions independently:

- platform: platform release plus API/contract compatibility;
- device: semantic version and protocol compatibility;
- mobile: semantic app version plus monotonic iOS/Android build number;
- Home Assistant: semantic integration version;
- contracts: independent version only after extraction.

All repositories should use:

- protected `main`;
- short-lived feature/fix branches;
- pull-request review and required CI;
- squash merge;
- `CODEOWNERS`;
- dependency automation;
- release tags and changelogs;
- environment-scoped secrets;
- explicit rollback/runbooks.

## AI-agent rules

Before editing code, an AI agent must:

1. read the local `AGENTS.md`;
2. read this repository map;
3. identify which repository owns the requested behavior;
4. inspect the canonical API/schema before creating DTOs or endpoints;
5. check related umbrella and companion issues;
6. keep changes scoped to one repository unless the task explicitly authorizes cross-repository work.

Agents must not:

- place mobile or Home Assistant code inside the FastAPI application;
- place cloud business logic in the Raspberry Pi agent;
- duplicate billing/forecast/optimization rules in mobile or Home Assistant;
- invent DEYE registers or claim unverified hardware support;
- store secrets in source, logs, analytics, fixtures or URLs;
- mark ACK as verified physical execution;
- turn missing/stale values into zero;
- introduce a breaking contract without migration and consumer coordination;
- report mock/simulator validation as real hardware/store validation.

When a task belongs elsewhere, create or reference the issue in the owning repository instead of implementing it in the wrong codebase.

## Current roadmap references

- Mobile MVP epic: [EMS-management-platform #215](https://github.com/bbogdan59/EMS-management-platform/issues/215)
- Mobile Home Assistant UX: [EMS-management-platform #216](https://github.com/bbogdan59/EMS-management-platform/issues/216)
- Platform Home Assistant/MQTT integration: [EMS-management-platform #189](https://github.com/bbogdan59/EMS-management-platform/issues/189)
