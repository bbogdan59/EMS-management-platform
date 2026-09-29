# ADR 0003: Context local Home Assistant prin MQTT, optional si fara comenzi

- Data: 2026-09-28
- Issue: #189; continua separarea telemetry/recommendation/live din #52 / ADR 0002.
- Status: implementat pentru context si recomandari; pilotul Home Assistant real ramane o validare separata.

## Alternative

| Integrare | Avantaje | Costuri si riscuri |
|---|---|---|
| Cloud direct / API REST-WebSocket Home Assistant | Acces la stari si evenimente, fara broker | Expunerea HA sau VPN; token cu acces larg; tentatia de a citi toate entitatile si de a apela servicii arbitrare |
| Broker MQTT + pachet HA explicit | ACL per statie, import minim, TLS, compatibil cu producatori MQTT diferiti; HA poate ramane in reteaua locala | Broker accesibil din HA si worker; configuratie TLS/ACL; schema proprie pentru timestamps/provenance |
| Componenta sau add-on HA dedicat | UX nativ de mapare, poate pastra verificari locale | Instalare, distributie, suport pentru versiuni HA si securizarea unui nou proces; cost nejustificat inaintea pilotului |

## Decizie

MQTT 5 prin TLS verificat, opt-in global si per statie. Endpointurile sunt
allowlistate de operator, nu URL-uri arbitrare furnizate de tenant. Credentialele
per statie sunt criptate cu mecanismul existent. Nu este necesar un token HA.
Un pachet HA generat publica doar entitatile selectate si creeaza optional un
senzor pentru starea EMS/recomandari. Nu exista discovery, citire generala de
atribute, import de istoric sau apel de servicii fizice.

Folosim sesiuni de worker scurte, la 30 s, cu mesaje de context retained.
Retained nu inseamna proaspat: observatia este datata la sursa, expirata dupa
pragul maparii si nu este reinnoita de retry. Alegerea evita un daemon suplimentar
si pastreaza dependentele operationale existente (Celery/beat). Consecinta este
latenta de ordinul a 30–60 s, fara garantie de captare a tuturor tranzitiilor;
nu este un canal pentru bucle de control sau arhivarea evenimentelor.

O conexiune are maximum 20 de mapari, cel mult 200 mesaje per sesiune si payload
de context de maximum 4 KiB. Lockul PostgreSQL pe conexiune serializeaza workerul
cu salvarea, rotatia si revocarea. Testul UI programeaza urmatoarea sesiune;
CONNACK, SUBACK si (la publicare) PUBACK sunt verificate de adaptor. Revocarea
asteapta sesiunea scurta deja in curs; dupa commit niciun worker nu mai importa
sau publica. Un worker blocat este oprit de limitele Celery 20/25 s.

## Limita de control si compatibilitate

Contractul canonic de telemetrie a invertorului ramane neschimbat. Puterea
unui boiler nu se aduna automat la consumul deja masurat al statiei. Contextul
are stocare separata, ultima observatie per entitate; nu reprezinta inventar
de device, capabilitate de scriere, ACK sau readback.

Flexibilitatea in kW este **declarata**, nu o capabilitate hardware verificata.
`physical_control=false` si `commands=[]` sunt publicate explicit. MQTT nu are
endpoint/topic de comanda si nu creeaza `Command`, aprobari sau politici.
Recomandarile publicate se revizuiesc in UI-ul EMS; aplicarea continua prin
`recommendation_service.act` / `control_service`, cu RBAC, policy/capability,
versiuni, expirare si verificare separata. Controlul termic din #52 ramane in
etapa recommendation/shadow. Contextul nu este inca intrare a optimizerului.

## Amenintari si masuri

| Risc | Masura |
|---|---|
| Import prezenta individuala | Allowlist de tipuri/domain-uri, valoare occupancy strict boolean, acord explicit ca senzorul este agregat; fara person/device_tracker/atribute |
| Cross-tenant / broker compromis | StationAccess pe toate rutele, topicuri generate per statie/configuratie, verificare topic + entitate + tip + unitate; conturi si ACL separate pe broker |
| Replay, QoS1 duplicates, mesaje vechi | Watermark timestamp + sample_id persistate; ordine strict crescatoare; viitor >30 s respins; date >24 h respinse |
| Stari sintetice prezentate ca masurate | Quality si source la fiecare observatie, source_quality/is_simulated pastrate inclusiv cand devin stale; pachet HA implicit estimated, flexibility declared |
| Furt de credentiale | Criptare Fernet existenta; secrete absente din UI, API, audit, task args si erori; TLS 1.2+ cu validarea certificatului/hostname |
| SSRF / scanare interna | Brokeri MQTT/TLS din configuratia operatorului, niciun URL liber, nicio dezactivare TLS; operatorul controleaza si egress/DNS |
| Stocare excesiva | Fara istoric sau payload brut; stergere valori dupa 24 h; revocare sterge contextul si credentialele din EMS |
| Retry storm / worker duplicat | Backoff 15 s pana la 15 min + jitter, next_attempt persistat, lock PG skip_locked, limite de sesiune |

Maparea explicita nu poate verifica semantic ca un binary_sensor al clientului
este agregat: administratorul confirma acest lucru. Brokerul este o limita de
incredere; platforma nu poate detecta un senzor care minte despre valoare/timp.
Retragerea consimtamantului in EMS nu modifica utilizatorii brokerului extern:
se revoca si contul brokerului si se sterge pachetul HA. Mesajele retained vechi
se curata pe broker; noua configuratie EMS foloseste un namespace nou.

## Surse primare verificate

- [Home Assistant MQTT](https://www.home-assistant.io/integrations/mqtt/): configurare, TLS, publish, retained, MQTT 5.
- [Home Assistant state objects](https://www.home-assistant.io/docs/configuration/state_object/): timestampul `last_reported`.
- [Paho MQTT Python](https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html): callbacks v2, MQTT 5, TLS, ACK, timeouts.
- [HA packages](https://www.home-assistant.io/docs/configuration/packages/): instalarea pachetului generat.
