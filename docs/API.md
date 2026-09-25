# API pentru dispozitiv (`/api/v1`)

Extensiile pentru control cu aprobare si observatii EV sunt documentate in
[ENERGY_OPERATIONS.md](ENERGY_OPERATIONS.md). Planurile publicate sunt Shadow
pana la aprobare. Comenzile manuale fara plan/politica nu sunt livrabile.
Agentul curent EMS-device-code v0.1 ramane read-only; extensiile de mai jos nu
reprezinta suport hardware verificat.

### Read-back separat de ACK/aplicare

`POST /api/v1/commands/{command_id}/readback`, cu autentificarea device obisnuita:

```json
{
  "schema_version": 1,
  "observed_at": "2026-10-01T00:01:05Z",
  "command_version": 1,
  "idempotency_key": "identitatea-exacta-din-comanda",
  "target_soc_percent": "60.00",
  "battery_power_kw": "1.000",
  "quality": "measured"
}
```

Necesita un rezultat `executed` anterior, acelasi device autorizat si o
observatie ulterioara aplicarii, inainte de expirarea comenzii si veche de cel
mult 5 minute. Raspunde cu `command_id` si `verification_status`; conflictul,
expirarea sau lipsa capabilitatii produc 409. Versiunile/campurile necunoscute
si numerele nefinite produc 422. Un retry identic este idempotent.

### Observatii EV

`POST /api/v1/ev/connectors/{connector_id}/observations`:

```json
{
  "schema_version": 1,
  "event_id": "boot-123:42",
  "observed_at": "2026-10-01T00:01:05Z",
  "state": "charging",
  "meter_kwh": "1042.125000",
  "meter_epoch": "meter-installation-1",
  "power_kw": "7.400",
  "vehicle_soc_percent": null,
  "vehicle_id": null,
  "quality": "measured"
}
```

State: disconnected, connected, available, charging, paused, completed, faulted.
Quality: measured, estimated, stale, simulated. Meter/power/SOC pot lipsi; NULL
nu este zero. SOC/vehicul necesita consimtamant; contorul/SOC necesita capabilitati
declarate. EVSE trebuie asociat exact device-ului autentificat din statia activa.
Raspuns: `event_id`, `status` (accepted, duplicate, retained_out_of_order), `applied`.
Conflictele de identitate/scop/tranzitie produc 409, payload invalid 422.

Inventarul uman foloseste `GET/POST /api/stations/{station_id}/evses` cu sesiune
utilizator; POST necesita organization_admin si CSRF. Schema `EVSEIn` din
OpenAPI include nume, device optional, limita kW, capabilitati, consimtamant si
retentie. UI/API nu trimit comenzi fizice catre EVSE.

Acest document e destinat dezvoltatorului **viitorului controler local**
(Raspberry Pi/ESP32 + Modbus/RS485 catre invertorul Deye), care nu face parte
din acest repository. Platforma expune un API REST versionat prin care acel
dispozitiv:

1. se asociaza unei statii printr-un cod unic cu expirare;
2. trimite telemetrie in batch-uri, cu deduplicare;
3. preia configuratia tehnica si preferintele curente ale statiei;
4. preia planul activ publicat de optimizator (informativ in modul `shadow`);
5. preia comenzi semantice concrete, le confirma/respinge si raporteaza rezultatul.

**Contract important:** platforma NU este mecanismul de protectie electrica.
Dispozitivul local trebuie sa isi valideze propriile limite de siguranta si
poate respinge orice comanda primita, in orice moment, indiferent de ce a
publicat platforma. Nu exista niciun endpoint pentru scriere bruta de
registre Modbus -- toate comenzile sunt semantice (`type` + `parameters`
documentate mai jos).

Toate exemplele folosesc `Content-Type: application/json`. Documentatia
interactiva (OpenAPI/Swagger) e disponibila la `/api/docs` cand platforma
ruleaza.

## Autentificare

Dupa asociere, fiecare cerere (in afara de `POST /devices/claim`) trebuie sa
includa header-ul:

```
Authorization: Bearer <device_id>.<credential_secret>
```

`credential_secret` e afisat **o singura data**, in raspunsul la `claim` (sau
la rotatia de credentiale). Platforma stocheaza doar hash-ul lui (Argon2) --
daca il pierzi, singura solutie e rotatia sau re-asocierea.

## Conventii de semn si unitati

- Puterile instantanee sunt transmise in **watti (W)**; energia agregata in
  platforma e in **kWh**. UI-ul afiseaza kW pentru putere si kWh pentru energie
  -- niciodata amestecate.
- `battery_power_w`: **pozitiv = incarcare**, **negativ = descarcare**.
- `grid_power_w`: **pozitiv = import din retea**, **negativ = export in retea**.
- `pv_power_w`, `load_power_w`, `ev_power_w`: intotdeauna `>= 0`.
- Toate timestamp-urile sunt UTC, format ISO 8601 cu fus orar explicit
  (`...Z` sau `+00:00`). Platforma le afiseaza in UI convertite in fusul orar
  al statiei.

## 1. Asociere (claim)

```
POST /api/v1/devices/claim
```

Cererea:

```json
{
  "claim_code": "EMS-7F3K-9QRT",
  "device_name": "Deye Bridge - Casa Popescu",
  "hardware_info": { "model": "raspberry-pi-4", "firmware": "1.0.0" }
}
```

Raspuns `201 Created`:

```json
{
  "device_id": "b7e2f3c1-...-...",
  "station_id": "a1c4d9e0-...-...",
  "credential_secret": "mNAihwOUbNlzwB2x77eKO2oQgJjhD24_YAS3_acdlOo"
}
```

Codul de asociere este generat de un operator/administrator in UI (statie ->
Dispozitive -> "Genereaza cod nou"), e valabil o perioada limitata
(implicit 15 minute, configurabil prin `DEVICE_CLAIM_CODE_TTL_MINUTES`) si
poate fi folosit o singura data. Un cod expirat sau deja folosit returneaza
`400 Bad Request` cu un mesaj explicit.

## 1b. Enrollment automat (issue #16) -- alternativa fara cod de asociere

```
POST /api/v1/devices/enroll
```

Flux DISTINCT de asocierea legacy cu cod temporar (sectiunea 1): dispozitivul
se prezinta singur, cu o identitate PROPRIE (nu i-o da serverul), fara sa
aleaga nicio statie/tenant. Clientul il poate asocia ulterior cu Device Code
sigilat; platform-adminul pastreaza si fluxul operational din
`/admin/devices/pending`.

**Nu necesita header `Authorization`** -- dispozitivul nu are inca nicio
credentiala emisa de server. Dovada de identitate e `provisioning_secret`,
generat si pastrat LOCAL de dispozitiv (ex. la prima pornire, persistat
langa restul starii lui), transmis in corp.

Cererea (idempotenta -- vezi mai jos):

```json
{
  "installation_uuid": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "provisioning_secret": "un-secret-lung-generat-si-pastrat-local-de-device",
  "serial_number": "EMS-ABCD-EFGH-IJKL-MNOP",
  "activation_code": "ACT-ABCDE-FGHIJ-KLMNO-PQRST-UVWXY-Z",
  "hardware_info": { "model": "raspberry-pi-4", "firmware": "0.1.0" }
}
```

`serial_number` si `activation_code` sunt optionale numai pentru agentii
legacy. Unitatile noi le trimit impreuna. Serverul stocheaza seria publica si
doar SHA-256-ul codului de activare; codul in clar nu este stocat in baza de
date, log sau audit. Codul este un bearer secret cu entropie mare, livrat
sigilat clientului, nu seria publica.

Raspuns `200 OK`, inainte de alocare:

```json
{
  "status": "pending",
  "device_id": null,
  "station_id": null,
  "credential_secret": null,
  "enrollment_expires_at": "2026-09-13T20:00:00Z",
  "message": null
}
```

Dupa ce un administrator aloca device-ul unei statii (din UI), ACELASI
apel, cu ACEEASI identitate, intoarce:

```json
{
  "status": "assigned",
  "device_id": "b7e2f3c1-...-...",
  "station_id": "a1c4d9e0-...-...",
  "credential_secret": "mNAihwOUbNlzwB2x77eKO2oQgJjhD24_YAS3_acdlOo",
  "enrollment_expires_at": null,
  "message": null
}
```

**Idempotenta si recuperare:** aceasta cerere e sigura de reincercat
oricand cu aceeasi identitate (`installation_uuid`+`provisioning_secret`).
Daca raspunsul de alocare se pierde in retea, dispozitivul nu are nevoie de
reprovisionare -- reincearca acelasi `POST /devices/enroll` si primeste din
nou `status=assigned` cu ACEEASI `credential_secret`, pana la prima cerere
autentificata reusita cu acea credentiala (ex. primul heartbeat), dupa care
serverul nu o mai poate retrimite (a fost stearsa din stocarea in clar) --
recuperarea ramane totusi posibila prin rotatie de credentiale (sectiunea 3),
odata ce dispozitivul e deja autentificat macar o data.

Daca `installation_uuid` e deja cunoscut dar `provisioning_secret` NU se
potriveste (identitate falsificata / cineva incearca sa "insuseasca" un
`installation_uuid` declarat de altcineva): `409 Conflict`, fara nicio
schimbare de stare. Alocarea unui enrollment expirat (implicit 72h,
configurabil prin `DEVICE_ENROLLMENT_TTL_HOURS`), deja alocat, sau revocat
e respinsa explicit de server (nu se produce silentios o schimbare de
tenant sau o realocare).

Un enrollment pending expirat se reinnoieste cand device-ul reapare cu
`installation_uuid` si `provisioning_secret` corecte. Astfel, o unitate poate
sta oprita in depozit mai mult de 72h fara reprovisionare.

### Asociere self-service de catre client

Un `organization_admin` deschide `/stations/{station_id}/devices` si introduce
Device Code de pe eticheta sigilata. Ruta web `POST
/stations/{station_id}/devices/activate` consuma codul atomic, leaga device-ul
pending de acea statie si nu afiseaza credentiala device-ului utilizatorului.
Agentul o recupereaza prin urmatorul `POST /api/v1/devices/enroll` autentificat
cu secretul de provisioning. Raspunsurile pentru cod necunoscut, expirat sau
deja folosit sunt identice, iar incercarile sunt limitate per utilizator si
statie.

`installation_uuid` si seria publica sunt DOAR chei de corelare -- NU
autorizeaza singure nicio statie/organizatie. Alocarea self-service necesita
Device Code-ul separat si este auditata (`device_activated_by_customer`).

## 2. Heartbeat si capabilitati

```
POST /api/v1/devices/heartbeat
```

```json
{
  "boot_id": "boot-2026-09-10T08:00:00Z",
  "firmware_version": "1.2.0",
  "capabilities": { "max_charge_power_w": 3000, "max_discharge_power_w": 3000, "supports_export_control": true },
  "system_stats": { "cpu_load_1m": 0.42, "memory_used_percent": 51.2, "memory_total_mb": 3819.4, "temperature_c": 46.5, "disk_used_percent": 12.8 }
}
```

`capabilities` sunt informatii **raportate** de dispozitiv. Platforma nu
transforma automat o limita de putere raportata intr-o capabilitate
presupusa de comanda -- capabilitatile sunt afisate administratorului
(sectiunea Operatiuni), nu folosite implicit ca autorizare.

`system_stats` (optional, camp liber) este instantaneul RAPORTAT de
dispozitiv al resurselor locale -- un camp lipsa inseamna ca dispozitivul nu
l-a putut citi, niciodata 0 inventat. Spre deosebire de `capabilities`
(combinat), `system_stats` este INLOCUIT integral la fiecare heartbeat: un
camp care nu mai e raportat dispare, nu ramane cu valoarea veche. Afisat pe
pagina admin a flotei de device-uri si pe pagina de configurare a
device-ului (statie).

Raspuns:

```json
{
  "server_time": "2026-09-10T08:00:01Z",
  "device_status": "active",
  "has_active_plan": true,
  "pending_command_count": 1
}
```

## 3. Rotatie credentiale

```
POST /api/v1/devices/credentials/rotate
```

Necesita autentificare cu credentiala **curenta**. Raspunsul contine noul
secret; cel vechi e revocat imediat.

## 3b. Jurnal compact de debug

```
POST /api/v1/devices/logs
```

```json
{
  "entries": [
    { "occurred_at": "2026-09-18T08:00:01Z", "level": "warning", "code": "cloud_http_error", "detail": "status=503" }
  ]
}
```

Linii COMPACTE (`code` max 64 caractere, `detail` optional max 200), niciodata
stack trace-uri sau payload brut -- gandite pentru debugging live pe pagina de
configurare a device-ului (statie), nu ca inlocuitor pentru `journalctl` de pe
unitate. Maxim 50 de intrari per batch. Server-ul pastreaza doar ultimele **10
zile** per device (sterse automat la fiecare ingest nou); nu exista
ACK/outbox ca la telemetrie -- pierderea unui batch la o intrerupere de retea
e acceptabila, nu necesita retry garantat.

Raspuns: `{"accepted": 1}`.

## 4. Contract telemetrie

```
GET /api/v1/telemetry/contract
```

Endpoint read-only, fara autentificare, folosit de dispozitiv/simulator pentru
a descoperi contractul masinabil al telemetriei acceptate de schema v1:

- `schema_version`, endpointul de ingestie si cheia de deduplicare;
- limitele temporale (`max_future_skew_seconds`, `max_age_days`,
  `late_after_seconds`);
- lista de metrici canonice, cu unitate, nullable, calitate si conventie de
  semn;
- schema extensiilor tipizate acceptate (`mppt`, `phases`, `battery`,
  `status`, `counters`) si unitatile lor;
- statuturile ACK si codurile de motiv retryable/permanent.

`raw_payload` ramane doar diagnostic/source payload. Metricile extinse
acceptate explicit in schema v1 sunt validate ca structura si pastrate in
`raw_payload.extended`; ele nu intra in agregarea energetica canonica pana nu
exista o regula de promovare separata. Metricile neacceptate explicit nu devin
telemetrie canonica doar fiindca apar in `raw_payload`.

## 5. Telemetrie (batch)

```
POST /api/v1/telemetry/batch
```

```json
{
  "items": [
    {
      "boot_id": "boot-2026-09-10T08:00:00Z",
      "sequence": 42,
      "schema_version": 1,
      "measured_at": "2026-09-10T08:05:00Z",
      "pv_power_w": 2500.0,
      "load_power_w": 800.0,
      "battery_power_w": -300.0,
      "grid_power_w": -1400.0,
      "battery_soc_percent": 62.5,
      "ev_connected": false,
      "ev_power_w": 0,
      "mppt": [
        {"index": 1, "voltage_v": 390.2, "current_a": 4.8, "power_w": 1873.0, "quality": "measured"}
      ],
      "phases": [
        {"phase": "L1", "voltage_v": 230.1, "current_a": 3.4, "active_power_w": 782.0}
      ],
      "battery": {"voltage_v": 51.8, "current_a": -5.8, "temperature_c": 28.4, "state": "discharging"},
      "status": {
        "inverter_state": "running",
        "battery_state": "discharging",
        "faults": []
      },
      "counters": [
        {"name": "pv_energy_total", "value": 12345.678, "unit": "kWh", "reset_id": "meter-boot-7"}
      ],
      "quality_flags": {},
      "raw_payload": {}
    }
  ]
}
```

- **Deduplicare**: cheia `(device_id, boot_id, sequence)` e unica. O
  reincercare dupa o eroare de retea, cu acelasi `boot_id`/`sequence`, e
  idempotenta (raspunsul indica `duplicates`, nu creeaza randuri noi).
- `sequence` trebuie sa fie monoton crescator in cadrul unui `boot_id`; la
  fiecare repornire fizica a dispozitivului, foloseste un `boot_id` nou.
- Metricile de putere sunt in W. `battery_power_w > 0` inseamna incarcare,
  `battery_power_w < 0` descarcare; `grid_power_w > 0` inseamna import,
  `grid_power_w < 0` export. `pv_power_w`, `load_power_w` si `ev_power_w`
  trebuie sa fie `>= 0`. `battery_soc_percent` trebuie sa fie intre `0` si
  `100`; `0` este valoare masurata valida, nu lipsa.
- Maxim `DEVICE_TELEMETRY_BATCH_MAX_ITEMS` (implicit 500) elemente per cerere.
- Payload maxim `DEVICE_MAX_PAYLOAD_BYTES` (implicit 256 KiB).
- `mppt`, `phases`, `battery`, `status` si `counters` sunt extensii tipizate,
  cu unitati explicite si `quality` per segment (`measured`, `derived`,
  `simulated`, `stale`). Serverul le valideaza si le pastreaza sub
  `raw_payload.extended`, dar nu le foloseste la agregari energetice in schema
  v1. Contoarele cumulative sunt in `kWh`; daca un contor fizic se reseteaza
  sau face rollover, device-ul trebuie sa schimbe `reset_id`, astfel incat un
  backfill viitor sa nu interpreteze diferenta ca spike de energie.

Raspuns:

```json
{
  "accepted": 1,
  "duplicates": 0,
  "rejected": 0,
  "errors": [],
  "results": [
    {
      "boot_id": "boot-2026-09-10T08:00:00Z",
      "sequence": 42,
      "status": "accepted",
      "retryable": false,
      "reason_code": null
    }
  ]
}
```

`results` este in aceeasi ordine ca `items` si permite ACK selectiv. Starile
sunt `accepted`, `duplicate` (ambele pot fi eliminate sigur din outbox) si
`rejected`. Pentru un item respins, `retryable=true` inseamna ca acelasi item
poate deveni acceptabil ulterior (de exemplu ceasul device-ului este temporar
in viitor); `retryable=false` il trimite in dead-letter pentru inspectie, nu il
sterge silentios. Motive permanente curente: `timestamp_too_old`,
`pv_power_negative`, `load_power_negative`, `ev_power_negative`,
`battery_soc_out_of_range`. Motiv retryable curent: `future_timestamp`.
Campurile agregate si `errors` raman pentru clientii v1. Validarea structurala
Pydantic a anvelopei ramane atomica: un payload invalid care nu poate fi
identificat sigur prin `boot_id`/`sequence`, are tipuri gresite, timestamp fara
fus orar sau numere non-finite (`NaN`, `Infinity`) primeste HTTP 422 pentru
intregul request.

## 6. Configuratie curenta

```
GET /api/v1/config
```

```json
{
  "station_id": "a1c4d9e0-...",
  "timezone": "Europe/Bucharest",
  "execution_mode": "shadow",
  "inverter_power_kw": "5.000",
  "battery_max_charge_power_kw": "3.000",
  "battery_max_discharge_power_kw": "3.000",
  "grid_import_limit_kw": "10.000",
  "grid_export_limit_kw": "8.000",
  "allow_grid_charge": false,
  "allow_battery_export": false,
  "min_reserve_soc_percent": "15.00",
  "max_normal_soc_percent": "95.00",
  "config_version": 3,
  "preference_version": 5
}
```

`execution_mode`:
- `shadow` (implicit) -- planul si comenzile sunt **doar informative**.
  Dispozitivul NU trebuie sa execute fizic nimic pe baza lor.
- `live` -- dispozitivul poate executa comenzile primite, sub propria
  validare de siguranta.

## 6. Planul activ

```
GET /api/v1/plan/active
```

```json
{
  "plan_id": "8e2a...",
  "version": 4,
  "status": "published",
  "execution_mode": "shadow",
  "published_at": "2026-09-10T05:00:00Z",
  "intervals": [
    {
      "interval_start": "2026-09-10T08:00:00Z",
      "interval_end": "2026-09-10T08:15:00Z",
      "battery_power_target_kw": 1.2,
      "grid_power_target_kw": 0.0,
      "battery_soc_target_percent": 58.6,
      "ev_charge_power_kw": 0.0,
      "explanation": "Se incarca bateria cu 1.20 kW din surplusul de productie PV."
    }
  ]
}
```

```
POST /api/v1/plan/accept?version=4
```

Marcheaza explicit acceptarea planului de catre dispozitiv (separat de
simpla citire). Necesar doar pentru statii `execution_mode=live`.

## 7. Comenzi

```
GET /api/v1/commands/pending
```

```json
[
  {
    "command_id": "c9f1...",
    "type": "set_battery_target_soc",
    "parameters": { "target_soc_percent": 60, "battery_power_kw": 1.2 },
    "version": 1,
    "idempotency_key": "plan:8e2a...:interval:2026-09-10T08:00:00+00:00",
    "reason": "Se incarca bateria cu 1.20 kW din surplusul de productie PV.",
    "author": "optimizer",
    "valid_from": "2026-09-10T08:00:00Z",
    "expires_at": "2026-09-10T08:15:00Z"
  }
]
```

La preluare, comanda trece automat din `created` in `delivered`. Tipuri de
comenzi disponibile (`type`): `set_battery_target_soc`,
`set_charge_power_limit`, `set_discharge_power_limit`, `hold_battery`,
`allow_grid_charge`, `disallow_grid_charge`, `allow_export`,
`disallow_export`, `resume_automation`, `suspend_automation`. Nu exista un
tip generic de "scriere registru" -- orice comanda noua necesita o extensie
explicita a acestui enum, revizuita ca atare.

Confirmare/respingere:

```
POST /api/v1/commands/{command_id}/ack
{ "status": "accepted" }   // sau "rejected", cu "reason"
```

Raportare rezultat (doar dupa `accepted`):

```
POST /api/v1/commands/{command_id}/result
{ "status": "executed", "details": { "applied_battery_power_kw": 1.2 } }
// sau "failed", cu "error_message"
```

**Starea "executata" provine EXCLUSIV din acest raport al dispozitivului.**
Salvarea/publicarea unei comenzi in platforma nu inseamna niciodata ca a
fost aplicata fizic.

O comanda cu `expires_at` depasit e marcata automat `expired` si nu mai
poate fi confirmata/respinsa -- dispozitivul trebuie sa astepte urmatoarea
comanda din plan.

## 8. Firmware OTA (issue #168)

"Firmware" inseamna strict versiunea aplicatiei EMS-device-code instalate pe
Raspberry Pi -- niciodata firmware-ul invertorului DEYE, niciodata un
upgrade de Raspberry Pi OS. Verificarea criptografica/instalarea atomica/
restart-ul/rollback-ul sunt implementate device-side (EMS-device-code#13);
platforma emite doar o TINTA structurata (`release_id`), niciodata o comanda
shell sau un URL arbitrar.

Campuri tipizate noi la enrollment (`POST /devices/enroll`, toate opționale
pentru compatibilitate cu device-uri vechi): `agent_version`, `build_id`,
`hardware_platform`, `architecture`, `os_version`. Un enrollment idempotent
poate actualiza aceste campuri + `last_seen_at` pe un device inca `pending`,
fara sa schimbe tenantul/alocarea. Heartbeat-ul accepta acum aceleasi campuri
(`build_id`/`hardware_platform`/`architecture`/`os_version`), optionale.

```
GET /api/v1/firmware/pending
```

Returneaza `null` sau oferta curenta (nu doar `offered` -- si o implementare
la mijlocul fluxului, ca device-ul sa poata relua dupa o reconectare):

```json
{
  "deployment_id": "d1a2...", "release_id": "r5b6...", "target_version": "1.4.0",
  "channel": "stable", "status": "offered", "is_downgrade": false,
  "offer_expires_at": "2026-09-18T09:00:00Z",
  "download_url": "https://...presemnata, durata limitata...",
  "sha256_hex": "...", "signature_ed25519_hex": "...", "signing_key_id": "prod-key-1",
  "artifact_size_bytes": 12345678
}
```

Progresul se raporteaza exclusiv de pe device:

```
POST /api/v1/firmware/deployments/{deployment_id}/events
{ "event_type": "downloading" | "verified" | "installing" | "restarting" | "confirmed" | "failed" | "rejected",
  "payload": {...}, "message": "..." }
```

Stare (`FirmwareDeploymentStatus`): `requested -> offered -> downloading ->
verified -> installing -> awaiting_confirmation -> succeeded`, cu
alternative terminale `rejected | failed | timed_out | rolled_back |
cancelled`. Evenimentul `restarting` cere `payload.boot_id` (boot_id-ul
CURENT, dinainte de repornire) si muta starea direct la
`awaiting_confirmation`. Evenimentul `confirmed`, dupa repornire, cere
`payload.boot_id` (NOU, diferit de cel dinainte) si `payload.version` --
succesul (`succeeded`) se marcheaza NUMAI aici, niciodata la oferta/dispatch;
`payload.rolled_back=true` sau o versiune diferita de tinta produce
`rolled_back`. Acest apel `confirmed` este el insusi un contact autentificat
in direct -- versiunea/boot_id-ul device-ului se actualizeaza imediat, fara
sa astepte urmatorul heartbeat.

Un device `pending` (neasociat, fara credentiala Bearer inca) primeste
oferta prin campul optional `firmware_offer` din raspunsul `EnrollResponse`
(acelasi format ca mai sus), livrata NUMAI dupa verificarea reusita a
identitatii de provisioning.

Backoffice (`platform_admin`, `/admin/firmware/releases` si
`/admin/firmware/rollouts`): registrul de release-uri e imutabil dupa
publicare (o corectie produce un release nou), semnat Ed25519 si hash-uit
SHA-256, cu artifactul intr-un backend de object storage configurabil
(niciodata pe discul efemer Railway -- vezi `FIRMWARE_STORAGE_BACKEND` in
`.env.example`). Un rollout controleaza concurenta, downgrade (blocat
implicit, necesita motiv explicit) si se opreste automat la un prag de esec.

## Coduri de eroare relevante

| Cod | Semnificatie |
|---|---|
| 401 | Credentiale lipsa/invalide/dispozitiv revocat |
| 403 | Payload peste limita configurata |
| 404 | (nefolosit pentru API-ul de dispozitiv -- resursele inexistente dau 400/409 cu mesaj) |
| 409 | Tranzitie de stare invalida (ex. rezultat raportat pentru o comanda nerespinsa) |
| 413 | Payload peste `DEVICE_MAX_PAYLOAD_BYTES` |
| 429 | Rate limit depasit (`DEVICE_API_RATE_LIMIT_PER_MINUTE` per dispozitiv) |

## Rezumat ciclu de viata plan/comanda

```
OptimizationRun (scenariu calculat)
    -> Plan (status=published, execution_mode=shadow|live)
        -> GET /plan/active (dispozitivul citeste planul)
        -> POST /plan/accept (acceptare explicita, doar in modul live)
        -> Command (generata din intervalul curent al unui plan acceptat, doar in modul live)
            -> POST /commands/{id}/ack (accepted|rejected)
            -> POST /commands/{id}/result (executed|failed)   <- singura sursa a starii "aplicat"
        -> efectul observat se reconciliaza ulterior din telemetria reala (PlanInterval.observed_*)
```

## Health, diagnostic access and notifications

See [HEALTH_DIAGNOSTICS.md](HEALTH_DIAGNOSTICS.md) for the complete contract,
retention policy, lifecycle, controls and verification scope.

Telemetry schema versions 1 and 2 accept typed `inverter` temperatures/raw status,
`battery.soh_percent`, grid/load `phases[].circuit`, and optional
`counters[].rollover_kwh`. Flat extensions emitted by EMS-device-code v0.1 are
normalized; raw status numbers are not decoded into alarms. Both simulation flags
are honored. `telemetry_raw.diagnostics` is the validated diagnostic source.

Late samples outside raw retention are permanently rejected with
`retention_window_expired`; a three-hour guard protects complete adjacent buckets.
Accepted historical samples queue durable background reaggregation by measured
UTC hour, never arrival hour. This does not change the existing ACK envelope.

Web endpoints use session authentication. All POSTs require CSRF:

| Method/path | Scope and behavior |
|---|---|
| `GET /fleet/health` | Authorized memberships and unexpired diagnostic grants; filters `online`, `severity`, `firmware`, `quality`, `fault`, `update_state` |
| `GET /stations/{id}/health` | Viewer or explicit diagnostic grant; evidence, lifecycle and timeline |
| `POST /stations/{id}/health/{alert_id}` | Operator+, form `action` and required `reason`; station-scoped |
| `POST /stations/{id}/diagnostic-grants` | Organization admin+, form `user_id`, aware `expires_at`, optional `revoke`; maximum 30 days |
| `GET /stations/{id}/diagnostics/export?start=...&end=...` | Viewer or grant; aware instants, maximum 31 days, deterministic sanitized JSON |
| `GET /health/runbooks` | Authenticated; versioned rule catalog |
| `GET /notifications` | Current user's active organizations only |
| `POST /notifications/{id}/read` | Current recipient and active station authorization |
| `POST /organizations/{id}/notification-preferences` | Viewer+, own preference; timezone, quiet hours 0-23, escalation 0-1440 minutes, matrix fields, weekly/opt-out |
| `POST /organizations/{id}/notifications/verify` | Own email; blank code requests queued verification, otherwise verifies expiring code |
| `POST /organizations/{id}/notifications/push` | Own browser; validated `PushSubscription` JSON, encrypted storage |
| `POST /stations/{id}/assistant` | Viewer membership (grants excluded), CSRF, feature flag; form `question`, optional local `day`; bounded read-only evidence response |
