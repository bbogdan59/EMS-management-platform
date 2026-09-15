# ADR 0002: Consumatori termici flexibili, simulator-first si fail-safe

- Status: propus pentru discovery/pilot; controlul fizic NU este aprobat
- Data: 2026-09-15
- Issue: #52

## Context si decizie

Pompele de caldura si sistemele HVAC au inertie, limite de confort si reguli
anti-short-cycle care nu exista la o sarcina EV simpla. O comanda gresita poate
produce disconfort, uzura, inghet sau pierderea apei calde. Platforma va folosi
un contract canonic provider-agnostic, dar va introduce capabilitati in etape:

1. `recommendation`: explica un program posibil, fara comanda;
2. `shadow`: simuleaza si compara planul cu functionarea reala;
3. `live`: va necesita un ADR ulterior, pilot aprobat si fail-safe local.

Prima integrare este adaptorul intern `simulator`, nu un protocol fizic.
Selectarea unui provider real ramane blocata pana cand documentatia oficiala,
modelele pilot si drepturile de control pot fi verificate. Astfel nu confundam
faptul ca un ecosistem expune telemetrie cu dreptul sau siguranta de a-l comanda.

Contractul initial este in `app/schemas/flexible_load.py`; modelul RC
determinist in `app/services/flexible_load_simulator.py`. Modelele sunt doar
pentru discovery/backtest si nu sunt montate in routerul `/api/v1`.

## Inventar de clase de integrare

| Clasa | Avantaj | Risc/gap obligatoriu inainte de pilot |
|---|---|---|
| API cloud al producatorului | instalare usoara | latenta, quota, consimtamant, token/revocare, lipsa controlului local |
| protocol local standardizat | functionare fara cloud | profile/capabilitati diferite, discovery si securitate de retea |
| Modbus/BACnet proprietar | control/feedback granular | harta de registre/obiecte specifica modelului, write-uri periculoase |
| releu/contact smart-grid | interfata simpla | feedback insuficient; nu confirma temperatura, mod sau putere |
| gateway al instalatorului | poate pastra logica nativa | dependenta comerciala si contract API necunoscut |

Nu se presupune compatibilitatea unui echipament doar din numele protocolului.
Pentru fiecare provider se cer documentatia oficiala versionata, matrice de
modele/firmware, sandbox sau hardware real si readback dupa orice actiune.

## Model canonic

`FlexibleLoadCapabilities` separa strict informatia raportata de autorizare:
moduri, banda/treapta de setpoint, putere nominala, timpi minimi on/off si
feedback disponibil. `FlexibleLoadPolicy` retine opt-in, etapa maxima permisa,
ferestre de confort, quiet hours, plafon de putere si override. Defaultul este
dezactivat + `recommendation`; prezenta capabilitatii nu activeaza controlul.

Intrari minime pentru orice recomandare: forecast meteo cu `issued_at` si
quality, model termic calibrat cu confidence, pret efectiv contractual,
surplus PV, stare proaspata (temperatura/mod/putere), timezone si preferinta
versionata. Lipsa unei intrari nu devine zero si nu permite plan `live`.

## Simulator si baseline

Simulatorul aplica un model termic RC de ordinul intai pe intervale UTC
contigui si timezone-aware. Scenariul contine explicit modul si puterea ceruta;
simulatorul nu creeaza comenzi si nu „repara” silentios valori imposibile.
Puterea peste capabilitate si modurile nesuportate sunt refuzate. Schimbarile
on/off prea rapide sunt raportate ca incalcari, pastrand scenariul pentru audit.

Costul foloseste `Decimal`. Daca pretul lipseste pentru un singur interval,
costul total ramane necunoscut (`None`), nu zero sau suma partiala prezentata
ca exacta. Baseline-ul pilot va fi controlul nativ observat; economia va fi
raportata numai dupa o perioada comparabila si cu interval de incredere.

## API propus (neexpus in acest PR)

- `GET /api/v1/flexible-loads/{id}/capabilities`: capabilitati raportate,
  momentul verificarii si provenance.
- `PUT /api/v1/stations/{id}/flexible-load-policy`: versiune noua de preferinte,
  cu RBAC, CSRF pentru web si audit.
- `POST /api/v1/stations/{id}/flexible-load-simulations`: job idempotent de
  simulare; raspunsul nu este o comanda.
- `GET /api/v1/flexible-loads/{id}/recommendations`: rezultat explicabil cu
  input snapshot si quality.
- Un endpoint de comanda NU este propus pana la ADR-ul de control live.

Orice viitor mesaj de control va necesita `command_id`, versiune, expirare,
idempotency key, expected state, limite locale si readback. API-ul platformei
nu va ocoli controlul nativ al echipamentului la pierderea conectivitatii.

## Threat model si fail-safe

| Amenintare | Control necesar |
|---|---|
| tenant gresit / IDOR | verificare station membership la fiecare acces, teste cross-tenant |
| credential provider furat | criptare, redactare log, scope minim, rotatie si deconectare |
| replay/dublu command | idempotency, expirare, expected state si audit imutabil |
| telemetrie stale/lipsa | blocare live, quality/freshness explicite, revenire la control nativ |
| short cycling | min on/off local, rate limit si readback; serverul nu este singura protectie |
| forecast/model gresit | shadow, confidence, limite hard si override client |
| cloud/platform offline | echipamentul continua programul nativ sigur; nicio dependenta de heartbeat cloud |
| cont compromis | RBAC, reautentificare pentru live/transfer, audit si opt-out imediat |

Datele de ocupare si ferestrele de confort sunt date personale: se colecteaza
minimal, se limiteaza retentia si nu se expun operatorilor fara nevoie/RBAC.

## Criterii pentru primul pilot fizic

- un singur provider si o lista explicita de modele/firmware;
- minimum doua saptamani telemetry-only si doua saptamani shadow;
- temperatura, stare si putere cu freshness masurabila;
- model calibrat cu eroare raportata, nu doar parametri default;
- zero incalcari hard in backtest si test de deconectare/fail-safe;
- override fizic si in UI, opt-out, audit si acord explicit;
- review de instalator pentru anti-inghet, DHW, compresor si garantie;
- canary la putere/interval limitat, cu rollback automat la control nativ.

## Backlog pe provider

1. Template de evaluare: auth, scope, latency, quota, revoke, capabilitati,
   modele/firmware, readback, ToS si mediu de test.
2. Shortlist bazat pe echipamentele clientilor pilot; nu pe popularitate.
3. Adaptor telemetry-only + contract tests cu fixtures sanitizate.
4. Calibrare model si backtest per cladire/sezon.
5. Shadow recommendations cu explicatii confort/cost/PV.
6. ADR separat pentru control live, threat-model actualizat si pilot canary.

## Consecinte

Castigam un limbaj comun si teste deterministe fara a creste suprafata de
control. Costul este ca automatizarea fizica este amanata deliberat; alegerea
providerului si integrarea in optimizer raman issue-uri copil dupa validarea
discovery-ului.
