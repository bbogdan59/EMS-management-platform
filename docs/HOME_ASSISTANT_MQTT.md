# Home Assistant / MQTT — contract v1

Implementarea issue #189 este optionala, separata de telemetria invertorului si
de orice control fizic. Decizia si alternativele sunt in
[ADR 0003](adr/0003-home-assistant-mqtt.md).

## Activare si configurare

Pe web, worker si beat foloseste aceeasi configuratie:

```dotenv
HOME_ASSISTANT_MQTT_ENABLED=true
HOME_ASSISTANT_MQTT_BROKERS={"Broker acasa":"mqtts://mqtt.example.net:8883"}
# Optional: CA privata, instalata de operator in worker. Nu dezactiveaza hostname verification.
# HOME_ASSISTANT_MQTT_CA_FILE=/run/secrets/mqtt-ca.pem
```

Default: disabled, fara broker, fara conexiuni de retea sau credentiale necesare
pentru functionarea EMS. Este necesara migrarea Alembic la head; migratia
`f189a27c803e` adauga doar doua tabele. Downgrade le elimina (context efemer si
configuratie), fara a modifica valorile NULL/zero sau istoricul energetic existent.

1. Configureaza brokerul cu TLS si doua conturi dedicate statiei: publisherul
   HA si clientul EMS. Foloseste parole diferite si ACL-uri minimale.
2. In statie → Home Assistant, selecteaza brokerul permis, introdu credentialele
   clientului EMS, mapeaza explicit senzorii si confirma consimtamantul.
3. Copiaza prefixul generat pentru ACL: HA poate scrie `<prefix>/context/+`
   si citi `<prefix>/ems/state`; EMS poate citi `<prefix>/context/+` si, numai
   daca este acceptata publicarea, scrie `<prefix>/ems/state`. Refuza restul
   topicurilor pentru aceste conturi. Nu folosi `#` sau un cont comun tuturor statiilor.
4. Descarca pachetul `ems-home-assistant.yaml` si include-l in Home Assistant:

   ```yaml
   homeassistant:
     packages:
       ems: !include ems-home-assistant.yaml
   ```

   Pachetul este JSON valid (submultime YAML). Contine automatizari `mqtt.publish`
   pentru entitatile selectate si optional un senzor MQTT cu recomandarile EMS.
   Integrarea MQTT a HA trebuie conectata la acelasi broker cu propriul cont TLS.
5. Verifica configuratia HA si reincarca/repornieste dupa procedura HA. Apasa
   „Testeaza conexiunea”; beat preia testul in cel mult un ciclu de 30 s.
   „Conexiune verificata” atesta autentificarea si acceptarea subscrierii/publicarii,
   nu aplicarea de comenzi sau disponibilitatea senzorilor. Un broker poate
   filtra citirile prin ACL fara sa refuze SUBACK; verifica si „Date primite”.

La modificarea maparii/brokerului se schimba namespace-ul si ID-urile maparilor:
inlocuieste pachetul vechi, actualizeaza ACL si curata topicurile retained vechi.
Rotatia separata a credentialelor pastreaza maparile si topicurile. Mai intai
schimba credentialele pe broker, apoi salveaza perechea noua in EMS.
Rotatia `SECRET_KEY` necesita reintroducerea credentialelor, conform mecanismului
comun `app/core/crypto.py`. Nu exista copii in clar pentru recuperare.

## Contract inbound

Topic exact: `ems/v1/<station_uuid>/<stream_uuid>/context/<mapping_uuid>`.
UUID-urile definesc scope-ul, nu inlocuiesc autentificarea si ACL-ul brokerului.
Publica QoS 1, retained; fara retained, sesiunile scurte pot rata mesajul.

```json
{
  "schema_version": 1,
  "entity_id": "sensor.boiler_power",
  "sample_id": "2026-09-28T12:00:00+00:00",
  "observed_at": "2026-09-28T12:00:00+00:00",
  "source": "home_assistant",
  "kind": "power",
  "unit": "W",
  "value": "1234.567",
  "quality": "estimated",
  "available": true
}
```

Toate campurile sunt obligatorii; campurile suplimentare/versiunile necunoscute
sunt respinse. Maximum 4096 bytes. Timestamp cu offset obligatoriu, normalizat
UTC. `sample_id` stabil la retry (1–80 caractere alfanumerice/`_.:+-`). Nu genera
un timestamp nou pentru o valoare cache-uita. Pachetul HA foloseste `last_reported`,
nu momentul retransmiterii. Senzorii care nu raporteaza periodic devin stale;
alege pragul 30–3600 secunde in functie de cadenta reala, implicit 180 s.

| Tip | Domenii HA | Unitati | Valoare |
|---|---|---|---|
| `occupancy` | `binary_sensor` | `boolean` | true/false agregat, fara persoane sau numar de persoane |
| `boiler_status`, `hvac_status` | `binary_sensor`, `sensor` | `state` | on/off/idle/heating/cooling/defrosting; stare observata |
| `power` | `sensor` | W/kW | Decimal >=0, max 1000 kW, max 6 zecimale la sursa |
| `flexibility` | `sensor`, `input_number` | W/kW | Aceleasi limite; quality declared/simulated/stale, niciodata capabilitate masurata |

`source`: home_assistant/mqtt. `quality`: measured/estimated/declared/simulated/
stale/unknown. Unknown/unavailable necesita `value:null`; zero si false sunt
valori valide distincte. Puterea se normalizeaza Decimal in kW, se stocheaza si
se prezinta ca string zecimal. `source_quality`, `is_simulated`, `is_stale` si
timestampurile raman vizibile separat. `usable_as_measured` nu acorda control.

Pachetul generat marcheaza datele HA drept **estimated**: nu certifica senzorul
sursa sau hardware-ul. Daca un adaptor local are dovezi de masurare reala poate
publica `measured` folosind acelasi contract. Datele simulate trebuie marcate
`simulated` chiar daca valoarea pare plauzibila.

Nu exista istoric de prezenta. Fiecare mapare retine cel mult ultima observatie;
workerul elimina periodic valoarea mai veche de 24 h, inclusiv cand integrarea
este dezactivata global, pastrand doar watermarkul temporal pentru
replay. UI/API expun `value:null` imediat dupa expirarea pragului, chiar daca
workerul/brokerul este oprit. Nu se completeaza valori lipsa cu zero si nu se
insereaza contextul in `TelemetryRaw` sau in agregari/optimizer.

## Contract outbound

Topic: `<prefix>/ems/state`, QoS 1 retained, MQTT 5 MessageExpiryInterval=120 s.
Snapshot cu `schema_version=1`, station_id, generated_at, expires_at,
control_mode (nullable), state_provenance=configured, is_simulated (statie demo),
capabilities si maximum 5 recomandari disponibile/neexpirate ale acelei statii.
Recomandarile expun doar id, kind, status, title, confidence, coverage, quality,
reason_code, expires_at si review_path. Nu se exporta snapshoturi brute,
atribute private, credentiale sau stari de ocupare.

Capabilitati: `context_import=true`, `recommendations=true`,
`physical_control=false`, `commands=[]`. Consuma snapshotul numai pana la
expires_at si fiecare recomandare numai pana la propria expirare. Senzorul
generat in HA are expire_after=120. Nu conecta recomandarile la automatizari
care comanda hardware: aplicarea trebuie aprobata prin policy/capability
engine-ul EMS. Nicio ruta/topic MQTT nu accepta comenzi sau ACK de dispozitiv.

## Rute web / RBAC

Toate rutele sunt sub `/stations/{station_id}/integrations/home-assistant`:

| Metoda / sufix | Acces si efect |
|---|---|
| GET / | Viewer+ din statie; configuratie redactata si provenienta |
| GET /status | Viewer+; JSON v1, disponibilitate calculata la citire, no-store |
| GET /home-assistant.yaml | Organization admin+; pachet pentru maparile consimtite, fara credentiale |
| POST /configure | Organization admin+, CSRF; form cu revision, broker_key, username/password, consent=yes, publish_consent optional; liste paralele entity_id/kind/unit/max_age_seconds |
| POST /manage | Organization admin+, CSRF; action=test/rotate/revoke + revision; rotatia cere username/password |

Test/rotate sunt limitate la 10 incercari/ora/statie. Salvarea are optimistic
revision si lock PG; workerul foloseste lock skip_locked. Credentialele/entitatile
nu apar in audit sau task args. Erorile brokerului sunt mapate la coduri fixe,
fara text de exceptie. Testul UI foloseste aceleasi ACL-uri si publicare opt-in
ca sesiunea obisnuita; nu este un simplu TCP ping.

## Verificare

```sh
.venv/bin/ruff check .
.venv/bin/pytest tests/unit/test_home_assistant_contract.py tests/unit/test_home_assistant_mqtt.py tests/integration/test_home_assistant.py -q
.venv/bin/pytest tests/e2e/test_home_assistant_ui.py -q
```

Contract tests folosesc broker/HA mock deterministic: consent, auth, TLS,
SUBACK/PUBACK refuzat, reconnect/backoff, retained/duplicate/out-of-order,
stale, zero/NULL, spoofing de topic, redaction, cross-tenant, revocare si lockuri
PostgreSQL. Testul de protocol cu un broker real TLS este separat in
`tests/manual/test_home_assistant_broker.py`, opt-in prin mediu; nu ruleaza
implicit in CI si publica numai intr-un namespace de test generat aleator.

```sh
docker pull eclipse-mosquitto:2.0
EMS_MQTT_REAL_BROKER_TEST=1 .venv/bin/pytest tests/manual/test_home_assistant_broker.py -q
```

Testul porneste un container temporar pe localhost cu certificat, parola si
ACL de test; verifica retained/reconnect, autentificare, certificat invalid si
refuzul publicarii pe un topic nepermis, apoi elimina containerul.

Pilotul HA real necesita o instanta separata si consimtamantul administratorului:

1. Instaleaza pachetul generat, verifica configuratia HA si foloseste senzori de
   test fara date de prezenta personala; marcheaza datele sintetice simulated.
2. Confirma importul valorii zero, unknown/unavailable, timestampul sursa si
   afisarea stale dupa oprirea HA. Verifica o reconectare dupa oprirea brokerului.
3. Confirma ca senzorul EMS din HA devine indisponibil dupa oprirea workerului,
   iar mesajele de comanda arbitrare nu creeaza `Command` in EMS.
4. Verifica refuzul topicurilor altei statii prin ACL si prin contractul EMS.
5. Roteste parola, revoca integrarea, elimina pachetul si mesajele retained de
   test; confirma stergerea contextului si functionarea dashboardului independent.

Validarea pe broker real, in browser, in CI si pe Home Assistant/hardware real
se raporteaza separat. Un broker mock sau un test de protocol nu certifica
integrarea fizica a boilerului/HVAC si nu autorizeaza controlul live.
