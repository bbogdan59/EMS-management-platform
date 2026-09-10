# API pentru dispozitiv (`/api/v1`)

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

## 2. Heartbeat si capabilitati

```
POST /api/v1/devices/heartbeat
```

```json
{
  "boot_id": "boot-2026-09-10T08:00:00Z",
  "firmware_version": "1.2.0",
  "capabilities": { "max_charge_power_w": 3000, "max_discharge_power_w": 3000, "supports_export_control": true }
}
```

`capabilities` sunt informatii **raportate** de dispozitiv. Platforma nu
transforma automat o limita de putere raportata intr-o capabilitate
presupusa de comanda -- capabilitatile sunt afisate administratorului
(sectiunea Operatiuni), nu folosite implicit ca autorizare.

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

## 4. Telemetrie (batch)

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
- Maxim `DEVICE_TELEMETRY_BATCH_MAX_ITEMS` (implicit 500) elemente per cerere.
- Payload maxim `DEVICE_MAX_PAYLOAD_BYTES` (implicit 256 KiB).

Raspuns:

```json
{ "accepted": 1, "duplicates": 0, "rejected": 0, "errors": [] }
```

## 5. Configuratie curenta

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
