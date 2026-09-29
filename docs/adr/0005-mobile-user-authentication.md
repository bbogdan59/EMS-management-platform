# ADR 0005: Installation-bound mobile user sessions

Status: proposed for review with mobile issue [#8](https://github.com/bbogdan59/EMS-mobile-app/issues/8).
Umbrella: [platform #215](https://github.com/bbogdan59/EMS-management-platform/issues/215).

## Ownership and contract

The platform owns identity, password verification, email delivery, session persistence
and revocation. Native code owns its secure vault, local biometric gate and UI.
Canonical schemas are `app/schemas/mobile_auth.py`, exported by `/api/openapi.json`.
All endpoints below use `/api/v1/mobile/auth`; browser cookies and device credentials
are never accepted as mobile user credentials.

| Method/path | Behavior |
| --- | --- |
| GET `/capabilities` | Reports whether SMTP delivery is configured; does not promise successful delivery. |
| POST `/signup` | Neutral 202 for existing/new email; sends verification instructions. |
| POST `/signup/verify` | Requires a ten-digit email code, name, password and installation; creates account once. |
| POST `/login` | Password authentication and a new installation-bound session. |
| POST `/refresh` | Consumes one refresh token and returns its successor. Never retry automatically. |
| POST `/logout` | Revokes the family using any known refresh token plus the installation proof. |
| POST `/password-reset` | Neutral 202 and browser reset instructions; explicit 503 on unavailable email. |
| GET `/me`, `/sessions` | Bearer + `X-Installation-Id` + `X-Installation-Key`; only the current user's data. |
| POST `/sessions/{id}/revoke` | Same credentials plus password reauthentication; foreign sessions return 404. |

Opaque 256-bit access credentials expire in five minutes. Refresh credentials rotate
on each use; the family has an absolute 30-day expiry. PostgreSQL retains hashes of
spent refresh tokens until the family expires, so a replay revokes even its newest
access/refresh credentials. User then session row locks serialize login, refresh and
revocation across workers; a second simultaneous refresh is a replay, not a grace
period. Native clients serialize refresh and require sign-in after ambiguous network
failure. A client never supplies expiry; `expires_in` is for monotonic scheduling only.

The installation ID is not a secret. The additional random installation key binds
credentials to a vault; the backend stores its hash. Maximum ten active sessions per
account, and signing in again on an installation revokes its previous family. Session
lists are therefore bounded. Expired/revoked records are never authorized.

Email verification is limited to five attempts and 15 minutes. Signup uses a database
advisory lock per normalized email before a pending row exists; unique user email is
the final constraint. The password is supplied only when proving email ownership.
New users receive one personal organization; a pending invitation suppresses that
organization, and membership is granted by the existing invitation acceptance flow.
Reset and invitation links stay on the configured EMS web origin with existing CSRF,
no-store/no-referrer pages. They never establish mobile sessions or carry mobile access
or refresh tokens. Without SMTP, admins can use existing manual invitation delivery.

## Threat model

| Threat | Control and residual risk |
| --- | --- |
| Stolen access/refresh token alone | Installation proof required; hash-only database. Full vault compromise is not prevented. |
| Refresh replay/concurrent refresh | Persistent spent-token history, row locks, family revocation committed even on 401. |
| Account enumeration/brute force | Same errors for missing/inactive/locked users; Argon2 work on unknown users; Redis IP + hashed-email limits; durable account lockout; fail closed on Redis failure. Email contents differ only for the mailbox owner. |
| Lost device | Password-confirmed remote revocation in native Settings and `/settings/mobile-sessions` with browser auth/CSRF; every authorization checks current user/session state. |
| Disabled user/password reset/offboarding | Authorization and refresh reject inactive users; shared revocation helpers include mobile families. Clients erase local material when the server rejects the session. An offline device cannot learn of disable until reconnecting. |
| Local data exposure | Native refresh and installation secret in SecureStore; access in memory only; Keychain THIS_DEVICE_ONLY, Android backup exclusion; protected cache cleared on background/sign-out. Optional strong biometrics gate local access and still require server auth. |
| Reinstall/backup restore | Nonsecret app-filesystem marker must match Keychain installation ID. Missing/mismatched identity clears retained credentials before creating a fresh installation. Full-device migration behavior still needs physical-device testing. |
| Request/log/URL leakage | Never log credentials/body or SMTP exception contents. Validation responses omit Pydantic input. Mobile responses no-store; tokens only in bodies/headers. Operators must redact Authorization, X-Installation-Key and auth request bodies in proxies/APM. |
| Interrupted logout | Clear local credentials first, best-effort family revocation, explicit uncertainty if offline; user can revoke remotely. SecureStore deletion failure is surfaced and requires remote revocation. |
| Compromised device/process | Root/jailbreak, malicious keyboard, OS compromise and extraction of the entire vault remain out of scope; binding is not hardware attestation. Biometrics are a local UI gate, not a server authentication factor. |

## Rollout and operation

1. Deploy this additive migration and backend before shipping the mobile consumer.
2. Configure SMTP plus HTTPS `BASE_URL`; confirm real signup/reset delivery on staging.
3. Export the OpenAPI snapshot at the reviewed backend commit into the mobile PR.
4. Check native reinstall, backup restore, biometric cancellation/enrollment changes,
   locked-device storage behavior and remote revoke on physical iOS/Android devices.

Require TLS outside local development. Keep the proxy's client-IP configuration
trusted; the app does not trust arbitrary forwarded headers. Rate keys expire in
five minutes. Periodically purge `mobile_sessions` where `expires_at < now()` (refresh
history cascades) and expired `mobile_registrations`; never purge spent tokens from an
unexpired family. Existing database retention/backup policies apply to session metadata.
Rollback the mobile rollout independently; old web/device APIs are unchanged. Do not
downgrade the migration while a mobile deployment is serving traffic.

PostgreSQL integration tests cover replay, simultaneous rotation, token theft,
installation mismatch, server-clock expiry, disable, password reset, scoped/password-
confirmed revocation, browser CSRF, enumeration, email unavailability and signup.
The mobile companion covers lifecycle races, cache boundaries, single-flight refresh,
reinstall, storage failures, monotonic timing and biometric cancellation.
