# Limitari reale si asumtii documentate

Aceasta lista e intentionat onesta: enumera ce NU e verificat/implementat
complet, cu motivul exact, asa cum a cerut specificatia ("nu masca
integrarile lipsa prin date fictive nemarcate").

## 1. Schema CSV-ului OPCOM (verificata ulterior impotriva unui export real)

Mediul in care a fost dezvoltata initial aceasta platforma blocheaza la
nivel de retea accesul catre `opcom.ro` (politica organizationala a
mediului de lucru, confirmata explicit de proxy-ul de iesire:
`EGRESS_BLOCKED`) -- initial nu s-a putut deci descarca un CSV real si
verifica numele exacte ale coloanelor, separatorul sau encoding-ul.

**Actualizare:** utilizatorul a furnizat ulterior un export CSV real
(`rezultatePZU_PT15M_...`, rezolutie 15 minute), care a scos la iveala
diferente reale fata de aproximarea initiala -- corectate in cod, nu doar
documentate:
- Fisierul foloseste **virgula** ca delimitator (nu punct-virgula, cum era
  presupus initial), cu fiecare camp incadrat in ghilimele duble (CSV
  standard RFC4180). Parserul folosea o simpla `line.split(delimiter)`, care
  NU intelege ghilimelele -- orice camp care ar fi continut el insusi
  delimitatorul (ex. un pret cu separator zecimal identic cu delimitatorul)
  s-ar fi rupt gresit. Inlocuit cu `csv.reader`, care gestioneaza corect
  incadrarea in ghilimele.
- Coloana reala de pret se numeste **"Pret de Inchidere a Pietei [lei/MWh]"**,
  nu doar "Pret" -- potrivirea antetului cerea egalitate EXACTA cu un alias
  scurt, deci nu se potrivea niciodata. Schimbata la potrivire pe **subsir**
  (alias continut in numele coloanei), pastrand totusi validarea structurala
  (antetul trebuie sa aiba o coloana de interval SI o coloana de pret pe
  indici DIFERITI -- un tabel sumar anterior in fisier, cu medii
  Base/Peak/Off-Peak, e ignorat automat pentru ca nu are coloana de interval).
- Fisierul are un titlu si un tabel sumar (medii Base/Peak/Off-Peak) INAINTE
  de tabelul detaliat pe intervale -- deja gestionat corect de cautarea
  antetului in primele `header_search_rows` linii nevide.
- `app/services/opcom_schema.py`: delimitatorul implicit a fost schimbat la
  virgula (era punct-virgula) sa reflecte formatul real observat; sniffer-ul
  de delimitator ramane ca fallback daca un format viitor difera.
- Test de regresie nou: `tests/unit/test_opcom_parser.py::test_parses_real_opcom_export_sample`,
  ruland impotriva unei copii neschimbate a exportului real
  (`tests/fixtures/opcom_real_sample_pt15m_2026-09-12.csv`), verificand toate
  cele 96 de intervale si primele/ultimele preturi exact.

**Ramane neverificat** (limitarea de retea originala inca se aplica in acest
mediu sandbox): fetch-ul HTTP live catre `opcom.ro` (`_fetch_raw` in
`app/services/opcom_service.py`) -- headerele de raspuns reale, coduri de
eroare, comportamentul retry-ului contra serverului real. Parserul insusi
(logica de interpretare a CSV-ului, odata continutul primit) e acum verificat
impotriva unui esantion real, nu doar presupus. Fallback-ul cu date sintetice
(`app/services/opcom_fixtures.py`) ramane neschimbat pentru cazul in care
sursa reala e indisponibila.

## 2. Sursa meteo (Open-Meteo) -- acelasi tip de limitare de retea

`api.open-meteo.com` e de asemenea blocat in mediul de dezvoltare. Adaptorul
(`app/services/weather_service.py`) e scris conform documentatiei publice a
Open-Meteo (parametri `hourly=shortwave_radiation,direct_normal_irradiance,...`),
dar nu a putut fi testat impotriva unui raspuns real -- doar impotriva
formei JSON documentate. Erorile de retea sunt gestionate explicit
(`WeatherUnavailableError`), propagate curat pana in UI/optimizator (fara
valori inventate).

## 3. Model PV simplificat (nu e un model de modul/invertor certificat)

`pv_forecast_service.py` foloseste pvlib pentru pozitia solara si
transpunerea iradiantei pe planul panourilor (calcule corecte, din
biblioteca standard a industriei), dar conversia in putere foloseste un
factor de derating fix (`SYSTEM_DERATE = 0.85`) in loc de un model
CEC/SAPM per-modul -- pentru ca platforma nu colecteaza date despre modelul
exact al panourilor/invertorului clientului, doar putere instalata,
orientare si inclinatie. E documentat clar in cod si va supraestima usor
productia in conditii de temperatura ridicata (unde pierderile reale sunt
mai mari decat 15% constant).

## 4. Prognoza de consum: fara sursa separata pentru "consumatori flexibili"

`ConsumptionForecast.flexible_component_kw` ramane intotdeauna 0. Platforma
nu are (inca) o sursa de date care sa separe masina de spalat/boilerul de
restul consumului casnic -- tot ce nu e EV e raportat ca `base_load_kw`.
Optimizatorul functioneaza corect cu aceasta simplificare (nu are nevoie de
componenta flexibila pentru a calcula planul), dar UI-ul nu poate afisa
separat "cat consum e amanabil".

## 5. Arbitraj: prag informativ, fara "pairing" explicit incarcare/descarcare

`PreferenceVersion.arbitrage_min_benefit_lei` e stocat si afisat, dar NU e
inca folosit ca o constrangere explicita in modelul de optimizare (care ar
necesita perechi explicite "incarca la ora X ca sa descarci la ora Y" cu
diferenta de pret peste prag). In schimb, costul de uzura a bateriei
(`DEFAULT_BATTERY_WEAR_COST_LEI_PER_KWH`, valoare implicita rezonabila dar
neconfirmata per client real) si randamentele de incarcare/descarcare deja
descurajeaza arbitrajul cu marja mica in mod natural, prin functia obiectiv.
O implementare completa a pragului explicit ramane de facut.

## 6. Formula de remunerare a exportului -- doar tarif fix sau indexat simplu

Sectiunea 7 din cerinte atrage atentia ca exportul clientului NU e neaparat
remunerat la pretul OPCOM brut. Platforma suporta doar doua forme: tarif fix
sau tarif indexat OPCOM +/- o marja constanta. Daca formula contractuala
reala e mai complexa (componente reglementate variabile, plafoane etc.),
administratorul trebuie sa bifeze `economic_calculation_disabled=True` pe
acea versiune de tarif si sa documenteze limitarea in `limitation_note` --
calculul economic aferent (pret efectiv, economii estimate) se dezactiveaza
explicit in UI in acest caz, nu se aproximeaza silentios.

## 7. Cost de uzura a bateriei -- valoare implicita, nu masurata

`DEFAULT_BATTERY_WEAR_COST_LEI_PER_KWH = 0.05 lei/kWh ciclat` e o
aproximare rezonabila, nu deriva dintr-un cost real de inlocuire a bateriei
clientului (necunoscut platformei). Afecteaza doar echilibrul intern al
optimizarii (cat de mult "descurajeaza" ciclarea bateriei pentru arbitraj
marginal), nu apare ca linie separata in facturi/rapoarte.

## 8. Mediul de dezvoltare nu a permis validarea `docker build`/`docker compose up`

Containerul de dezvoltare in care a fost scrisa aceasta platforma nu are
`dockerd` functional (`Operation not permitted` la pornirea daemon-ului
Docker). Am validat separat, cu instrumentele disponibile:
- sintaxa `docker-compose.yml` cu `docker compose config` (trece);
- sintaxa `docker/entrypoint.sh` cu `sh -n` (trece);
- validitatea JSON a fisierelor `railway*.json`;
- **intreaga aplicatie, end-to-end, direct pe gazda** (PostgreSQL 16 si
  Redis 7 instalate local, nu in container): migratii, toate rutele web,
  API-ul de dispozitive, simulatorul, optimizatorul, importul OPCOM,
  Celery (rulat sincron pentru teste).

Nu am putut rula efectiv `docker build .` in acest mediu. Recomandare
inainte de a considera imaginea "gata de productie": ruleaza
`docker compose build && docker compose up` intr-un mediu cu Docker
functional (orice masina de dezvoltator normala) inainte de primul deploy.

## 9. Deploy Railway -- nu a fost efectuat

Nu am primit acces la un cont/proiect Railway in aceasta sesiune. Am scris
si documentat configuratia (`railway.json` + `railway.worker.json` +
`railway.scheduler.json` + `railway.migrate.json` + `docs/DEPLOY_RAILWAY.md`)
pe baza documentatiei publice Railway (cautata explicit, cu surse citate),
dar deploy-ul propriu-zis si verificarea URL-ului public raman de facut
odata ce sunt disponibile credentialele/proiectul tinta.

## 10. Testare cu date OPCOM/meteo reale

Din cauza limitarilor 1-2 de mai sus, testele automate pentru adaptorul
OPCOM folosesc exclusiv fixture-uri sintetice (generate determinist). Ele
verifica corectitudinea parserului si a logicii de business (unitati,
preturi negative, DST, deduplicare, idempotenta), nu compatibilitatea cu
formatul exact publicat curent de OPCOM.

## 11. E-mail -- doar adaptor "console" testat end-to-end

Adaptorul SMTP (`app/core/email.py`) e implementat conform bibliotecii
standard `smtplib`, dar nu a fost testat impotriva unui server SMTP real
(niciun server SMTP disponibil in mediul de dezvoltare). Adaptorul
`console` (implicit) a fost testat complet -- toate invitatiile/resetarile
de parola functioneaza corect, doar ca scriu in loguri in loc sa trimita
email real.

## 12. Predictia de preturi PZU pana la finalul anului -- metoda simpla, nu econometrica

`app/services/market_analytics_service.get_forecast_to_year_end` foloseste o
metoda "seasonal-naive ajustata cu tendinta recenta": media istorica pe
zi-din-an (din anii anteriori disponibili), inmultita cu raportul dintre
ultimele 30 de zile reale din anul curent si media istorica pentru aceleasi
zile calendaristice. E simpla, transparenta si usor de explicat, dar NU
modeleaza sezonalitate saptamanala, evenimente de piata, schimbari de
capacitate/reglementare sau alti factori structurali. Predictia e afisata
intotdeauna cu linie punctata si eticheta explicita a metodei -- niciodata ca
un fapt cert. Daca nu exista niciun an anterior cu date, se foloseste un
fallback si mai simplu (medie constanta a ultimelor 30 de zile), marcat ca
atare. Backfill-ul istoric (`scripts/backfill_opcom_history.py`) ruland din
2024-01-01 imbunatateste direct calitatea acestei predictii (mai multi ani
de referinta sezoniera).

## 13. Corectii din revizia de cod

Fallback-urile optimizatorului sunt publicate exclusiv in `shadow`, inclusiv
pentru o statie live. Valorile zero din aceste intervale sunt substituenti
de diagnostic, nu instructiuni de descarcare. Dispatcherul revalideaza modul
planului/statiei, starea run-ului, versiunile configuratiei/preferintelor si
suspendarea automatizarii; trimite numai dispozitivului care a acceptat planul.
Comenzile nelivrate ale planurilor invalidate sunt retrase la polling.
Aceasta verificare nu poate retrage fizic o comanda deja executata.

`OPCOM_USE_SYNTHETIC_FIXTURE_ON_FAILURE` este implicit `false`, inclusiv in
Compose. Pornirea in productie cu aceasta optiune activa este respinsa.
Datele sintetice istorice existente nu sunt sterse automat; trebuie izolate
sau inlocuite cu importuri reale inainte de activarea live.

Corectiile nu reprezinta certificarea modului live: validarea capabilitatilor,
provenienta/freshness completa a intrarilor, limitele energetice si bugetele
EFC istorice necesita in continuare lucrarile de follow-up din GitHub.

## 13. Integrarea energetica a telemetriei -- conventii si asumtii documentate (issue #4)

`app/services/aggregation_service.py` integreaza puterea instantanee (W) in
energie (kWh) pe convenția "zero-order hold" (ZOH): valoarea unui esantion se
considera valabila de la momentul lui pana la urmatorul esantion cunoscut,
dar niciodata mai mult de `MAX_GAP_SECONDS` (implicit 300s = 5 minute, un
multiplu generos al intervalului tipic de polling de 20s al
simulatorului/dispozitivelor). Dincolo de acest prag, portiunea ramasa e
NECUNOSCUTA (nu extrapolata) -- reflectata explicit in campul `coverage`
(fractie 0..1, separat pe metrica: pv/load/battery/grid/ev/soc), iar campul
de energie corespunzator devine `NULL` cand acoperirea e zero, nu 0.

Presupunere documentata: `aggregate_day`/`aggregate_month` calculeaza
limitele UTC din miezul de noapte local al statiei (corect pentru zile de
23/25h la schimbarea orei), dar `aggregate_hour` foloseste limite fixe de
ora UTC -- corect doar daca offset-ul fusului orar al statiei e un numar
intreg de ore (adevarat pentru `Europe/Bucharest`, singurul fus folosit
curent). Un fus cu offset de 30/45 minute ar produce ore UTC nealiniate cu
miezul de noapte local si ar necesita o revizuire a acestei presupuneri.

Contractul AC/DC pentru bateria: `battery_power_w` e raportat de dispozitiv
la bornele DC (convenția API-ului de dispozitive), fara nicio conversie
AC/DC suplimentara aplicata la agregare -- randamentele de
incarcare/descarcare se aplica separat, explicit, doar in motorul de
optimizare. EFC-ul (`dashboard_service.get_efc_used`) foloseste explicit
`battery_reference_capacity_kwh` (nameplate) ca numitor, nu capacitatea
disponibila (care poate scadea din degradare).

`run_aggregation_task` (Celery, la fiecare 15 minute) reface automat o
fereastra recenta (`RECENT_REAGGREGATION_LOOKBACK`, implicit 3 ore) la
fiecare rulare, ca sa prinda telemetria usor intarziata fara interventie
manuala. Un backfill pe un interval istoric mai vechi (ex. un dispozitiv
offline cateva zile) necesita un apel explicit al
`aggregation_service.reaggregate_range` (script/consola de administrare --
nu exista inca un declansator din UI pentru asta).

Schimbare de contract semnalata explicit in issue #4 pentru lucrarile in
paralel: cele 7 coloane de energie din `TelemetryAggregate` sunt acum
nullable (vezi migratia `a3f7c9d1e6b2`). Consumatorii care fac
`float(rand.pv_energy_kwh)` direct, fara verificare de `None`, trebuie
actualizati -- interogarile bazate pe `coalesce(sum(...), 0)` raman sigure
neschimbate.

### Upgrade of calendar aggregates
Existing UTC day/month rows are retained as legacy_day/legacy_month and excluded from current rollups. Reaggregate retained lower-resolution history to populate local day/month rows. If raw/hour history expired, legacy values remain archived; do not relabel them as local days. Downgrade archives new local rows as local_day/local_month and restores legacy keys. Repeated upgrade after a downgrade requires reconciling archived local rows before rebuilding; archived periods are not queried by normal dashboards. NULL consumer support is included in this PR; forecasts reject insufficient coverage and treat absent EV as zero only when the station explicitly disables EV.
## 14. Enrollment automat al dispozitivelor (issue #16) -- domeniu si asumtii

- **Alocarea e restransa la `platform_admin`.** Inventarul de device-uri
  neasociate (`/admin/devices/pending`) si actiunea de alocare nu sunt
  expuse administratorilor de organizatie: un `installation_uuid` e doar o
  identitate declarata de dispozitiv, fara nicio afiliere de organizatie in
  acel moment, iar expunerea globala a inventarului catre orice admin de
  organizatie ar permite unei organizatii sa vada/revendice un device
  destinat altei organizatii. Corelarea "acest installation_uuid e al
  clientului X" ramane, ca si la codul de asociere clasic, o comunicare
  in afara platformei (instalator -> administrator).
- **Fara sweep automat de expirare.** Un enrollment expirat (implicit 72h)
  ramane in tabela `devices` cu `enrollment_expires_at` in trecut -- e
  filtrat/marcat explicit in UI si respins explicit la alocare, dar nu
  exista inca un task periodic care sa-l revoce/curete automat. Un operator
  poate revoca manual din UI. Un task Celery dedicat ramane de adaugat.
- **Fereastra scurta de expunere a `pending_credential_secret`.** Intre
  momentul alocarii si prima cerere autentificata reusita a dispozitivului
  cu noua credentiala, secretul e pastrat in clar (nu doar hash-uit) in
  `devices.pending_credential_secret`, EXPLICIT ca sa poata fi recuperat
  idempotent daca raspunsul de alocare se pierde in retea. Fereastra se
  inchide automat la prima autentificare reusita. Un compromis al bazei de
  date exact in acest interval ar expune acea credentiala in clar -- acceptat
  deliberat, documentat, nu ascuns.
- **Fara sesiune criptografica de tip challenge-response.** Dovada de
  posesie e un secret static transmis o data (ca parola), verificat prin
  hash Argon2 la fiecare reincercare -- nu o schema cu chei asimetrice/HMAC
  per-cerere. E suficient pentru amenintarea principala vizata (impiedicarea
  insusirii unui `installation_uuid` cunoscut), dar nu protejeaza impotriva
  unui atacator care a interceptat deja `provisioning_secret`-ul o data
  (ex. la primul enrollment, printr-un TLS compromis) -- acelasi model de
  amenintare ca `DeviceCredential` existent.
- **Agentul real (`EMS-device-code`) nu implementeaza inca acest flux.**
  v0.1 al agentului foloseste exclusiv codul de asociere clasic (sectiunea 1
  din docs/API.md); enrollment-ul automat descris aici e contractul
  SERVER-SIDE pe care viitorul `EMS-device-code#3` il va consuma. Nu am
  putut deci valida acest API impotriva unui agent real -- doar impotriva
  testelor de integrare proprii (Postgres real, inclusiv un test de cursa
  concurenta reala la nivel de baza de date) si a exemplelor din docs/API.md.
- **Suprapunere de fisiere cu issue #10.** Issue-ul #16 a necesitat atingeri
  minime, aditive, in fisiere nominal detinute de #10
  (`app/api/v1/router.py` -- o linie de inregistrare a noului router;
  `app/api/v1/device_deps.py` -- stergerea `pending_credential_secret` la
  prima autentificare reusita). Fluxul clasic cu cod de asociere
  (`app/api/v1/devices.py::claim_device`, `device_service.claim_device`) nu
  a fost atins.

## 15. Protocol web-device: atomicitate, idempotenta si limita reala a corpului (issue #10)

Domeniul strict al acestei lucrari: `app/api/v1/` (in special `device_deps.py`),
`device_service.py` (claim/ack/rezultat), `command_dispatch_service.py`
(dispatch). UI-ul claim/invitatii detinut de #6 nu a fost atins.

- **Capabilitatile raportate NU sunt folosite ca autorizare pentru comenzile
  de baza (`set_battery_target_soc`, `hold_battery` etc.).** Verificat
  explicit inainte de a adauga vreo poarta noua: docs/API.md documenteaza
  deja aceasta decizie deliberata ("capabilities sunt informatii RAPORTATE
  de dispozitiv... nu folosite implicit ca autorizare"). Singurul tip de
  comanda unde capabilitatile chiar autorizeaza emiterea/livrarea e
  `apply_inverter_settings` (issue #17,
  `inverter_config_service.supports_write`), neschimbat aici. Nu am adaugat
  o poarta similara pentru comenzile de baza -- ar fi contrazis explicit
  contractul documentat existent, fara sa fi fost ceruta o schimbare de
  contract.
- **Validarea de valori finite pentru telemetrie exista deja, verificat, nu
  adaugat de aceasta lucrare.** Pydantic v2 respinge implicit NaN/Infinity
  atat pentru `Decimal` cat si pentru `float` (`finite_number`); confirmat
  printr-un test direct de validare inainte de a atinge schema, ca sa nu
  adaugam o constrangere deja existenta crezand-o lipsa.
- **Limita reala a corpului HTTP citeste bytes-ii efectivi din stream**, nu
  doar header-ul `Content-Length` (care poate fi omis sau minte). Header-ul
  ramane o respingere rapida cand e prezent si valid; un header malformat
  (`Content-Length: abc`) intoarce acum 400, nu un 500 brut in `int()`.
- **Doua bug-uri reale de concurenta/persistenta gasite si corectate**, fiecare
  cu test de regresie pe PostgreSQL real (nu mock): claim-ul unui cod nu
  serializa consumarea lui (`SELECT ... FOR UPDATE` adaugat); expirarea unei
  comenzi descoperite chiar in `acknowledge_command` se pierdea la
  `db.rollback()`-ul facut de ruta apelanta la exceptie (`db.commit()`
  explicit inainte de a ridica eroarea, ca sa supravietuiasca indiferent de
  ce face apelantul).
- **ACK/rezultat sunt acum idempotente la reincercare identica** (acelasi
  status + acelasi payload dupa un raspuns pierdut in retea intoarce acelasi
  succes, fara sa duplice evenimentul de audit), dar un rezultat
  CONTRADICTORIU pentru o comanda deja finalizata e respins explicit (409),
  niciodata suprascris tacit.
- **Dispatch-ul izoleaza fiecare incercare de creare a unei comenzi intr-un
  SAVEPOINT dedicat.** Constrangerea unica `(device_id, idempotency_key)`
  exista deja in schema si preveneste duplicatele reale la nivel de baza de
  date; problema corectata aici e ca, fara acest SAVEPOINT, o coliziune la
  o singura statie ar fi invalidat `flush()`-ul intregului lot si ar fi
  anulat dispatch-ul pentru TOATE celelalte statii live procesate in aceeasi
  rulare -- nu doar pentru cea aflata in cursa.
- **Doua teste pre-existente, negasite legate de acest issue, esueaza deja pe
  `main` inainte de aceasta lucrare** (verificat explicit prin `git stash` /
  checkout curat, nu presupus): `test_org_isolation.py::test_operator_cannot_manage_station_config_but_can_view`
  (bug RBAC real in `app/web/routes/stations.py`, in domeniul issue-ului #8,
  nu #10 -- il abordez separat, in acel issue) si
  `test_aggregation_review.py::test_nullable_flow_and_simulated_carry_in`
  (comportament EV in `consumption_forecast_service`, posibil sensibil la
  ceasul real, in domeniul altui issue). Niciunul nu a fost modificat aici,
  ca sa nu depasesc granitele acestui issue; raportate explicit, nu ascunse.
## 15. Validare configurare statie, preferinte si creare statie (issue #8)

Domeniul strict: `app/web/routes/stations.py` (config/preferinte/tarife),
doar functia `create_station` din `app/web/routes/organizations.py`,
`app/schemas/station_forms.py` (nou), template-urile acestor formulare.
Invitatiile/claim/SSE (#6) si solver-ul nu au fost atinse.

- **Validare stricta reala, nu doar cosmetica.** Toate campurile numerice
  trec acum prin scheme Pydantic dedicate (`app/schemas/station_forms.py`)
  inainte sa atinga baza de date: valori nefinite (NaN/Infinity trimise ca
  text brut de formular, ocolind constrangerile `type=number` ale
  browser-ului) sunt respinse explicit, `pv_installed_power_kw`/
  `inverter_power_kw` trebuie sa fie strict pozitive (zero nu mai e acceptat
  tacit), randamentele bateriei sunt constranse la `(0, 1]`, iar SOC
  minim/maxim la `[0, 100]` cu `min <= max` validat explicit. Anterior,
  `_dec()` inlocuia orice input invalid cu o valoare implicita (adesea 0)
  fara sa anunte utilizatorul -- acum orice esec de validare respinge
  INTREAGA cerere, fara nicio versiune noua creata (verificat exact prin
  teste care compara numarul de versiuni inainte/dupa).
- **Grupuri PV multiple, editabile, fara pierdere de date.** Salvarea
  configuratiei reconstruia anterior necondiționat un singur grup PV hardcodat
  ("Grup principal", azimut 180, inclinatie 30), indiferent cate exista deja.
  Acum orice numar de grupuri (cu nume/putere/azimut/inclinatie proprii) e
  trimis ca JSON validat (`panel_groups_json`, populat dintr-un editor simplu
  in JS vanilla care adauga/sterge randuri) si persistat integral la fiecare
  versiune noua.
- **Coordonatele statiei se colecteaza la creare.** `create_station` seta
  anterior necondiționat `latitude=None, longitude=None` -- prognoza PV
  (issue #8 dependent de #7/#5 anterior) nu putea functiona fara ele pentru
  o statie noua pana la o editare manuala ulterioara, niciodata ceruta
  explicit. Acum sunt campuri obligatorii, validate in intervalul geografic
  valid ([-90,90]/[-180,180]).
- **Flux complet in UI pentru tinte SOC si suspendarea automatizarii.**
  `PreferenceVersion.soc_targets`/`automation_suspended_until` existau in
  model si erau deja CITITE de `optimization_service`
  (`_resolve_soc_targets`) si `command_dispatch_service`
  (`plan_allows_dispatch`), dar formularul de preferinte nu avea niciun
  camp pentru ele -- un admin nu putea seta niciodata o tinta SOC sau
  suspenda automatizarea live din UI. Acum ambele au flux complet: tinte
  SOC recurente (ora locala HH:MM, procent, zile optionale ale saptamanii,
  validate inclusiv peste miezul noptii -- 23:45/00:15 testate explicit) si
  suspendare pana la o data/ora locala (convertita corect in UTC prin fusul
  orar al statiei, verificat cu un test explicit de conversie).
- **Concurenta optimista pe versiuni.** Formularele de configurare/preferinte
  poarta acum un camp ascuns `expected_version` (versiunea vazuta la
  incarcarea paginii). O trimitere cu o versiune invechita (alt editor a
  publicat intre timp) e respinsa explicit, cu mesaj clar, fara sa
  suprascrie tacit modificarea celuilalt. Constrangerea unica existenta
  `(station_id, version)` ramane ca ultim garant la nivel de baza de date
  pentru o cursa reala simultana (`IntegrityError` prins si transformat in
  acelasi mesaj clar, nu un 500 brut) -- verificat cu un test real de
  concurenta pe doua sesiuni/thread-uri separate (fixtura `engine`, nu `db`).
- **XSS stocat prevenit explicit la embedarea JSON in `<script>`.** Numele
  unui grup PV sau o tinta SOC salvata anterior sunt reafisate ca JSON
  direct intr-un bloc `<script>` (pentru editorul JS) folosind `| safe` in
  Jinja -- `json.dumps()` nu escapeaza `<`/`>`/`&`, deci un nume de grup
  continand literal `</script><script>...` ar fi putut rupe blocul si
  executa cod arbitrar la urmatoarea randare a paginii. Corectat cu o
  escapare explicita (`_safe_script_json`) inainte de orice inserare `| safe`.
- **Bug real de infrastructura de testare gasit si corectat, nu doar
  ocolit.** Un test RBAC pre-existent
  (`test_org_isolation.py::test_operator_cannot_manage_station_config_but_can_view`)
  esua intermitent in suita completa, NU izolat -- am investigat exhaustiv
  (nu doar presupus "flaky") si am gasit cauza reala: rate limiting de login
  e cheiat per IP client (`app/web/routes/auth.py`), iar `TestClient`
  raporteaza mereu acelasi IP fals (`"testclient"`) pentru toate cererile;
  Redis (spre deosebire de baza de date) nu e golit intre teste individuale
  in cadrul unei rulari, deci contorul se acumuleaza pe TOATA sesiunea de
  testare -- dupa exact limita implicita (10) de apeluri `login()` cumulate
  din ORICE combinatie de teste, urmatoarele autentificari esueaza tacit cu
  429 (fara cookie de sesiune), iar `TestClient` urmeaza automat
  redirect-ul 401-catre-/login pana la un 200 aparent nevinovat -- usor de
  confundat cu un bug real de autorizare (exact ce am crezut initial).
  Reprodus determinist prin instrumentare directa (`role_at_least` niciodata
  apelat, contor Redis exact la limita, cookie de sesiune absent) inainte de
  a scrie orice fix. Corectat in `tests/conftest.py` (limita ridicata generos
  doar pentru mediul de test, prin variabila de mediu deja citita de
  `Settings`) -- nu prin golirea Redis intre teste, care ar fi sters si
  starea pe care alte teste de rate-limiting ar vrea sa o verifice explicit.
  Testul original devine acum determinist (verificat cu 3 rulari complete
  succesive ale intregii suite, fara nicio recurenta a esecului), si am
  adaugat teste RBAC proprii, deterministe, pentru criteriile acestui issue.
- **Atins minim, in afara domeniului nominal, strict necesar:**
  `tests/e2e/test_ui_flows.py` (adaugat completarea campurilor
  latitudine/longitudine acum obligatorii in formularul Playwright de creare
  statie -- fara aceasta modificare testul e2e existent ar fi esuat, intrucat
  browserul blocheaza trimiterea formularului cu campuri `required` goale) si
  `tests/conftest.py` (fix-ul de rate-limiting de mai sus, infrastructura
  comuna de testare, nu specifica niciunui issue).
- **Nu acopera:** un editor JS complet drag-and-drop/vizual pentru grupurile
  PV sau tintele SOC -- editorul implementat e functional (adauga/sterge
  randuri, validare server-side completa, fara pierdere de date la editare),
  dar ramane un tabel HTML simplu cu JS vanilla, nu o interfata avansata.

## 15. Pregatirea inputurilor de optimizare -- prospetime, proveniență si concurenta (issue #9)

Domeniul strict al acestei lucrari: `optimization_service.py` -- pregatirea
inputurilor (`_current_soc_kwh`, `_build_pv_series`, `_build_load_series`,
`_fill_gaps`, `_fill_price_gaps`, `_ensure_forecasts`), orchestrarea
tranzactiei (`run_optimization_for_station`, `_run_locked`) si publicarea
(`_publish_plan`). Modelul matematic din `_solve` (constrangerile fizice,
formularea Pyomo) NU a fost atins -- ramane in sarcina issue-ului #12.

- **SOC de pornire are acum prospetime si proveniență explicite.** O
  telemetrie SOC mai veche decat `optimization_soc_max_age_minutes`
  (implicit 10 min, configurabil) sau absenta totala blocheaza un plan
  **LIVE** (`_fallback` cu motiv explicit) -- nu mai e inlocuita tacit cu o
  presupunere de 50%. Un plan **shadow** ramane calculabil chiar cu SOC
  invechit/lipsa, pentru vizibilitate, dar calitatea (`"measured"` /
  `"stale"` / `"missing"`) e vizibila explicit in `OptimizationRun.input_snapshot["soc"]`,
  niciodata ascunsa/tratata ca masuratoare reala.
- **Golurile de pret nu mai imprumuta o valoare arbitrara din alta parte a
  orizontului.** `_fill_price_gaps` propaga din cel mai apropiat interval
  cunoscut in timp (inainte, apoi -- pentru golul initial -- inapoi), nu
  dintr-o valoare oarecare gasita oriunde in orizont. Fiecare interval e
  marcat explicit `"real"` sau `"estimated"` in
  `input_snapshot["price_buy_quality"]`. Un plan care ar fi altfel LIVE dar
  contine cel putin un interval de pret `"estimated"` e retrogradat automat
  la shadow (`shadow_downgrade_reason`, vizibil in `explanation_summary`),
  in loc sa fie publicat live pe baza unei estimari.
- **Fixture-urile sintetice de piata (import demo/diagnostic,
  `ImportRun.is_synthetic_fixture=True`) nu mai pot alimenta un pret folosit
  de optimizator**, nici macar cand sunt singura sursa "disponibila" pentru
  un interval -- sunt tratate identic cu absenta datelor (deci completate
  prin extrapolare temporala si marcate `"estimated"`, cu efectul de
  retrogradare la shadow de mai sus).
- **Aliniere prognoza de consum <-> grila de optimizare.** `_ensure_forecasts`
  primeste acum granitele orizontului deja aliniate la grila UTC a
  optimizarii (`start`/`end`, calculate o singura data in `_run_locked`), nu
  `utcnow()` brut. Anterior, `generate_consumption_forecast` genera intervale
  incepand de la un moment nealiniat la sfertul de ora, deci
  `_build_load_series` nu gasea niciodata o potrivire exacta si intregul
  consum cadea pe valoarea implicita de fallback -- reprodus si acoperit
  explicit de `test_consumption_forecast_alignment_matches_optimization_grid`.
- **`input_snapshot` e acum suficient pentru replay**, nu doar contoare de
  acoperire: contine granitele orizontului, fusul orar al statiei, SOC-ul
  folosit cu proveniența lui, seriile complete PV/consum/pret (cu calitate
  per interval pentru pret) -- toate cheiate ca timestamp UTC ISO 8601.
- **Versionarea planurilor e monotona pe toate planurile statiei, indiferent
  de status.** Anterior, cautarea "ultimului plan" se limita la statusurile
  active (`published`/`accepted_by_device`/`executing`); un plan ajuns
  `completed` (executie incheiata) devenea invizibil acelei cautari, iar
  urmatoarea optimizare reincepea numerotarea de la 1 -- coliziune garantata
  cu constrangerea unica `(station_id, version)`. Numerotarea foloseste acum
  `MAX(version)` pe toate planurile statiei; cautarea planului activ de
  inlocuit (superseded) ramane separata si neschimbata.
- **Serializarea ramane activa pana la commit fara ca serviciul sa comita
  tranzactia apelantului.** Lock-ul Redis evita lucrul concurent obisnuit, iar
  un advisory lock PostgreSQL transaction-scoped pe ID-ul statiei ramane activ
  pana la commit/rollback-ul detinut de ruta sau worker. Astfel urmatoarea
  rulare vede versiunea deja publicata, iar auditul si planul pot ramane in
  aceeasi tranzactie. Acoperit de un test real cu doua thread-uri/sesiuni
  separate pe `engine`-ul de test.
- **Un refresh best-effort esuat (meteo/PV/consum) nu mai poate lasa sesiunea
  SQLAlchemy inutilizabila.** Fiecare incercare din `_ensure_forecasts` ruleaza
  acum intr-un SAVEPOINT dedicat (`db.begin_nested()`); o exceptie in timpul
  unui flush anterior invalida intreaga tranzactie pana la un rollback
  complet, ceea ce ar fi sters si `OptimizationRun`-ul deja adaugat de
  apelant in aceeasi sesiune necomisa.
- **Neatins deliberat:** `_solve` (modelul Pyomo), API-ul device si
  formularele de configurare a statiei/preferintelor -- conform delimitarii
  issue-ului.
- **Nu acopera:** un job de reconciliere real care sa marcheze planurile
  drept `completed` pe baza telemetriei observate (folosit doar simulat in
  testul de versionare, prin setarea manuala a statusului) -- ramane in
  sarcina altui issue de operare/reconciliere.

## 15. Joburi admin asincrone, CI si deploy verificabil (issue #11)

**Joburi admin asincrone.** `POST /admin/operations/import-opcom` si
`POST /admin/operations/optimize/{station_id}` rulau anterior sincron, in
firul cererii HTTP -- un import OPCOM sau o (re)optimizare putea tine
cererea blocata cat dura efectiv operatia (posibil zeci de secunde pentru
solver). Acum ambele rute doar creeaza un rand `AdminJob` (status=`queued`),
il comit si trimit un task Celery (`admin_opcom_import_job_task` /
`admin_optimize_station_job_task`), apoi redirecteaza imediat. Panoul
`/admin/operations` afiseaza lista de joburi (tip, tinta, status, legatura
catre `ImportRun`/`OptimizationRun` rezultat sau eroarea, daca a esuat).
Fiecare ruta respinge o declansare duplicata pentru aceeasi tinta (aceeasi
data de livrare / aceeasi statie) cat timp exista deja un job `queued` sau
`running`. Modelul `AdminJob` e distinct de `ImportRun`/`OptimizationRun`
(acelea raman inregistrarea de business a rezultatului) si NU e folosit de
rularile planificate (Celery beat) -- acelea nu au un declansator uman de
urmarit si isi gestioneaza deja propria idempotenta/lock-uri.

**CI: build imagine + smoke test.** A fost adaugat un al doilea job in
`.github/workflows/ci.yml` (`docker-build-and-smoke-test`) care ruleaza
`docker build .`, porneste containerul cu rolul `web` (cu Postgres 16 si
Redis 7 ca service-uri, `RUN_MIGRATIONS_ON_START=true`) si asteapta un
raspuns 200 la `/health` (pana la 60s), afisand log-urile containerului la
esec sau intotdeauna pentru diagnostic. **Nu am putut rula acest job local**
-- acelasi motiv ca la limitarea 8 (`dockerd` nefunctional in acest mediu
sandbox: `Operation not permitted` la pornirea daemon-ului). Am validat doar
sintaxa YAML (parsare cu `yaml.safe_load`) si am revizuit manual, cu atentie,
fiecare pas fata de `Dockerfile`/`docker/entrypoint.sh` existente
(`ENTRYPOINT ["/entrypoint.sh"]`, rolul `web` accepta migratii la pornire
prin `RUN_MIGRATIONS_ON_START`). Corectitudinea job-ului ramane de confirmat
la prima rulare reala in GitHub Actions.

**Dezvaluire de detalii interne catre apelanti neautentificati.** Am gasit
si corectat doua puncte in care un eșec de conectare la Postgres/Redis
ajungea, cu detaliul brut al exceptiei, fie intr-un raspuns HTTP
neautentificat, fie repetat in log-urile persistente ale containerului:
- `GET /readiness` (`app/main.py`) intorcea anterior
  `{"status": "error", "detail": str(exc)}` oricui, neautentificat -- acum
  intoarce un mesaj generic (`"Serviciul nu este pregatit."`), iar logul
  server-side pastreaza doar tipul exceptiei si evenimentul structurat.
- `docker/entrypoint.sh` (`wait_for_postgres`/`wait_for_redis`) tiparea
  `str(exc)` la FIECARE din cele pana la 30 de reincercari (la fiecare
  pornire de container) -- acum tipareste doar `type(exc).__name__` la
  fiecare incercare si nu mai include detaliul brut nici in mesajul final.

Am verificat empiric (conexiune reala, cu parola gresita, la Postgres local)
ca `str(exc)` pentru o eroare de autentificare psycopg contine
host/port/utilizator, dar **NU contine parola insasi** -- deci descrierea
corecta a acestei probleme e "dezvaluire de detalii de infrastructura
interna", nu "scurgere de parola". Ambele corectii raman utile indiferent de
continutul exact al mesajului: un apelant neautentificat sau un log
persistent nu ar trebui sa afle niciodata topologia interna (host intern,
nume de utilizator de baza de date).

**Strategia de testare pentru taskurile Celery.** Niciun test existent nu
exercita anterior stratul Celery. Taskurile noi (`admin_opcom_import_job_task`
/ `admin_optimize_station_job_task`) folosesc intern `session_scope()` --
o sesiune SQLAlchemy noua, pe o conexiune reala separata -- ceea ce nu e
vizibil din fixture-ul obisnuit de test `db` (izolat printr-un SAVEPOINT pe
o singura conexiune, niciodata comis efectiv la nivel Postgres). Din acest
motiv NU am activat `task_always_eager` global (ar fi produs esecuri
"job_not_found" greu de diagnosticat, din cauza acestei neconcordante de
izolare):
- Taskurile insele sunt testate direct (apel Python direct, nu `.delay()`)
  in `tests/integration/test_admin_job_tasks.py`, cu date de test comise
  REAL prin fixture-ul `engine` (acelasi tipar folosit deja de testele de
  cursa concurenta din `test_device_enrollment.py`), inclusiv un test pentru
  `skipped_locked` (lock Redis pre-achizitionat manual), redelivery
  idempotent si mesaj generic sigur la esec.
- Rutele HTTP (`trigger_opcom_import`/`trigger_optimization`) sunt testate
  separat in `tests/integration/test_admin_operations_routes.py`, cu
  `.delay()` inlocuit printr-un stub (fixture-ul `db`/`client` obisnuit,
  izolat prin SAVEPOINT) -- se testeaza doar crearea randului `AdminJob` si
  protectia la declansare duplicata, nu executia reala a taskului.

Contorul de rate-limit folosit de testele HTTP este resetat explicit in
fisierul de teste al rutelor admin. Nu este ridicata global limita din
productie, astfel incat testele de securitate continua sa exercite aceleasi
valori implicite ca aplicatia.

## 16. Solver: buget baterie/EFC, limite fizice, tinte SOC, EV (issue #12)

**SOC initial recuperat, nu "teleportat" artificial in banda.** Anterior,
`current_soc_kwh` masurat era clamp-at direct in banda de preferinta
(`min_reserve_soc_percent`/`max_normal_soc_percent`) inainte de a intra in
model -- daca bateria era real la 5% si rezerva minima era 15%, planul
"pretindea" ca porneste de la 15%, inventand energie care nu exista fizic.
Acum `current_soc_kwh` e clamp-at doar la limitele FIZICE (0, capacitate
disponibila), iar banda de preferinta devine o tinta soft, puternic
penalizata (`RESERVE_BAND_PENALTY_LEI_PER_KWH = 50 lei/kWh`, mult peste orice
semnal de pret sau uzura): solverul recupereaza spre banda cat de repede
permite fizica bateriei, fara sa fabrice sau sa stearga energie si fara sa
faca planul infezabil doar pentru ca starea reala e in afara benzii dorite.
Documentat explicit si in docstring-ul `PreferenceVersion` (nu mai descrie
banda ca fiind strict hard in toate situatiile).

**EV: eliminata dubla numarare, respectata starea conectat/deconectat.**
`_build_load_series` insuma anterior `ConsumptionForecast.ev_component_kw`
(o medie istorica pasiva) in consumul dat optimizatorului, desi optimizatorul
are propria variabila de decizie (`m.ev_charge`) pentru incarcarea EV --
aceeasi energie EV risca sa fie numarata de doua ori in bilant. Componenta EV
din prognoza ramane folosita DOAR pentru compararea prognoza-vs-real din
dashboard, niciodata ca input de consum al optimizatorului. In plus, EV-ul nu
mai poate fi "incarcat" de plan daca ultima telemetrie cunoscuta (indiferent
de vechime) arata explicit `ev_connected=False`; absenta oricarei informatii
(`None`) lasa comportamentul neschimbat.

**EFC zilnic/lunar scade utilizarea deja realizata, pe calendar LOCAL.**
Bugetul EFC ramas pentru fiecare zi/luna calendaristica LOCALA (statia
poate fi in orice fus orar) e acum `buget_nominal - deja_folosit`, unde
`deja_folosit` vine din `get_efc_used` pe fereastra reala [inceput-zi/luna
locala, inceput-orizont). Anterior, bugetul zilnic era intotdeauna cel
nominal complet (ignora ce s-a descarcat deja azi), iar granita lunara
folosea `horizon[0].replace(day=1)` in UTC -- gresit langa miezul noptii,
unde ora locala si UTC pot cadea in luni calendaristice diferite (ex.
00:15 la Bucuresti pe 1 ianuarie e inca 22:15 UTC pe 31 decembrie).

**Curtailment PV si limita comuna a invertorului.** A fost adaugata o
variabila noua `m.pv_curtailed` (marginita intre 0 si productia PV prezisa)
si o constrangere noua `inverter_limit_rule`: `(PV - curtailed) + descarcare
baterie <= StationConfigVersion.inverter_power_kw` pentru fiecare interval --
anterior nu exista nicio limita comuna intre PV si descarcarea bateriei pe
partea AC, desi ambele trec fizic prin acelasi invertor.

**Tinta SOC aplicata la momentul corect.** O tinta la ora X se aplica acum
starii EXISTENTE la ora X (`m.soc[idx-1]`, sfarsitul intervalului anterior),
nu sfarsitului intervalului care INCEPE la ora X (`m.soc[idx]`, fostul
comportament, care aplica tinta cu un interval intreg -- 15 minute implicit
-- mai tarziu decat ora ceruta).

**Configuratie SOC min >= max e semnalata explicit, inainte de solver.**
`_run_locked` verifica acum acest caz (o eroare de configurare, nu o
problema de rezolvat de solver) si publica direct un plan de fallback cu
status `infeasible`, pastrand comportamentul testului existent pentru acest
caz. Infezabilitatea REALA a solverului (ex. consum peste toate sursele de
putere disponibile) ramane testata separat, cu un scenariu independent de
banda SOC.

**Ramane in afara scopului acestui PR (deja documentat, nu ascuns):**
pragul minim de beneficiu de arbitraj (`arbitrage_min_benefit_lei`) inca nu
e o constrangere explicita in model -- vezi limitarea 5 de mai sus, neschimbata.
`max_optimization_energy_kwh` (energia gestionabila maxima autorizata de
client per orizont) ramane de asemenea neaplicat explicit ca o constrangere
proprie -- comportamentul actual e guvernat in continuare doar de limitele
fizice (putere baterie/retea/invertor) si de bugetele EFC, nu de un plafon
energetic global per rulare.

**Testare.** Toate cele 6 corectii de mai sus au teste noi in
`tests/unit/test_optimization.py`, rulate cu solverul HiGHS real (nu mock-uri):
recuperare SOC din afara benzii fara infezabilitate/fabricare de energie,
eliminarea dublei numarari EV, buget EFC zilnic care scade utilizarea deja
realizata, buget EFC lunar pe granita locala (testat explicit langa o
schimbare de an/luna UTC-vs-local), tinta SOC aplicata la intervalul corect
(nu cu unul mai tarziu) si curtailment PV cu limita comuna a invertorului
respectata in fiecare interval. A fost adaugat si un test de infezabilitate
REALA a solverului (fara PV, fara import de retea, consum peste puterea
maxima de descarcare), distinct de vechiul test care exercita acum
pre-verificarea de configurare (banda SOC min >= max).

## 17. Backoffice admin: lifecycle organizatie, suspendare/arhivare, arhivare statie (issue #24)

**Ce s-a implementat.** `Organization` primeste un camp `status`
(active/suspended/archived), cu metadate ale ULTIMEI tranzitii de
suspendare/arhivare (motiv, actor, timestamp) pastrate ca istoric --
tranzitiile valide sunt aplicate strict prin `organization_service.py`
(niciodata direct pe model), fiecare serializata printr-un advisory lock
PostgreSQL transaction-scoped (acelasi tipar ca `optimization_service`) si
insotita de o intrare `AuditLog`. O pagina noua de backoffice
(`/admin/organizations/{id}`, distincta de `/organizations/{id}` --
autoservirea managerului de client) arata profilul, membrii, statiile (cu
numar de device-uri, alerte active si ultima telemetrie), audit recent, si
controalele de suspendare/reactivare/arhivare/restaurare.

**Efectul suspendarii/arhivarii e aplicat la mai multe niveluri, nu doar in
UI:**
- Sesiunile web active ale TUTUROR membrilor organizatiei sunt revocate
  imediat (`auth_service.revoke_all_sessions_for_users_in_organization`) --
  la fel ca revocarea individuala deja existenta (`revoke_all_sessions_for_user`).
- `app/api/deps.py` (`OrganizationAccess`/`StationAccess`, verificate pe
  FIECARE ruta protejata, nu doar ascunse in UI): o organizatie `suspended`
  blocheaza actiunile de scriere (rol peste `viewer`) ale membrilor
  non-platform_admin, dar pastreaza accesul de CITIRE -- clientul isi poate
  in continuare vedea/exporta datele inainte de reactivare. O organizatie
  `archived` blocheaza TOT accesul non-platform_admin, inclusiv citirea.
  platform_admin nu e afectat de niciuna dintre stari (trebuie sa poata
  gestiona/reactiva organizatia).
- `command_dispatch_service.plan_allows_dispatch`/`dispatch_due_commands`:
  nicio comanda live nu mai e dispecerizata catre statiile unei organizatii
  care nu e `active`, indiferent de starea proprie (`is_active`,
  `execution_mode`) a statiei -- verificat la momentul dispecerizarii, nu
  doar la publicarea planului (acelasi principiu ca celelalte re-verificari
  deja existente in aceasta functie).

**Arhivarea statiei e nedistructiva.** `POST /admin/stations/{id}/archive`
seteaza doar `Station.is_active=False` (camp deja existent, deja verificat
la dispecerizare) -- nicio telemetrie, plan sau device nu e sters ori
modificat, reversibil prin `/restore`.

**Hard-delete e deliberat INDISPONIBIL in aceasta prima versiune** -- issue-ul
insusi cere explicit acest lucru ("preferabil indisponibil in prima
versiune"). Arhivarea ramane starea cea mai "finala" disponibila din UI, si
e recuperabila prin restaurare. O implementare viitoare de hard-delete real
ar necesita o politica de retentie explicita (cat timp se pastreaza
telemetria/facturarea dupa arhivare, export obligatoriu inainte de stergere
etc.), netratata aici.

**Procedura de offboarding recomandata (documentatie operationala ceruta de
issue):**
1. Suspendare (`reason` obligatoriu) -- efect imediat, reversibil. Clientul
   pastreaza acces de CITIRE/export (rol `viewer`) cat timp doar suspendat.
2. Clientul (sau administratorul, in numele lui) exporta orice date
   necesare (export CSV existent pe dashboard-ul statiei/pagina de piata)
   inainte de pasul urmator -- arhivarea blocheaza inclusiv citirea.
3. Arhivare (`reason` obligatoriu) -- stare finala din UI, dar recuperabila
   prin `/restore` de catre platform_admin oricand ulterior (nicio limita
   de timp impusa in cod).
4. Hard-delete real (stergere ireversibila din baza de date) ramane un pas
   MANUAL, in afara aplicatiei, in aceasta prima versiune -- nu exista
   niciun buton/ruta care sa il declanseze.

**Ramas explicit in afara scopului acestei PR (nu ascuns):**
- Gestiunea completa a membership-urilor (adaugare/eliminare/schimbare rol
  din backoffice) -- ramane doar prin fluxul de invitatii existent
  (`/organizations/{id}` autoservire); vezi issue #23 (RBAC delegat), care
  acopera explicit acest domeniu.
- Creare/editare completa a statiei din backoffice-ul admin -- UI-ul de
  autoservire (`/organizations/{id}/stations`, `/stations/{id}/config`)
  acopera deja aceasta nevoie pentru managerul clientului; backoffice-ul
  admin adauga doar arhivare/restaurare (nedistructiva), nu re-implementeaza
  formularele de creare/configurare tehnica.
- Cautare/paginare avansata in lista de organizatii (`/admin/organizations`
  ramane o lista simpla, ca inainte) -- volum mic asteptat in aceasta faza.
## 17. Import OPCOM: rezolutie variabila (15/30/60 minute) si agregare orara pentru grafice mari (issue #35)

**Bug real, nu doar teoretic: parserul presupunea orbeste 15 minute pentru
ORICE zi.** `opcom_service.parse_csv` avea divizorul `900` (secunde) si
pasul `timedelta(minutes=15*(i-1))` hardcodate, desi CSV-ul real OPCOM
contine deja o coloana explicita "Rezolutie" (valori ISO-8601: `PT15M`,
`PT30M`, `PT60M` -- confirmata in `tests/fixtures/opcom_real_sample_pt15m_2026-09-12.csv`)
niciodata mapata inainte. Anii istorici sunt publicati de OPCOM la rezolutie
ORARA (24 intervale/zi), iar anul curent alterneaza intre 30 si 15 minute --
orice zi la alta rezolutie decat 15 minute avea `count != expected_count`
(96 asteptat mereu) si `OpcomParseError`, ceea ce facea acea zi sa cada pe
fallback-ul sintetic (daca activat) sau sa esueze complet. Backfill-ul
istoric (`scripts/backfill_opcom_history.py`) foloseste exact acelasi cod,
deci era afectat identic pentru toate zilele vechi.

**Fix: rezolutia REALA, citita din CSV, in loc de o valoare fixa.**
`opcom_schema.py` mapeaza acum coloana "Rezolutie" (optionala -- CSV-urile
simple din teste/fixture-uri sintetice nu o au si raman neschimbate,
implicit 15 minute). `parse_csv` converteste valoarea ISO-8601 la minute
(`PT15M`->15, `PT30M`->30, `PT60M`/`PT1H`->60; orice altceva e respins
explicit, nu interpretat tacit gresit), valideaza ca TOATE randurile din
acelasi CSV au aceeasi rezolutie (o rezolutie amestecata in acelasi fisier
e o anomalie reala, semnalata clar, nu ignorata), si foloseste aceasta
rezolutie atat pentru numarul de intervale asteptat (inclusiv corect langa
tranzitiile DST, generalizat de la calculul deja existent pentru 15 minute)
cat si pentru pasul `interval_start`/`interval_end`. `MarketPriceInterval`
NU a necesitat nicio schimbare de schema -- `interval_start`/`interval_end`
puteau deja reprezenta orice durata, doar parserul o forta gresit la 15 minute.

**Consumatorii existenti au fost verificati explicit, nu doar presupusi
corecti.** `market_analytics_service.get_daily_averages` (medie aritmetica
pe zi -- corecta indiferent de numarul de randuri, atat timp cat toate
randurile UNEI zile au aceeasi durata, ceea ce OPCOM garanteaza),
`dashboard_service.get_prices`, si interogarile de pret din
`optimization_service`/`tariff_service` (`interval_start <= t < interval_end`)
sunt deja agnostice la rezolutie -- nu presupun un numar fix de randuri/zi.
Toate graficele care afiseaza preturi (`/market/prices`, cardul de preturi
de pe dashboard-ul statiei) porneau deja goale (empty-state) si isi incarca
datele printr-un fetch separat, dupa randarea paginii (`market.js`/`dashboard.js`)
-- niciun bloc mare de date de pret nu era randat sincron server-side inainte
de acest fix; verificat explicit, nu modificat (nu era nevoie).

**Agregare orara adaugata pentru ferestre mari (>10 zile).**
`market_analytics_service.get_timeline_split` (folosit de `/market/data/timeline`)
returneaza rezolutia bruta a randurilor doar pentru ferestre de cel mult 10
zile; peste acest prag, agrega pe ORA (medie). Fereastra implicita a
graficului principal e de 30 de zile (`market.js::loadTimeline`), deci
DEPASESTE pragul si beneficiaza automat de agregarea noua -- rezolva
practic problema de performanta semnalata in #33 (prea multe puncte pentru
intervale mari) fara nicio schimbare de UI. Un an intreg de istoric la 15
minute (altfel ~35.000 de puncte) devine cel mult ~8.760 de puncte (una pe
ora). #33 ramane deschis doar pentru partea de UI neceruta aici (un selector
explicit de interval pentru ferestre >30 zile).

**Ramas in afara scopului (deliberat, nu ascuns):** un selector de interval
in UI pentru "Piata energie" care sa permita cererea explicita a unei
ferestre mai mari de 30 de zile (fereastra implicita ramane hardcodata la
30 de zile in `market.js::loadTimeline`) -- vezi #33.
## 17. Dashboard: costuri istorice corecte, beneficiu EMS separat de cel al sistemului, prognoza fara informatie din viitor (issue #13)

**Costul istoric folosea tariful CURENT aplicat retroactiv, nu venitul de
export deloc.** `get_estimated_savings` apela `tariff_service.get_current_tariff_version(...,
at=utcnow())` si aplica ACEL pret (valabil doar "acum") la TOT intervalul
cerut (ex. ultimele 30 de zile) -- daca tariful s-a schimbat in acel
interval, costul istoric raportat era pur si simplu gresit. In plus, venitul
din export NU era scazut deloc din cost (doar `grid_import_energy_kwh` era
folosit). Fix: `_tariff_versions_for_range`/`_market_intervals_for_range` +
`_lookup_at` (bisectie) gasesc, pentru FIECARE ora din interval, tariful
REAL valabil in acea ora (import si export, inclusiv formula indexata
OPCOM+marja), iar `actual_net_cost_lei` include acum si venitul de export.

**Beneficiul sistemului PV/baterie era conflat cu beneficiul EMS.**
Rezultatul avea un singur reper ("fara PV/baterie deloc"), care masoara
beneficiul INSTALATIEI, nu al platformei. Acum sunt DOUA repere distincte,
documentate explicit in raspuns (nu prezentate ca economie masurata):
`whole_system_benefit_lei` (fata de "fara PV/baterie deloc") si
`ems_incremental_benefit_lei` (fata de "PV+baterie instalate, dar fara
optimizare activa -- auto-consum direct, fara arbitraj de pret"). Al doilea
reper e o APROXIMARE simpla (nu o simulare completa a unui dispecerat
alternativ fara EMS), documentata ca atare -- o simulare completa ar
necesita re-rularea unui dispecerat contrafactual peste tot istoricul, in
afara scopului acestui PR.

**Acoperirea datelor e acum vizibila, nu absorbita tacit.** O ora fara
consum masurat sau fara pret rezolvabil e EXCLUSA din toate cele trei sume
(real/reper1/reper2), nu tratata ca zero -- si `hours_priced`/`hours_expected`/
`hours_with_load_but_no_price`/`coverage_ratio` sunt raportate explicit, ca
un utilizator sa vada cat de completa e perioada analizata.

**Prognoza-vs-real putea folosi retroactiv o prognoza regenerata ulterior.**
`get_forecast_vs_actual` nu filtra deloc dupa `issued_at` -- daca o prognoza
era regenerata de mai multe ori (normal, periodic), interogarea returna MAI
MULTE randuri pentru acelasi `interval_start` (unul per batch), amestecand pe
grafic o prognoza veche cu una noua, fara ordine garantata. Fix: pentru
fiecare `interval_start`, se pastreaza doar cea mai recenta prognoza care
exista deja LA MOMENTUL acelui interval (`issued_at <= interval_start`) --
un singur punct per moment, niciodata o prognoza "din viitor" fata de
intervalul prezis.

**Planificat vs. executat: campurile `observed_*` existau in schema, dar
nu erau expuse deloc.** `PlanInterval.observed_battery_power_kw`/
`observed_grid_power_kw`/`observed_soc_percent`/`deviation_notes` sunt
mentionate in docstring-ul `OptimizationRun`/`Plan` ca fiind completate
"ulterior dintr-un job de reconciliere", dar **niciun asemenea job nu
exista inca in acest cod** (verificat explicit -- nicio referinta la aceste
campuri in afara modelului insusi). `get_plan_chart` le expune acum explicit
(None cand nu exista, nu omise), iar `dashboard.js` deseneaza seria "real"
doar daca exista deja cel putin o valoare -- pregatit pentru momentul in
care reconcilierea va exista, dar NU implementeaza reconcilierea insasi
(job Celery separat care ar calcula aceste valori din `TelemetryAggregate`
per interval de plan -- ramane de facut, in afara responsabilitatii
`dashboard_service`/UI stabilite de acest issue).

**Ramas in afara scopului (deliberat, nu ascuns):** unificarea seriilor
PV/consum/baterie/pret pe ACELASI grafic (in prezent, `chart-power` si
`chart-prices` raman grafice separate, fiecare cu axa lui) -- o redesenare
de UI mai ampla, neceruta explicit in criteriile testabile numeric ale
acestui issue; jobul de reconciliere `observed_*` mentionat mai sus.


## Addendum: Contracte tarifare: componente de cost distincte, TVA explicit, preview numeric (issue #46)

**Formula unica, centralizata.** Inainte de acest PR, logica de "pret
efectiv" era duplicata implicit intre `dashboard_service._effective_price`
(folosita atat "acum" cat si istoric, prin `_effective_price_at`) fara sa
existe un singur loc documentat cu formula exacta. `tariff_service.
compute_effective_price_lei_per_kwh` devine acel singur loc -- documentat
explicit (sursa: acest PR, 2026-09-12, NU o regula legala verificata, doar
aritmetica generica de facturare pe care operatorul trebuie sa o confirme
fata de contractul lui real) -- iar `dashboard_service._effective_price`
acum doar delega la el (verificat: toate cele 9 teste existente din
`test_dashboard_service.py` trec neschimbate, deci comportamentul pentru
tarifele deja existente ramane identic).

**Cost marginal separat de costul fix, pentru ambele tipuri de contract.**
`TariffVersion.fixed_price_lei_per_kwh`/`opcom_margin_lei_per_kwh` raman
strict costul de ENERGIE (marginal, pe kWh); `fixed_monthly_fee_lei` ramane
strict abonamentul FIX, independent de consum -- niciodata amestecate in
acelasi numar (`build_invoice_preview` le raporteaza separat explicit).
Pentru `kind=fixed`, pretul e CONSTANT indiferent de ora -- un pret constant
nu are niciun gradient de arbitraj intraday de extras, deci optimizerul nu
"inventeaza" o oportunitate de arbitraj de pret care nu exista (proprietate
matematica automata a formularii curente a obiectivului, nu cod nou).

**Componente de retea/taxe explicite, nu o "gaura neagra" generica.**
`TariffVersion` capata `distribution_lei_per_kwh`/`transport_lei_per_kwh`/
`other_regulated_lei_per_kwh` (migratia `e2a4c8f1d9b3`, toate 0 implicit --
backward-compatibil, comportament identic pentru versiunile existente).
`variable_component_lei_per_kwh` ramane disponibil pentru compatibilitate/
simplitate cand contractul nu separa aceste componente.

**TVA explicit opt-in, `None` != 0%.** `vat_rate_percent` (procent, ex. 19)
e `None` implicit -- inseamna explicit "TVA neinclus in aceasta formula",
nu o presupunere de 0%. Cand e setat, se aplica atat pe partea de energie
(in `compute_effective_price_lei_per_kwh`) cat si, separat, pe abonamentul
fix (in `build_invoice_preview`) -- asta e aritmetica generala de TVA (se
aplica pe toata suma taxabila, nu doar pe energie), NU o regula specifica
legislatiei romanesti inventata aici.

**Lipsa pretului OPCOM blocheaza calculul, nu produce un fallback tacut.**
Comportament deja existent inainte de acest PR (verificat, nu adaugat acum)
in `compute_effective_price_lei_per_kwh`: pentru un tarif `indexed_opcom`
fara pretul OPCOM al orei respective, functia returneaza `None` explicit --
apelantul exclude acea ora, nu foloseste 0 sau un pret vechi.

**Preview numeric pe pagina de tarife.** `/stations/{id}/tariffs` calculeaza
acum, pentru ultima versiune a fiecarui tarif, un exemplu numeric la 300 kWh
(consum lunar tipic, arbitrar ales pentru ilustrare) folosind ultimul pret
OPCOM disponibil ca referinta pentru tarifele indexate -- etichetat explicit
"exemplu... NU e o factura reala" in UI, cu motivul afisat cand nu poate fi
calculat (calcul dezactivat sau niciun pret OPCOM disponibil).

**Spot vs. cost efectiv, clarificat in dashboard (impartit cu issue #51).**
KPI-urile de pret din `dashboard/station.html` devin explicit "Cost efectiv
import"/"Venit efectiv export" (cu link catre pagina de tarife), distincte
de graficul "Pret spot OPCOM PZU" -- vezi si sectiunea 18 anterioara
(issue #51) pentru detalii, aceeasi schimbare de UI acopera ambele issue-uri.

**Teste:** `tests/unit/test_tariff_service.py` (16) -- separarea marginal/
fix, indexare OPCOM cu marja pozitiva/negativa, blocare fara fallback la
pret lipsa, `economic_calculation_disabled`, insumarea tuturor componentelor
noi, TVA aplicat/neaplicat (`None` vs. `0%` distincte), TVA pe abonament
separat de energie, motivele de preview indisponibil, persistarea noilor
campuri prin `add_tariff_version`. `tests/integration/test_tariffs_routes.py`
(4) -- ruta HTTP persista noile campuri, `viewer` nu poate crea tarife,
preview afisat/motiv-indisponibil pe pagina.

**Ramas in afara scopului (deliberat, nu ascuns, coordonat cu alte issue-uri):**
- **Wizard-ul dedicat cu preseturi** (issue #41, inca neimplementat) --
  configurarea ramane prin formularul de tarife existent, extins cu noile
  campuri, nu o experienta ghidata pas-cu-pas.
- **Reguli legale/comerciale romanesti verificate** (cote OPANAF/ANRE reale,
  formule reglementate de distributie/transport) -- deliberat NEINVENTATE;
  operatorul introduce valorile reale din contractul/factura lui, iar acest
  PR ofera doar structura si aritmetica de combinare a lor, nu sursa datelor.
- **Decontare neta ora-cu-ora in preview** -- preview-ul foloseste UN SINGUR
  pret OPCOM de referinta (cel mai recent disponibil) inmultit cu consumul
  total, nu o simulare completa interval-cu-interval (aceea exista deja,
  separat, in `dashboard_service.get_estimated_savings`, issue #13).


## Addendum: Retentie revizii OPCOM (max. active/zi) si contract explicit de unitati (issue #51)

**O revizie de pret e strict un `ImportRun` cu `status=succeeded`.** Fiecare
incercare de import (`opcom_service.import_opcom_day`) primeste un numar de
revizie nou, INDIFERENT daca reuseste sau esueaza -- deci `revision` singur
nu distinge o versiune reala de pret de o tentativa esuata/metadata de
audit. Politica de retentie noua (`app/services/market_retention_service.py`)
numara si arhiveaza EXCLUSIV revizii reusite; tentativele esuate sunt
ignorate complet (nu conteaza la prag, nu sunt niciodata arhivate).

**Arhivare STRICT NEDISTRUCTIVA, nu stergere.** Peste
`DEFAULT_MAX_ACTIVE_REVISIONS` (5) revizii reusite pastrate active per zi de
livrare, cele mai vechi capata `ImportRun.is_archived=True` +
`archived_at`/`archived_reason` (migratia `b7d3f6a9c1e4`) -- randul si toate
`MarketPriceInterval` asociate raman intacte in baza de date, interogabile
oricand (verificat explicit intr-un test). Revizia CURENTA
(`is_current=True`) nu e niciodata arhivata, indiferent de varsta -- e
singura referinta "vie" folosita de restul platformei; asta satisface
cerinta ca "revizia folosita de un optimization run/factura ramane
referentiabila" fara sa fie nevoie de o legatura FK explicita (nu exista
inca niciun cod care sa retina un FK catre o revizie specifica -- toate
calculele istorice folosesc intervalele stocate direct, pe interval de
timp, nu pe numar de revizie).

**Stergerea DEFINITIVA (hard-delete) NU e implementata automat, deliberat**
-- necesita aprobare legal/ops explicita, in afara acestui cod (identic cu
decizia din issue #24 pentru organizatii/statii). Retentia planificata
(Celery beat, zilnic la 03:30, `market_revision_retention_task`) si
declansarea manuala din admin (`POST /admin/operations/market-retention`,
prin fluxul `AdminJob` din issue #11) fac ACEEASI operatie idempotenta si
concurrent-safe (lock advisory PostgreSQL, cheiat pe sursa) -- rulata de
doua ori fara nimic nou intre timp nu arhiveaza nimic suplimentar. Ambele
suporta `dry_run` (implicit BIFAT in formularul admin -- utilizatorul
trebuie sa debifeze explicit pentru o rulare reala).

**Contract explicit de unitati, centralizat.** Inainte existau 3 locuri
separate care converteau intre lei/MWh si lei/kWh, fiecare cu propriul
`*1000`/`/1000` scris ad-hoc (`opcom_service.parse_csv`,
`market_analytics_service.get_daily_averages`/`get_forecast_to_year_end`).
Noul modul `app/core/units.py` (`KWH_PER_MWH`, `mwh_to_kwh`, `kwh_to_mwh`)
e singurul loc care stie factorul de conversie -- toate cele 3 locuri il
folosesc acum, eliminand riscul unei conversii gresite scrise independent
in viitor. API-ul canonic (JSON) continua sa expuna EXPLICIT ambele unitati
pe fiecare punct (`price_lei_mwh`/`price_lei_kwh`, `avg_price_lei_mwh`/
`avg_price_lei_kwh` etc.) -- deja asa inainte de acest issue, verificat, nu
schimbat.

**Spot OPCOM vs. cost efectiv contractual -- clarificat explicit in UI.**
Dashboard-ul statiei (`dashboard/station.html`) avea doua notiuni de "pret"
etichetate identic ("lei/kWh") dar DIFERITE: KPI-urile "Pret cumparare"/
"Pret vanzare" (costul EFECTIV, din tariful contractual al statiei) si
graficul "Preturi PZU" (pretul SPOT OPCOM brut). Etichetele KPI devin
explicit "Cost efectiv import"/"Venit efectiv export" (cu tooltip catre
pagina de tarife), iar graficul devine "Pret spot OPCOM PZU" cu o nota text
ca nu e neaparat costul efectiv al clientului.

**Teste:** `tests/unit/test_market_retention_service.py` (arhivare peste
prag, niciodata revizia curenta, ignora tentative esuate, dry-run
nemodificator, idempotenta la a doua rulare, nicio stergere de rand/interval,
prag invalid respins, date/surse independente), `tests/unit/test_units.py`
(conversie round-trip, semn negativ, zero), `tests/integration/test_admin_job_tasks.py`
(taskul Celery real arhiveaza corect printr-o sesiune separata),
`tests/integration/test_admin_operations_routes.py` (ruta HTTP, dry-run
implicit, respinge prag invalid, protectie impotriva declansarii duble).

**Ramas in afara scopului (deliberat, nu ascuns):** stergerea definitiva
efectiva (vezi mai sus -- pas manual, cu aprobare, in afara acestui cod);
o pagina UI dedicata de rasfoire a reviziilor arhivate (in prezent doar
badge-ul "arhivat" in tabelul de import-uri din `/admin/operations`);
o formula completa de contract tarifar (componente separate distributie/
transport/taxe/TVA) -- ramane in scopul issue #46, coordonat separat.


## 18. Grafic timeline preturi OPCOM: agregare adaptiva pe 3 niveluri + incarcare progresiva (issue #33)

**Restul acestui issue era deja rezolvat de #35** (agregarea orara pentru
ferestre >10 zile, aplicata automat ferestrei implicite de 30 de zile din
UI). Ramasese totusi un gol fata de criteriile explicite ale issue-ului:
agregarea orara singura tot produce mii de puncte pentru un an intreg
(~8760), nu "sute" cum cere criteriul de acceptare, iar UI-ul nu avea nicio
cale sa ceara o fereastra mai mare decat cele 30 de zile implicite fara sa
rezulte, potential, intr-un singur fetch masiv.

**Agregare pe 3 niveluri, nu 2.** `market_analytics_service.get_timeline_split`
alege acum intre rezolutia bruta (<= `TIMELINE_HOURLY_THRESHOLD_DAYS` = 10
zile), agregare orara (intre acel prag si `TIMELINE_DAILY_THRESHOLD_DAYS` =
60 zile) si agregare ZILNICA (peste 60 de zile) -- extrase intr-un helper
comun `_get_timeline_aggregated(..., trunc_unit)` ca sa nu se duplice
interogarea SQL intre nivelul orar si cel zilnic. Pentru un an intreg de
istoric la 15 minute, raspunsul ramane la cel mult ~366 puncte (un punct pe
zi), nu ~35.000 (rezolutie bruta) si nici ~8760 (doar orara).

**Incarcare progresiva in UI, nu un singur fetch cu tot istoricul.**
`market.js` adauga un selector de interval (30/90/180/365 zile) langa
graficul principal de preturi -- fiecare optiune declanseaza o cerere NOUA
catre `/market/data/timeline?days=...` doar cand utilizatorul o cere
explicit, nu un fetch initial care ar incerca sa incarce tot intervalul
maxim posibil. Fereastra implicita la incarcarea paginii ramane 30 de zile,
neschimbata (agregare orara, ca inainte de acest issue).

**Teste:** `test_timeline_split_stays_hourly_at_exactly_the_daily_threshold`
(pragul de 60 de zile e strict, ca cel de 10 zile), `test_timeline_split_
aggregates_daily_for_year_long_windows` (an intreg la 15 minute -> <=366
puncte), `test_timeline_split_daily_aggregation_averages_within_bucket`,
`test_timeline_split_daily_aggregation_excludes_synthetic_by_default`, plus
un test HTTP (`test_market_data_timeline_stays_bounded_for_large_windows`)
care verifica direct raspunsul rutei `/market/data/timeline?days=365`.

**Ramas neschimbat (in afara scopului):** celelalte grafice de pe pagina
(yearly-overlay, monthly, forecast) -- deja agregate corespunzator (zi/luna),
nu au fost atinse.


## 19. Gestiune membri si RBAC delegat pentru managerul clientului (issue #23)

**Matricea de capabilitati** e documentata explicit in `docs/RBAC_MATRIX.md`
(nu doar in acest fisier) -- patru roluri ordonate strict (`viewer` <
`operator` < `organization_admin` < `platform_admin`), verificate SERVER-SIDE
la fiecare endpoint (`StationAccess`/`OrganizationAccess`, parametrizate cu
`min_role`), niciodata doar prin ascunderea unui buton in UI. Un test nou
(`tests/unit/test_rbac_matrix.py`) verifica direct, parametrizat pe toate
cele 4 roluri, ca fiecare helper `can_*` din `app/core/rbac.py` respecta
exact pragul documentat -- daca cineva schimba un prag fara sa actualizeze
documentul (sau invers), testul pica.

**Membership-urile pot fi acum administrate complet, nu doar create.**
Inainte de acest issue exista doar crearea de invitatii si listarea
(read-only) a membrilor -- lipseau schimbarea de rol, retrimiterea/anularea
unei invitatii si dezactivarea/eliminarea unei membership. Serviciul nou
`app/services/membership_service.py` adauga toate acestea, disponibile atat
prin autoservire (`/organizations/{id}/members/...`, `organization_admin`+
al organizatiei respective) cat si din backoffice
(`/admin/organizations/{id}/members/...`, `platform_admin`, cross-tenant,
FARA impersonare -- actioneaza explicit ca platform_admin, auditat cu
`actor_label` = emailul lui, nu al membrului).

**Dezactivare NEDISTRUCTIVA, distincta de eliminare.** `Membership` capata
un camp `is_active` (migratia `f1c7a9e2b4d6`, default `true` pentru randurile
existente -- comportament identic cu inainte). "Dezactiveaza" pastreaza
randul (istoricul de rol/audit ramane atasabil, reversibil prin
"Reactiveaza"); "Elimina" e hard-delete, pentru corectarea unei
invitatii/membership create din greseala, nu mecanism normal de offboarding
-- acelasi pattern folosit deja pentru `Station.is_active`/`Organization.status`
in issue #24.

**`is_active=False` echivaleaza PESTE TOT cu lipsa membership-ului.**
`OrganizationAccess`, `StationAccess` (`app/api/deps.py`) si autorizarea
fluxului SSE (`app/web/routes/sse.py::_authorized_summary`) filtreaza acum
explicit `Membership.is_active.is_(True)` -- o membership dezactivata
blocheaza acces identic cu una inexistenta. Fluxul SSE re-verifica autorizarea
la FIECARE ciclu de polling (nu doar la deschiderea conexiunii), deci o
dezactivare intrerupe un flux deja deschis in cel mult
`POLL_INTERVAL_SECONDS`, nu doar la o reconectare -- verificat direct
(`tests/unit/test_sse_authorization.py`) apeland `_authorized_summary`
inainte/dupa dezactivare, fara sa porneasca un flux async real.

**Schimbare sensibila = revocare imediata de sesiune, pe TOATE dispozitivele.**
Schimbarea de rol, dezactivarea si eliminarea unei membership revoca acum
toate sesiunile web ale utilizatorului afectat
(`auth_service.revoke_all_sessions_for_user`) -- nu doar sesiunea curenta.
Testat explicit cu doua sesiuni simultane ale aceluiasi membru ("doua
taburi"): ambele devin invalide, nu doar cea care a facut ultima cerere
(`test_two_active_sessions_of_the_same_member_are_both_invalidated_on_deactivation`).
Aplicatia NU are (inca) un flux dedicat de reconfirmare cu parola/MFA pentru
aceste actiuni -- revocarea de sesiune de mai sus e substitutul functional
actual (utilizatorul trebuie sa se re-autentifice pentru orice acces
ulterior), documentat explicit ca decizie deliberata, nu omisiune ascunsa
(vezi si `docs/RBAC_MATRIX.md`).

**Protectia ultimului `organization_admin` activ.** Nicio actiune
(retrogradare, dezactivare, eliminare) nu poate lasa o organizatie fara
niciun `organization_admin` activ (`membership_service._assert_not_last_admin`). Mutațiile sunt serializate
cu row locks PostgreSQL pentru a preveni write-skew-ul în care doi admini
se retrogradează simultan -- altfel ar bloca administrarea ulterioara a membrilor/statiilor pentru toata
lumea, inclusiv pentru un nou `organization_admin` promovat manual (nimeni
nu ar mai avea dreptul sa faca promovarea). "Transferul" rolului de manager
se face in doi pasi (promoveaza intai un alt membru, apoi optional
retrogradeaza-l pe cel vechi). Regresia concurenta ruleaza cu doua sesiuni
PostgreSQL independente -- aceasta protectie garanteaza ca al doilea
pas ramane mereu posibil dupa primul.

**`platform_admin` nu poate fi acordat printr-o invitatie/schimbare de rol
de organizatie.** `auth_service.ORGANIZATION_ROLES` (allowlist-ul validat
server-side pentru invitatii SI pentru `membership_service.change_role`) nu
contine niciodata `platform_admin` -- e exclusiv un flag global pe `User`,
acordat doar prin bootstrap (o singura data) sau direct in baza de date.
Verificat explicit (`test_platform_admin_cannot_be_granted_via_organization_role_allowlist`,
`test_change_role_rejects_platform_admin_as_target`).

**Teste negative cross-tenant si per rol.** O membership dintr-o alta
organizatie nu poate fi modificata prin ID-ul ei (`_get_membership_in_org`
verifica apartenenta la organizatia din URL inainte de orice mutatie) --
verificat ca redirect generic cu eroare, nu 404/500 care ar confirma
existenta ei catre un actor neautorizat. `operator`/`viewer` nu pot accesa
niciuna dintre rutele noi de administrare a membrilor (403). Toate rutele
noi de mutatie cer CSRF.

**Ramas in afara scopului (deliberat, nu ascuns):**
- MFA/reconfirmare cu parola pentru actiuni sensibile -- aplicatia nu are
  MFA deloc inca (vezi mai sus).
- UI dedicat pentru "istoricul" schimbarilor de rol ale unui membru dincolo
  de jurnalul general de audit (`recent_audit`, deja afisat) -- niciun
  criteriu testabil numeric al issue-ului nu a cerut un ecran separat.
- Migrarea/backfill-ul datelor existente pentru `Membership.is_active` --
  `server_default=true` acopera deja toate randurile existente, identic cu
  starea dinainte de acest issue.

## Addendum: Catalog administrabil de invertoare/baterii/panouri (issue #42)

**Ce exista.** `EquipmentManufacturer`/`EquipmentModel` (`app/models/equipment_catalog.py`)
formeaza catalogul administrabil, gestionat exclusiv din backoffice
(`/admin/catalog`, `platform_admin`). Fiecare model are `equipment_type`
(inverter/battery/pv_module), `specs` (JSON liber) si `source_note`
**obligatoriu** la creare (`equipment_catalog_service.create_equipment_model`)
-- catalogul nu accepta o intrare fara o sursa citata, exact cerinta
issue-ului de a nu inventa specificatii. Search/autocomplete
(`GET /equipment/search`, orice utilizator autentificat) exclude implicit
modelele inactive.

**Seed: deliberat gol.** Nu am incarcat NICIUN model Deye (sau alt
producator) cu specificatii numerice presupuse -- issue-ul cere explicit fie
o citare a fisei oficiale, fie un catalog minimal. Fara acces verificat la
fise tehnice oficiale in acest mediu, catalogul livrat e gol la instalare;
un administrator populeaza modelele reale prin `/admin/catalog`, fiecare cu
`source_note` propriu.

**Snapshot imutabil, nu pointer live.** `StationConfigVersion`/`PanelGroup`
retin `*_model_id` + `*_model_spec_revision` + `*_model_snapshot` (JSON)
capturate la momentul selectiei (`equipment_catalog_service.build_snapshot`).
O editare ulterioara a specificatiilor unui model (`update_equipment_model_specs`,
care creste `spec_revision`) NU modifica retroactiv configuratii deja
publicate -- verificat explicit
(`test_catalog_edit_does_not_change_already_published_snapshot`). Acelasi
principiu de imutabilitate ca `StationConfigVersion` insusi (issue #8).

**Dezactivare nedistructiva, fara hard-delete.** `is_active=False` (pe
producator sau model) scoate intrarea din search-ul folosit la configurari
NOI, dar randul ramane in DB si orice configuratie care il refera prin
snapshot continua sa functioneze identic -- verificat
(`test_toggle_model_active_is_nondestructive`). Nu exista niciun flux de
stergere fizica a unui model sau producator din catalog.

**Fallback custom, fara a crea o intrare de catalog nevalidata.** Optiunea
"modelul meu nu e in lista" NU creeaza un rand `EquipmentModel` cu date
nevalidate -- salveaza doar o eticheta text (`*_custom_label`) direct pe
configuratia statiei, langa campurile numerice deja existente (completate
manual, ca inainte de acest issue). Un `model_id` de catalog si o eticheta
custom in acelasi timp sunt respinse explicit la validare
(`test_config_rejects_both_catalog_model_and_custom_label`) -- niciodata
ambele tacit.

**Catalogul comercial e distinct de compatibilitatea RS485.** A adauga
"Deye SUN-10K-SG04LP3" in catalog NU activeaza automat vreun control fizic
prin Modbus -- `InverterProfile`/`InverterDesired` (issue #17) raman un
artefact separat, aprobat explicit de un `platform_admin`, complet
neconectat la acest catalog comercial.

**Ramas in afara scopului (deliberat, nu ascuns):**
- Wizard-ul dedicat de configurare (issue #41, inca neimplementat) --
  search-ul/catalogul e integrat in formularul existent
  `/stations/{id}/config`, reutilizabil de un viitor wizard.
- Import/export administrativ in masa al catalogului (CSV/JSON) -- backoffice-ul
  actual e CRUD unu-cate-unu, suficient pentru un catalog pornit gol.
- Deduplicare automata / sugestii de fuziune intre modele introduse manual
  de mai multe ori cu variatii de nume -- `UniqueConstraint(manufacturer_id,
  equipment_type, model_name)` previne doar duplicatele EXACTE.
- Migrarea configuratiilor de statii EXISTENTE catre un model de catalog --
  toate coloanele noi sunt nullable si opționale; statiile configurate
  inainte de acest issue raman neschimbate (fara model de catalog asociat).

## Addendum: Actualizare live a dashboard-ului (issue #50)

**Decizie de transport: SSE, nu WebSocket** -- vezi
`docs/adr/0001-realtime-dashboard-transport.md`. Fluxul SSE existent
(issue #6) e extins la un contract per-metrica versionat
(`metric`/`value`/`unit`/`measured_at`/`received_at`/`quality`/`source`),
nu inlocuit cu un transport nou. `dashboard_service.get_summary` (randarea
HTTP initiala) ramane NESCHIMBAT -- `get_live_metrics` e o functie noua,
separata, verificat explicit prin
`test_get_summary_output_unchanged_alongside_new_live_metrics`.

**Contract de resume, fara jurnal de evenimente.** Serverul NU pastreaza
un buffer de evenimente trecute pentru replay dupa o reconectare. Prima
emisie a FIECAREI conexiuni (initiala sau reconectare) e intotdeauna un
eveniment `snapshot` complet -- verificat
(`test_first_event_is_always_a_snapshot`). Acesta ESTE mecanismul de
"refresh la gap" cerut de issue: reconectarea aduce starea completa prin
insusi fluxul SSE, fara o cerere HTTP separata. Un jurnal real de
evenimente pentru replay partial peste un gap de minute a fost evaluat si
respins deliberat -- ar fi la fel de costisitor ca un `snapshot` complet,
fara niciun beneficiu practic aici.

**Coalescing, nu token-bucket separat.** Fiecare tur de polling (5s)
retrimite DOAR metricile schimbate fata de ultima emisie
(`sse.diff_metrics`, testat direct cu mai multe cazuri, inclusiv
schimbari multiple intre doua tururi care se contopesc intr-un singur
`delta`). Intervalul de 5s e deja bugetul de rata -- nu a fost nevoie de
un mecanism suplimentar de backpressure pentru acest volum de date
(cateva zeci de metrici per statie, nu telemetrie bruta la fiecare
esantion).

**Stari de conexiune** (`connecting`/`live`/`stale`/`offline`) afisate in
UI (`#sse-status`, `dashboard.js`) -- derivate din callback-urile
`emsConnectSSE` plus un watchdog client-side (`stale` daca nu s-a primit
niciun mesaj in ultimele `3 x POLL_INTERVAL_SECONDS`). Verificat end-to-end
intr-un browser real (Playwright, `test_dashboard_sse_connection_reaches_live_status`)
-- singurul mod fiabil de a confirma ca `EventSource` chiar se conecteaza
si primeste `snapshot`-ul initial, fara sa mocuiasca transportul.

**Regresie preexistenta descoperita (neinrudita, nu introdusa aici):**
`test_bootstrap_login_create_org_and_station_flow` (issue #14) navigheaza
prin `/admin/organizations/{id}` (backoffice-ul introdus de issue #24) si
asteapta acolo formularul "+ Adauga statie", care exista DOAR pe pagina de
autoservire `/organizations/{id}` (paginile s-au despartit intre timp).
Confirmat identic pe `main`, inainte de orice schimbare din acest PR --
documentat aici, nu reparat (in afara scopului issue #50); testul nou de
SSE isi creeaza propriile date direct in baza de date ca sa ramana
independent de acest flux stricat.

**Ramas in afara scopului (deliberat, nu ascuns):**
- Un canal WebSocket real -- evaluat explicit si respins (vezi ADR 0001);
  ramane o optiune viitoare STRICT daca apare o cerere reala de control
  bidirectional prin dashboard.
- Jurnal de evenimente persistat pentru replay partial peste un gap lung
  de reconectare -- `snapshot`-ul complet la reconectare acopera deja
  acest caz, la un cost comparabil.
- Reparatia testului e2e preexistent stricat mentionat mai sus -- afara
  din scopul acestui issue.

## Addendum: Blocaj real raportat local -- fluxul SSE bloca orice alta cerere (fix critic)

**Simptom raportat.** Rularea locala a serverului si deschiderea fluxului
`GET /stations/{id}/sse` (issue #50) bloca NEDEFINIT orice alta cerere care
folosea acelasi cookie de sesiune (ex. reincarcarea dashboard-ului intr-un alt
tab al aceluiasi browser) -- "totul altceva ramane blocat, la infinit".

**Cauza reala, reprodusa si confirmata cu `py-spy dump` pe procesul blocat.**
`get_current_context` (`app/api/deps.py`) actualiza `Session.last_seen_at`
doar cu `db.flush()`, fara `db.commit()`. `db` provine din `get_db()`, o
dependinta FastAPI cu `yield` -- Starlette/FastAPI NU inchide/rollback-uieste
o astfel de dependinta decat DUPA ce raspunsul e trimis INTEGRAL. Pentru un
raspuns in flux lung (`EventSourceResponse`), "trimis integral" inseamna
"niciodata cat timp clientul ramane conectat" -- deci UPDATE-ul necomis tinea
un row lock PostgreSQL deschis pe randul `sessions` pe TOATA durata conexiunii
SSE. Orice alta cerere autentificata cu acelasi cookie (acelasi rand
`sessions`) bloca la randul ei nedefinit, incercand acelasi UPDATE in propria
ei rulare a `get_current_context` -- exact simptomul raportat.

**Fix (doua parti, ambele necesare):**
1. `get_current_context` acum face `db.commit()` imediat dupa actualizarea
   `last_seen_at`, nu doar `flush()` -- elibereaza lock-ul in momentul
   actualizarii, indiferent cat dureaza restul cererii (fix sistemic: se
   aplica oricarui raspuns lent/in flux, nu doar SSE).
2. `station_live_stream` (`app/web/routes/sse.py`) inchide explicit `db`
   (dependinta `get_db()`) INAINTE de a construi `EventSourceResponse` --
   altfel conexiunea Postgres din pool ar ramane ocupata (desi fara lock,
   dupa fix-ul 1) pe toata durata vizionarii de catre fiecare tab de
   dashboard deschis, un risc real de epuizare a pool-ului cu mai multi
   vizitatori simultani.

**Verificat end-to-end** cu un server real (nu doar teste): reprodus blocajul
cu `curl` (doua conexiuni separate, acelasi cookie -- deci nu era o limitare
de conexiuni per-origine a unui singur browser), confirmat cauza cu
`py-spy dump` (un thread `AnyIO worker` blocat in `psycopg` `wait()` pe un
UPDATE), verificat rezolvarea (cereri concurente raspund in <25ms cu fluxul
SSE deschis). Regresie acoperita si de
`tests/integration/test_auth_session_lock_regression.py` (doua conexiuni
Postgres reale, nu fixture-ul `db` cu SAVEPOINT, care nu poate exercita
contentie de lock reala).

## Addendum: Provisioning prin serial -- wizard, transfer/factory reset, dezactivare legacy (issue #44, dupa PR #59)

PR #59 a livrat deja partea grea a issue-ului #44: provisioning secret separat
pe device, serial public, Device Code sigilat stocat server-side numai ca
hash, enrollment/polling idempotent, claim self-service atomic, RBAC/CSRF/
rate-limit si raspuns generic anti-enumerare -- vezi sectiunea 14 de mai sus
si docs/API.md. Nota de progres a issue-ului enumera patru bucati ramase;
aceasta lucrare le acopera punctual pe toate patru, cu limitele fiecareia
documentate explicit mai jos.

**1. Integrare in wizard-ul multi-step -- NU exista niciun wizard multi-step
in aplicatie** (verificat explicit: nicio ruta/template cu "wizard" in tot
codul, in afara acestei mentiuni). In loc sa construiesc un subsistem nou de
wizard de la zero (scop propriu, mult mai mare decat acest issue), am
integrat pasul de asociere prin serial in singurul loc unde chiar apare in
fluxul real: crearea unei statii (`POST /organizations/{id}/stations`)
redirecteaza acum direct la `/stations/{id}/devices?onboarding=1` (in loc de
pagina organizatiei), iar `stations/devices.html` afiseaza un indicator de
pasi ("1. Statie creata -> 2. Asociaza device-ul -> 3. Configurare") si, dupa
o asociere reusita in acest context, un buton explicit spre pasul 3
(`/stations/{id}/config`). Parametrul `onboarding=1` e purtat prin toate
redirect-urile intermediare (form ascuns + query string), fara stare server
noua. Un wizard dedicat, cu preseturi si validare progresiva intre pasi,
ramane issue-ul #41 -- neatins aici, doar secventiat ce exista deja.

**2. Transfer / factory reset -- implementate, strict `platform_admin`,
deliberat cross-tenant-capabile.** Pagina noua `/admin/devices/assigned`
(tab nou in `admin/_tabs.html`) listeaza orice device activ, asociat unei
statii, cu doua actiuni noi in `device_service.py`:
- `transfer_device` muta un device ACTIV la o alta statie (inclusiv intre
  organizatii diferite -- decizie deliberata: e un scenariu administrativ
  real, hardware revandut/reinstalat la alt client, nu o preluare
  neautorizata de utilizator; niciun organization_admin nu are acces la
  aceasta ruta). Revoca IMEDIAT credentiala curenta si genereaza una noua,
  livrabila device-ului prin exact acelasi canal idempotent deja existent
  (`POST /api/v1/devices/enroll` -> raspuns `assigned` cu
  `credential_secret`) -- niciun cod nou de editat manual pe device, niciun
  protocol nou. Device-ul nu primeste config/secrete ale statiei noi inainte
  de acest transfer explicit.
- `factory_reset_device` detaseaza un device ACTIV de statia lui, revoca
  imediat credentiala curenta si il intoarce la `pending_claim` fara statie
  -- exact starea unui enrollment proaspat, deci realocabil imediat prin
  `/admin/devices/pending` sau printr-un nou Device Code. Identitatea proprie
  a dispozitivului (`installation_uuid`/`provisioning_secret_hash`) NU e
  atinsa -- un factory reset FIZIC real, pe hardware, si-ar regenera-o
  singur la urmatorul enroll; acest capat administrativ acopera doar partea
  controlata de server (detasare + revocare), nu simuleaza reset-ul fizic.
- Ambele cer in formular confirmarea explicita, server-side, a serialului
  (sau `installation_uuid` daca inca nu are serial public) afisat pe pagina
  -- nu doar un dialog JS `confirm()`, ocolibil trivial cu un POST direct --
  si sunt inregistrate in audit (`device_transferred`, `device_factory_reset`,
  plus varianta `_failed` la confirmare gresita sau stare invalida).
- **"Device inlocuit" nu are un capat nou dedicat** -- e deja acoperit complet
  de primitivele existente: `revoke_device` pe unitatea veche (stricata) +
  fluxul normal de enrollment/Device Code pentru unitatea noua, la aceeasi
  statie. Un capat separat "replace" ar fi doar aceasta compunere, fara
  comportament nou -- l-am documentat aici in loc sa adaug cod redundant.

**3. Dezactivare completa a fluxului legacy -- flag explicit
`legacy_claim_code_enabled` (`app/config.py`), interzis in productie, NU
stergere destructiva.** Codul temporar de 15 minute (`ClaimCode`,
`device_service.create_claim_code`/`claim_device`) ramane folosit direct de
`scripts/mock_device_cli.py`, `scripts/seed_demo.py` si o parte din testele
de integrare existente (`test_device_api.py`, `test_commands.py`,
`test_device_protocol_hardening.py`) -- eliminarea lui completa ar fi
stricat toate acestea pentru un beneficiu de securitate nul in productie
(flag-ul rezolva deja riscul real). Asadar:
- `Settings.legacy_claim_code_enabled` este implicit `False` in orice mediu;
  suitele istorice il activeaza explicit. `model_post_init` **refuza pornirea
  aplicatiei** daca e `True` si `ENVIRONMENT=production` -- acelasi tipar folosit pentru
  `demo_mode_enabled`/`opcom_use_synthetic_fixture_on_failure`/
  `session_cookie_secure`/`email_backend`. Nu exista nicio cale de a porni
  serverul de productie cu acest bypass activ.
- Independent de acel guard de pornire, si ruta web
  (`POST /stations/{id}/claim-codes`) si cea de dispozitiv
  (`POST /api/v1/devices/claim`) verifica flag-ul la fiecare cerere si refuza
  explicit (redirect cu `error=legacy_disabled`, respectiv `410 Gone`) cand e
  dezactivat -- deci flag-ul chiar opreste functional fluxul oriunde e setat
  pe `False`, nu doar la pornirea in productie. UI-ul (`stations/devices.html`)
  ascunde complet cardul "Flux legacy" cand flag-ul e dezactivat.
- Codurile deja emise si neconsumate NU sunt invalidate retroactiv la
  dezactivare (flag-ul blocheaza doar EMITEREA/CONSUMUL de la acel moment
  incolo) -- acceptabil, fiindca in productie flag-ul nu poate fi activat
  niciodata, deci nu exista coduri legacy emise acolo.
- **Ramas explicit pentru un PR viitor:** stergerea fizica a modelului
  `ClaimCode`/`claim_device` si a scripturilor care il folosesc, dupa ce
  `EMS-device-code#3` (dependinta cross-repo mentionata in issue, inaccesibila
  din acest mediu) confirma ca simulatoarele/dispozitivele reale au migrat
  complet pe enrollment automat + Device Code.

Transferul si resetarea administrativa blocheaza randul device-ului cu
`SELECT ... FOR UPDATE`; doua cereri concurente nu pot emite credentiale
active conflictuale pe baza aceleiasi stari vechi.
Acest lock completeaza testul de concurenta al activarii; operatiile de
mentenanta si asocierea initiala au astfel garantii explicite separate.

**4. Test de concurenta cu doua conturi/sesiuni Postgres --
`test_concurrent_claim_two_accounts_same_code_exactly_one_wins`
(`tests/integration/test_device_provisioning_issue44.py`).** Doua conturi
DIFERITE, din doua organizatii DIFERITE, incearca sa revendice ACELASI cod
de asociere in acelasi timp, prin doua sesiuni SQLAlchemy separate legate de
`engine` (conexiuni Postgres reale, commit-uri reale) intr-un
`ThreadPoolExecutor`, sincronizate cu `threading.Barrier` si citite cu
`future.result(timeout=10)` -- niciodata fixtura `db` cu SAVEPOINT, care nu
poate exercita contentie reala de lock (acelasi tipar ca testele de cursa
deja existente in `test_device_protocol_hardening.py` si
`test_device_enrollment.py`). Verificat explicit: exact un cont castiga
(celalalt primeste eroare curata, fara deadlock si fara timeout), rezultatul
apartine statiei pentru care codul a fost emis (niciodata celeilalte,
indiferent care cont a castigat cursa -- fara asociere cross-tenant tacita),
si nu apare niciun al doilea device dublat.

**Ramas in afara scopului acestui PR (deliberat, nu ascuns):**
- Un wizard multi-step dedicat, cu preseturi si pasi de validare separati --
  issue #41, neatins; am secventiat doar fluxul existent (vezi punctul 1).
- Un capat "replace device" dedicat -- deja acoperit de compunerea
  revoke + enrollment nou (vezi punctul 2).
- Stergerea fizica a fluxului legacy cu cod de 15 minute -- gatat complet
  functional (flag + interdictie de pornire in productie), dar codul insusi
  ramane in repo pentru simulatoare/dezvoltare pana la migrarea confirmata
  mentionata la punctul 3.
- Testele de replay/enumerare-de-seriale/brute-force explicite -- deja
  acoperite de PR #59 ("raspuns generic anti-enumerare"); nu erau in nota de
  progres ca ramase, deci nu au fost reluate aici.
- Contractul cross-repo cu `EMS-device-code#3` -- repo inaccesibil din acest
  mediu, mentionat explicit in issue ca dependinta separata.
## Addendum: Detaliere financiara PV/autoconsum/export, cu provenienta tarifului (issue #49)

**Scop, limitat deliberat.** Issue #49 cere atat o detaliere financiara mai
clara (carduri separate pentru valoare PV/economie autoconsum/venit export,
cu formula si acoperire vizibile) cat si un motor de recomandari pentru ziua
urmatoare (meteo, SOC, contract fix/dinamic, feedback, deduplicare/cooldown,
backtest). Acest PR implementeaza DOAR prima parte -- detalierea financiara,
peste `dashboard_service.get_estimated_savings` (issue #13) si formula unica
de pret din `tariff_service` (issue #46). Motorul de recomandari NU e
implementat deloc (vezi sectiunea "Ramas in afara scopului" mai jos) -- nu
exista nicio recomandare hardcodata/mock in acest PR.

**Trei numere noi, fiecare cu formula lui proprie, NU "economie totala".**
Definitia din issue #49 e explicita: `pret contractual x productie PV` poate
fi afisat ca valoare bruta/cost de cumparare potential evitat, dar NU automat
ca economie totala. `get_estimated_savings` adauga acum, langa cele doua
repere deja existente (`whole_system_benefit_lei`/`ems_incremental_benefit_lei`,
neschimbate):
- `gross_pv_value_lei` = productie PV (kWh) x pret de cumparare efectiv,
  ora-cu-ora -- costul de cumparare POTENTIAL evitat de toata productia PV,
  indiferent daca a fost efectiv autoconsumata, exportata sau pierduta.
- `self_consumption_savings_lei` = min(PV, consum) (kWh) x pret de cumparare
  efectiv, ora-cu-ora -- economia REALA prin autoconsum direct (aproximare
  orara, nu tine cont de decalaje in cadrul orei).
- `export_revenue_lei` = energie exportata (kWh) x pret de export efectiv,
  ora-cu-ora -- venit REAL, deja parte din `actual_net_cost_lei`, expus aici
  separat pentru claritate (nu un numar nou/dublu-numarat).

Fiecare are un camp `_description` cu formula exacta in romana, afisat ca
tooltip in UI (`title` pe eticheta cardului) -- niciun numar nu apare fara
explicatia lui alaturata.

**Acoperire lipsa pentru export, raportata explicit.** Cand exista export
real sau in baseline, dar tariful de export nu are o versiune valabila,
ora este exclusa din sumele comparabile si numarata in
`hours_export_price_missing`; pretul necunoscut nu devine zero. La fel, o
ora cu oricare flux energetic necesar `NULL` este exclusa si raportata prin
`hours_with_incomplete_energy_data`.
Prin urmare, `coverage_ratio` descrie numai orele complet evaluabile, nu
pretinde acoperire pentru intervalele financiare necunoscute.
Aceasta semantica este independenta de rezolutia adaptiva a graficelor:
valorile financiare continua sa foloseasca agregatele orare istorice.

**Provenienta tarifului de import: masurat vs. estimat.** Nu exista in schema
o notiune de tarif "modelat" (o prognoza de pret viitor) -- doar tarif fix
contractual, tarif indexat cu pret PZU real decontat, sau (folosit exclusiv
pentru teste/demo) un fixture sintetic de piata (`ImportRun.is_synthetic_fixture`,
deja existent din issue #35/#51). `_price_provenance_at` (nou,
`dashboard_service.py`) clasifica fiecare ora platita in `fixed_contract`
(pret exact din contract), `indexed_settled` (pret OPCOM real, decontat) sau
`indexed_synthetic` (fixture de test, NU un pret real) -- `tariff_buy_provenance`
raporteaza numarul de ore din fiecare categorie, iar `tariff_provenance_summary`
e `"estimated"` daca ORICE ora foloseste un fixture sintetic, altfel
`"measured"`. Indicatorul e deci binar (masurat/estimat), nu cu trei stari
(masurat/modelat/estimat) din criteriul de acceptare -- pentru ca platforma
nu are inca nicio sursa REALA de "tarif modelat" (ex. o prognoza de pret
contractual viitor); adaugarea uneia ar fi o functionalitate noua, in afara
scopului acestui PR.

**UI.** `dashboard/station.html` capata un card nou "Detaliere valoare PV si
export" cu cele trei numere si un badge de provenienta a tarifului
(`kpi-tariff-provenance`, `badge-ok`/`badge-warn`), populat de
`loadEfcAndSavings` din `dashboard.js` (aceeasi cerere HTTP existenta,
`/stations/{id}/data/savings`, extinsa cu campurile noi -- nicio ruta noua).
Cardurile "Beneficiu sistem PV/baterie" si "Beneficiu incremental EMS"
existente raman neschimbate (aceleasi elemente, acelasi text).

**Teste.** `tests/unit/test_dashboard_service.py` include teste pentru
in fisier) -- productie zero (valoare PV/economie autoconsum = 0), consum
zero (autoconsum 0, valoare PV != venit export, ca sa nu fie confundate),
caz mixt autoconsum+export, interval de pret NEGATIV (valoare PV negativa,
nu trunchiata la 0), provenienta `fixed_contract`/`indexed_settled`/
`indexed_synthetic` (cu `tests.factories.make_market_day(is_synthetic=...)`),
raportarea `hours_export_price_missing` si excluderea energiei incomplete. `tests/integration/
test_dashboard_savings_route.py` (2 teste noi) -- campurile noi ajung in
raspunsul JSON real al rutei `/stations/{id}/data/savings`, si ruta ramane
protejata (403 pentru un utilizator fara acces la statie). Toate cele 9 teste
existente pentru `get_estimated_savings`/alte functii din `dashboard_service`
trec neschimbate (verificat explicit) -- nicio modificare de comportament
pentru `whole_system_benefit_lei`/`ems_incremental_benefit_lei`.

**Ramas in afara scopului (deliberat, nu ascuns -- domeniu de multe zile):**
- **Motorul de recomandari pentru ziua urmatoare** (meteo/ore de soare, PV/
  consum, SOC, contract fix/dinamic, preferinte client) -- NEIMPLEMENTAT.
  Infrastructura de prognoza EXISTA deja (`weather_service`, `pv_forecast_
  service`, `consumption_forecast_service`, folosita de `get_forecast_vs_actual`),
  deci nu e un blocaj total, dar o recomandare demna de incredere mai are
  nevoie de: reguli deterministe explicite pentru fix vs. dinamic (fix: NU
  recomanda mutarea consumului doar din cauza OPCOM; dinamic: explica
  intervalul si diferenta estimata), reason codes, impact estimat, incredere,
  deadline -- niciuna dintre acestea nu exista inca in cod.
- **Feedback "util/nu e relevant" si deduplicare/cooldown** -- necesita un
  model de date nou (persistarea recomandarilor emise + feedback-ul lor) care
  nu exista deloc; adaugarea lui e o schimbare de schema separata, in afara
  scopului acestui PR (care se limiteaza la `dashboard_service`, fara migratii
  noi).
- **"Nu genereaza verdict cand datele sunt insuficiente/stale"** pentru
  recomandari -- moot cat timp nu exista nicio recomandare; regula echivalenta
  pentru detalierea financiara (excluderea orelor fara pret rezolvabil) EXISTA
  deja din issue #13 si e mostenita neschimbata de campurile noi.
- **Backtest/calibrare si teste numerice pentru prognoza gresita** -- nu exista
  un motor de recomandari de calibrat; testele numerice adaugate in acest PR
  acopera doar formulele de detaliere financiara (productie/consum zero,
  pret negativ, tarif lipsa), nu o prognoza meteo/PV gresita.
- **Trei stari masurat/modelat/estimat** pentru provenienta tarifului -- vezi
  mai sus; implementat doar binar (masurat/estimat), pentru ca nu exista o
  sursa reala de "tarif modelat" in schema curenta.

## Addendum: Grafic principal al dashboard-ului (putere + SOC): agregare server-side metric-aware, empty/error state pe widget (issue #33)

**Restul acestui issue era deja acoperit partial.** Sectiunea 18 de mai sus
rezolvase deja agregarea adaptiva pe 3 niveluri pentru graficul de preturi
OPCOM de pe `/market`. Fiecare widget al dashboard-ului statiei avea deja
propriul endpoint si propriul `fetch()` independent (`dashboard.js` apela
separat `/data/timeseries`, `/data/prices`, `/data/plan`, `/data/heatmap`
etc.) -- premisa "un singur payload monolitic blocheaza tot dashboard-ul" nu
mai era adevarata inainte de acest PR. Ramasesera insa doua goluri reale fata
de criteriile explicite ale issue-ului, exact pe graficul cel mai probabil sa
devina lent (putere + SOC, potential un an de telemetrie bruta):
`/stations/{id}/data/timeseries` intorcea intotdeauna rezolutia BRUTA,
indiferent de interval (un an de telemetrie la cateva zeci de secunde/esantion
ar fi insemnat sute de mii de puncte trimise brut in browser), si niciun
widget nu avea o stare de eroare vizibila -- un fetch esuat era doar
`console.error`, fara nicio indicatie pentru utilizator si fara retry.

**Contract de agregare nou, separat de cel al pretului OPCOM (nu s-a
duplicat logica din `market_analytics_service`).** `app/services/
chart_aggregation.py` e un modul PUR (fara acces la DB), reutilizabil:
`choose_resolution(range_key)` aplica exact maparea din contractul issue-ului
-- `24h -> 15m`, `7d -> 30m`, `30d -> 1h`, `1y -> 1d` -- iar `aggregate_series`
grupeaza randuri pe bucket-uri si aplica o metoda de agregare DECLARATA per
coloana (`mean`/`sum`/`min`/`max`). Metric-aware in mod activ, nu doar prin
conventie: functia REFUZA (`ValueError`) o cerere de `sum` pe orice nume de
coloana ce contine `soc`/`pct`/`percent` -- imposibil sa se reintroduca din
greseala bug-ul "SOC insumat" intr-un apel viitor fara ca testele sa pice
imediat. `dashboard_service.get_timeseries_chart` foloseste `mean` pentru
puterile PV/consum/baterie/retea SI pentru SOC (niciodata suma). Fostul
`get_timeseries` (folosit doar de exportul CSV, care are voie sa vrea date
brute) a fost redenumit explicit `get_timeseries_raw` si NU a fost atins.

**Raspunsul HTTP declara explicit rezolutia, agregarea, fusul orar si
acoperirea** -- exact criteriul de acceptare din issue: `{"resolution":
"15m", "aggregation": {"pv_kw": "mean", ..., "soc_pct": "mean"}, "timezone":
"Europe/Bucharest", "coverage": 0.83, "points": [...]}`. `coverage` e
fractia de bucket-uri asteptate in interval care contin date (0 daca nu
exista deloc telemetrie) -- nu pretinde o precizie mai fina decat poate
oferi onest seria selectata. Pentru 24h se folosesc punctele brute recente;
pentru 7d/30d/1y se citesc rollup-urile persistate `interval_15m`/`hour`/
`day`, limitand interogarea la aproximativ 672/720/365 randuri indiferent de
frecventa telemetriei brute. Rollup-ul `day` este delimitat la miezul noptii
locale a statiei si pastreaza corect zilele DST de 23/25 ore. Selectorul a capatat si
optiunea "1 an" (`range=1y`), ca sa existe o cale reala prin UI catre
rezolutia zilnica.
Un test de regresie acopera explicit conversia energiei in putere medie
pentru ziua locala de 23 de ore de la trecerea la ora de vara.
Exportul CSV ramane separat si brut; limitarea cardinalitatii se aplica
doar contractului JSON folosit de grafice.

**Widget independent cu empty/error state, nu doar "console.error".**
`dashboard.js`: graficele de putere si SOC au acum propriile elemente
`.empty-state` / `.error-state` in `station.html` (SOC nu avea deloc
empty-state inainte). O eroare de fetch (timeout, HTTP non-2xx, retea) arata
DOAR acelui widget un mesaj + buton "Reincearca", fara sa afecteze celelalte
grafice de pe pagina. Fiecare fetch are timeout propriu (`AbortController`,
15s) SI e anulat explicit daca utilizatorul schimba intervalul inainte sa
raspunda cererea anterioara (`cancel la schimbarea intervalului`, cerut
explicit de issue) -- un raspuns intarziat al unei cereri deja inlocuite nu
mai apuca sa deseneze peste graficul curent. Un cache scurt (30s, cheie =
URL exacta cu tot cu interval) evita fetch-uri redundante cand ambele
grafice (putere si SOC) cer aceeasi fereastra aproape simultan.

**Teste:** `tests/unit/test_chart_aggregation.py` (rezolutie per range_key;
putere mediata NU insumata; energie poate fi insumata; SOC si orice alta
metrica "procentuala" REFUZA explicit suma -- parametrizat pe mai multe nume
de coloana; bucketing pe mai multe intervale; lipsa de date ramane `None`,
nu devine 0; acoperire calculata corect, inclusiv cazul fara date si cazul
cu esantioane foarte dese intr-un singur bucket). `tests/unit/
test_dashboard_service.py` adauga teste pentru `get_timeseries_chart`
(metadate complete, mediere corecta pe bucket pentru putere si SOC, rezolutie
diferita per interval, `points: []` + `coverage: 0.0` fara nicio telemetrie)
si un test explicit ca `get_timeseries_raw` (exportul CSV) a ramas
neagregat. `tests/integration/test_dashboard_timeseries_route.py` verifica
direct raspunsul HTTP al rutei (metadate, rezolutie zilnica pentru `range=1y`,
empty-state fara date, izolare RBAC intre organizatii).

**Ramas explicit in afara scopului acestui PR (nu e o pretentie de acoperire
completa a issue-ului #33):**

- **Doar 2 din multele grafice ale dashboard-ului** (putere si SOC) au fost
  migrate la contractul de rezolutie/agregare/empty-error-state descris mai
  sus. Celelalte (preturi OPCOM, plan, prognoza PV/consum, heatmap, energii
  zilnice/lunare) raman neschimbate -- fiecare are deja fetch propriu, dar
  fara timeout/cancel/retry explicit si fara metadate de rezolutie in
  raspuns. O migrare completa, widget cu widget, ramane pentru un PR viitor.
- **Fara infrastructura generica de retry controlat** (backoff, numar maxim
  de incercari) -- butonul "Reincearca" e manual, apasat de utilizator, nu
  un retry automat cu backoff exponential.
- **Fara benchmark de latenta pe o baza de productie reala** -- cardinalitatea
  interogarii este acum marginita de rollup-uri pentru ferestrele lungi, iar
  testul DST verifica numeric o zi locala de 23h, dar nu s-a rulat inca un
  EXPLAIN/buget de timp pe volumul real al unei instalatii.
- **Fara reprezentare vizuala distincta pentru stale/estimat/sintetic/gaps**
  -- `is_simulated`/`is_late` sunt calculate per bucket (OR logic) si trimise
  in raspuns, dar `dashboard.js` nu le foloseste inca pentru un stil vizual
  distinct in chart (doar KPI-ul `data_quality` de mai sus, deja existent,
  le reflecta la nivel de statie).
- **Fara eliminare de point-symbols peste un prag configurabil** -- liniile
  foloseau deja `showSymbol: false` dinainte de acest PR; un prag explicit
  configurabil (marker doar sub un numar de puncte) nu a fost adaugat.

## 20. Tabel explicabil si reexecutare controlata a optimizarii (issue #47)

**Scop deliberat restrans.** Issue #47 cere, in specificatia completa, un
flux asincron nou cu idempotenta si job dedicat, diff intre versiuni de
plan, jurnal complet al actorului si teste de conflict/rulare concurenta --
un proiect de mai multe zile. Acest PR NU incearca specificatia completa:
implementeaza doar felia explicabila peste ce exista deja (tabel/coloane
clare, rezumat in limbaj natural, provenance/freshness, confirmare inainte
de a inlocui un plan activ), fara sa atinga deloc modelul matematic
(`optimization_service._solve` ramane neschimbat -- nicio linie modificata).

**Ce exista deja si NU a fost reconstruit.** Jobul asincron cu status,
idempotenta si link catre noul run cerut explicit de criteriile de acceptare
EXISTA deja din issue #11 (`AdminJob` + `admin_optimize_station_job_task`,
vezi limitarea 15 "Joburi admin asincrone") -- `POST
/admin/operations/optimize/{station_id}` deja nu blocheaza pagina, deja
respinge o declansare duplicata pentru aceeasi statie (lock Postgres +
verificare de job activ), iar rularea concurenta la nivel de solver e deja
serializata prin lock Redis + `pg_advisory_xact_lock`
(`OptimizationLockedError`, testat in `tests/unit/test_optimization.py`
inclusiv cu thread-uri reale). Acest PR NU reimplementeaza niciuna dintre
acestea -- doar le leaga vizibil de noul tabel explicabil (link direct catre
`/admin/operations/optimization-runs/{run_id}` din lista de joburi si din
lista de rulari).

**Ce s-a adaugat efectiv:**
- `app/services/optimization_view.py` -- view-model PUR (fara DB/solver):
  `classify_action` clasifica fiecare interval intr-o actiune de baza
  (incarca din PV / incarca din retea / descarca / exporta / importa /
  mentine), `build_rows` adauga un "motiv" euristic (rezerva minima de SOC,
  plafon SOC, pret, echilibru cerere-oferta) si costul/beneficiul net al
  intervalului, iar `group_segments` grupeaza intervale consecutive cu
  aceeasi actiune SI acelasi motiv intr-un segment rezumat in limbaj
  natural. **Motivul e o euristica pe datele deja publicate, NU o extragere
  a multiplicatorilor Lagrange reali ai solverului** -- planul, o data
  publicat, nu mai poarta acea informatie; documentat explicit in docstring
  si in UI.
  Costul unui interval/segment ramane explicit necunoscut daca lipseste
  tariful necesar fluxului efectiv; un pret absent nu este inlocuit cu zero.
- `GET /admin/operations/optimization-runs/{run_id}` -- pagina noua cu:
  legenda coloanelor (definitie + unitate + conventie de semn explicita
  pentru baterie/retea, `+`/`-`), sectiunea de provenance/freshness
  (SOC folosit ca punct de start si calitatea lui masurat/invechit/lipsa,
  acoperirea prognozelor PV/consum, cate intervale de pret sunt estimate
  vs. reale, versiunile de configuratie/preferinte folosite), rezumatul pe
  segmente si tabelul detaliat pe interval. Pentru o rulare `fallback`
  (date insuficiente), pagina explica EXPLICIT motivul
  (`OptimizationRun.fallback_reason`, deja existent) in loc sa arate un
  tabel gol fara context.
- `GET /admin/operations/optimize-confirm?station_id=...` -- pasul de
  confirmare cerut de issue: arata ce configuratie/preferinte vor fi
  folosite, modul shadow/live curent al statiei si, daca exista, planul
  activ care ar fi marcat `superseded`. `POST
  /admin/operations/optimize/{station_id}` respinge acum server-side
  (`error=optimization_confirmation_required`, fara sa creeze niciun
  `AdminJob`) o reexecutare fara `confirmed=true` STRICT cand exista deja
  un plan activ -- o statie fara plan activ (prima rulare) nu are nimic de
  pierdut si ramane neschimbata (verificat explicit,
  `test_trigger_optimization_without_active_plan_does_not_require_confirmation`).
  Un camp optional "motiv" e retinut in `AdminJob.params` si in
  `record_audit` -- nu schimba executia, doar imbogateste jurnalul actorului.

**Ramas explicit in afara scopului acestui PR (nu ascuns):**
- **Diff intre versiunea noua si cea precedenta a planului.** Pagina de
  detaliu arata un singur run/plan izolat, nu o comparatie randuri-cu-randuri
  intre `Plan v(n-1)` si `Plan v(n)`. Ar necesita alinierea a doua orizonturi
  posibil diferite (start/durata) si o reprezentare vizuala dedicata --
  proiect separat.
- **Jurnal complet al actorului la nivel de UI**, dincolo de audit log-ul deja
  existent (`record_audit`, vizibil in `/admin/audit`). Nu s-a construit o
  vedere dedicata "istoricul deciziilor asupra acestui plan" in pagina de
  detaliu.
- **Conflict de rulare concurenta prin UI-ul admin, testat explicit la acest
  nivel.** Serializarea reala (Redis + advisory lock la nivel de statie) e
  deja acoperita de teste existente la nivelul serviciului
  (`tests/unit/test_optimization.py`, thread-uri reale). Acest PR NU adauga
  un test HTTP separat de "doua cereri POST simultane catre ruta admin" --
  ar exercita aceeasi cale deja testata, prin `_lock_admin_job_target` +
  verificarea de job activ (deja acoperite in
  `test_trigger_optimization_rejects_duplicate_in_progress`).
- **Enforcement server-side mai puternic al confirmarii** (ex. un token
  legat de versiunea exacta a planului activ vazuta pe pagina de
  confirmare, care sa expire sau sa devina invalid daca planul activ se
  schimba intre timp). Verificarea actuala e binara (exista sau nu un plan
  activ) -- suficienta pentru a preveni un click accidental din lista, dar
  nu o garantie criptografica ca admin-ul a vazut EXACT starea curenta.
- **Reason codes structurate** (enum) pentru motivul dominant, in loc de
  text liber generat de `_dominant_reason` -- suficient pentru un om, dar
  nu usor de filtrat/agregat programatic peste multe rulari.
- **Modificarea modelului matematic** -- nu a fost cautat si nu a fost gasit
  niciun bug de solver in cadrul acestui PR; `optimization_service._solve`
  e neschimbat linie cu linie.
## 21. Design system minim: breadcrumb, grupuri de campuri, focus pe eroare (issue #48)

Issue #48 cerea un "design system" pentru UI -- domeniu larg, care poate
insemna orice, de la un ghid de stil complet cu componente reutilizabile pana
la teste de regresie vizuala automate. Ce s-a implementat efectiv, cu scop
explicit limitat la ce era fezabil si verificabil in acest repo:

- **Breadcrumb semantic** (`partials/_breadcrumb.html`, macro `trail`) adaugat
  pe 8 pagini (configurare, preferinte, dispozitive, tarife, configurare
  invertor, dashboard statie, detaliu organizatie self-service, detaliu
  organizatie admin) -- `<nav aria-label="breadcrumb">` cu `aria-current="page"`
  pe elementul curent, link-uri construite EXCLUSIV din ID-uri deja
  autorizate din context (niciodata din query-uri neverificate), verificat
  cu test dedicat ca nu exista risc de open-redirect.
- **Grupuri de campuri corelate** (`_field_group.html`) -- `<fieldset>`
  semantic cu `<legend>`, folosit pentru a grupa vizual campuri care se
  citesc impreuna (locatie lat/lng, sistem PV/invertor, capacitate/putere
  baterie, SOC).
- **Input numeric cu sufix de unitate** (`_numeric_input.html`) -- sufixul
  (`kW`, `kWh`, `%`) e strict decorativ (`aria-hidden`, in afara `name`-ului
  campului), nu modifica valoarea trimisa la server.
- **Progressive disclosure** prin `<details>/<summary>` native pentru
  campuri avansate/rar-modificate (limite retea, preferinte flexibile EV).
- **Focus + evidentiere pe primul camp invalid dupa un submit respins**
  (`form-errors.js`) -- contract HTML generic (`data-error-summary`/
  `data-error-field`), functioneaza pe orice pagina care foloseste macro-ul
  `_form_errors.html`, verificat atat cu teste de integrare (maparea
  `data-error-field` -> `name`) cat si cu un test Playwright pe browser real
  (`document.activeElement`, imposibil de verificat doar din HTML static).

**Explicit in afara scopului acestei implementari** (nu exista infrastructura
in acest repo si nu a fost construita acum, ca sa nu se pretinda o acoperire
care nu exista):

- **Teste de regresie vizuala/snapshot** (comparatie pixel-cu-pixel intre
  randari) -- nu exista in acest repo (nici pentru codul preexistent). Ce
  exista sunt capturi de ecran facute manual in timpul dezvoltarii pentru
  verificare vizuala punctuala si teste Playwright care verifica marcaj/
  comportament (prezenta claselor, focus, continut), nu aspectul vizual
  pixel-cu-pixel.
- **Ghid de stil/catalog de componente formal** (ex. Storybook sau
  echivalent) -- componentele noi sunt macro-uri Jinja documentate prin
  comentarii, nu un catalog navigabil separat.
- **Acoperire completa a tuturor paginilor** -- breadcrumb-ul si grupurile de
  campuri au fost aplicate pe paginile de configurare/preferinte/admin cele
  mai relevante (unde exista formulare cu mai multe campuri corelate), nu
  literal pe fiecare pagina din aplicatie (ex. paginile de listare simple nu
  au fost modificate, intrucat nu au campuri de grupat).

## Addendum: Dashboard client "one station first", felie limitata (issue #45)

**Domeniul acestui PR e strict felia de rutare + explicatii, NU refacerea
completa a informatiei arhitecturale a dashboard-ului** ceruta de issue --
acel domeniu complet (comparatie fata de ieri pentru fiecare KPI, skeleton
per widget, limbaj complet non-tehnic peste tot, un audit de accesibilitate)
e un proiect de mai multe zile; issue-ul insusi cere sa nu se rescrie
backend-ul energetic, iar `dashboard_service`/`dashboard.js` au fost deja
extinse semnificativ de #13/#18/#33/#49/#50 -- acest PR se adauga la ele, nu
le inlocuieste.

**Ce s-a implementat:**

1. **Redirect automat "o singura statie" (`app/web/routes/dashboard.py`,
   `home()`).** Cand un utilizator autentificat NON-admin de platforma are
   acces la exact O statie si nu a cerut explicit alta (`station_id` lipseste
   din query), `GET /` face 302 direct catre URL-ul relativ `/?station_id=<statia lui>` (fara a reflecta headerul `Host`, comportament acoperit de test de regresie) --
   clientul NU mai vede o pagina intermediara cu un selector cu o singura
   optiune. Cu 0 statii, ramane empty state-ul explicativ existent
   (`dashboard/no_station.html`, neschimbat). Cu 2+ statii, comportamentul
   ramane identic celui dinainte (pagina de alegere / selector din navbar).
2. **Administratorii de platforma sunt exclusi explicit din acest
   auto-redirect.** `build_nav_context` le arata TOATE statiile din sistem
   (nu doar ale lor) -- daca sistemul are, la un moment dat, o singura statie
   inregistrata, asta nu inseamna ca admin-ul e "clientul cu o singura
   statie" din issue; ar fi fost teleportat implicit intr-o statie oarecare,
   posibil a altcuiva. Verificat explicit
   (`test_platform_admin_not_auto_redirected_with_single_system_station`).
3. **Selectorul multi-statie din navbar (`partials/_nav.html`) apare DOAR
   cand exista mai mult de o statie.** Cu exact o statie, navbar-ul arata
   numele ei ca text simplu (nimic de "selectat"); cu zero, nu arata nimic
   in acel loc. Inainte de acest PR, dropdown-ul cu o singura optiune plus
   placeholder-ul "Selecteaza statia..." aparea intotdeauna, indiferent de
   numarul de statii.
4. **"Cum se calculeaza?" (`<details>`/`<summary>`, fara JavaScript nou)**
   adaugat pe cele mai opace 4 KPI-uri de pe dashboard, NU pe toate ~15
   widget-urile: cost efectiv import, venit efectiv export (text static,
   formula din `tariff_service.compute_effective_price_lei_per_kwh`, plus
   link catre `/stations/{id}/tariffs`) si cele doua KPI-uri de beneficiu
   (Beneficiu sistem PV/baterie, Beneficiu incremental EMS -- text static ce
   descrie metodologia celor doua repere din `dashboard_service.get_estimated_savings`,
   DISTINCT de nota dinamica `kpi-savings-note`/`kpi-ems-benefit-note` deja
   populata de `dashboard.js` din raspunsul API existent (#13), care ramane
   neschimbata). Nicio cifra de economie/recomandare noua nu a fost
   inventata -- acest PR doar explica in cuvinte formulele deja calculate
   de codul existent.

**De ce nu un redirect si pentru RBAC/rolul de membership.** Testele acopera
explicit viewer/organization_admin ajungand direct pe dashboard cu o singura
statie (comportamentul de rutare nu depinde de rol) si un utilizator FARA
niciun membership, care nu vede/atinge nicio statie a altei organizatii
(`test_no_membership_user_not_redirected_into_unrelated_station`) -- izolarea
RBAC insasi (`build_nav_context`, `StationAccess`) nu a fost modificata,
doar exercitata de testele noi.

**Empty state / freshness / calitate date -- deja acoperite, nu duplicate
aici.** `#kpi-quality` (masurat/estimat/simulat/invechit/lipsa),
`#sse-status` (conectare/live/stale/offline) si KPI-urile afisand "-" in loc
de un zero fals cand lipsesc date sunt deja livrate de #13/#18/#50 si au
ramas neschimbate -- verificat ca suita completa (413 teste, minus 1
deselectat, nelegat) trece neschimbata dupa acest PR.

**Ramas explicit in afara scopului (nu ascuns):**
- Comparatie "mai mult/mai putin decat ieri/perioada comparabila" pentru
  FIECARE KPI de pe pagina -- ar necesita o sursa de agregate istorice
  comparabile per-metrica si o decizie explicita despre cand "insuficiente
  date" trebuie sa opreasca orice verdict; niciun calcul de acest fel nu a
  fost adaugat in acest PR.
- Skeleton loader per widget si o revizuire completa a "layout shift"-ului
  la incarcare -- graficele principale (`echarts`) si empty state-urile lor
  (#33/#50) raman neschimbate; nu s-a adaugat un schelet vizual per card KPI.
- O reorganizare completa a ierarhiei vizuale (grafice secundare "compacte,
  progresive, ordonate dupa utilitatea clientului" intr-o zona avansata
  distincta) -- ordinea si gruparea actuala a cardurilor din
  `dashboard/station.html` nu a fost restructurata, doar cele 4 KPI-uri de
  mai sus au primit disclosure-uri noi.
- "Cum se calculeaza?" pe restul KPI-urilor (PV, consum, retea, SOC, putere
  baterie, EV, automatizare, EFC) -- acestea sunt fie masuratori brute directe
  (nu au o "formula" de explicat), fie deja documentate de sectiuni anterioare
  din acest fisier; nu s-a adaugat disclosure pe ele in acest PR.
- Un audit complet de accesibilitate (focus vizibil, ordine de tab, roluri
  ARIA pe grafice) -- neatins in acest PR, in afara de faptul ca
  `<details>`/`<summary>` sunt native, deci focusabile si utilizabile de
  tastatura fara JavaScript suplimentar.
## 21. Meteo/PV versionat -- rasarit/apus reale si backtesting MAE/bias (issue #53)

Issue #53 cere o re-arhitecturare ampla (evaluare formala de provider,
worker cu retry/backoff/observabilitate, backtesting complet). Cea mai mare
parte a infrastructurii de baza EXISTA DEJA in acest repo (verificat explicit
inainte de a scrie cod nou, ca sa nu se reconstruiasca ce functioneaza):

**Deja existent, verificat, neschimbat:**
- **Provider ales si documentat.** `app/services/weather_service.py` +
  `app/config.py` (`weather_provider="open-meteo"`, `weather_base_url`)
  foloseste deja Open-Meteo (fara autentificare, gratuit pentru uz
  necomercial, acoperire globala inclusiv Romania) -- alegerea e documentata
  in limitarea 2 de mai sus. Fallback controlat: `WeatherUnavailableError`
  se propaga explicit pana in optimizator/UI, fara date inventate.
- **Prognoza deja versionata cu `issued_at`.** `WeatherForecast`/
  `PvForecast`/`ConsumptionForecast` au deja `issued_at`, `source`,
  `source_version`, `confidence`, `is_synthetic` (`app/models/forecast.py`)
  -- fiecare rulare a importului creeaza un batch nou, niciodata suprascris.
- **Look-ahead deja prevenit pentru istoric.** `dashboard_service.
  get_forecast_vs_actual` alege deja, pentru fiecare `interval_start`, doar
  cea mai recenta prognoza cu `issued_at <= interval_start` -- fix aplicat in
  issue #13 (limitarea 17 de mai sus), verificat aici ca ramane corect si
  reutilizat ca principiu (nu ca import direct) in noul modul de backtesting.
- **Fetch in background, deja pe worker existent, la fiecare 30 minute.**
  `app/workers/tasks.py::weather_and_forecast_task` (Celery beat, issue #10)
  ruleaza deja meteo + PV + consum pentru toate statiile active, cu lock
  Redis anti-suprapunere; ruta web nu asteapta niciodata providerul.

**Adaugat de acest PR (gap real, nu acoperit inainte):**
- **Rasarit/apus/ore utile de soare, calculate real, nu aproximate.**
  `app/services/solar_geometry_service.py` (nou) foloseste algoritmul SPA din
  `pvlib` (deja dependinta a platformei) pe latitudine/longitudine REALE ale
  statiei, intotdeauna in UTC -- fara nicio migratie de schema (rasaritul e
  calculabil determinist din data+coordonate, nu are nevoie sa fie
  persistat/versionat ca prognoza meteo propriu-zisa, care depinde de un
  provider extern). Conversia in ora LOCALA foloseste `zoneinfo` (DST corect
  automat) -- testat explicit pe ambele treceri DST din 2026 ale Romaniei
  (28->29 martie si 24->25 octombrie): ora UTC a rasaritului nu sare
  (continuitate fizica), dar reprezentarea ei LOCALA sare cu ~1 ora, exact
  cum ar trebui. `pv_forecast_service.generate_pv_forecast` foloseste acum
  aceasta fereastra ca o plasa de siguranta suplimentara: orice interval din
  afara ferestrei reale de lumina e clampat explicit la 0 kW, indiferent de
  o eventuala valoare mica/nenula de iradianta raportata de sursa meteo
  langa amurg/rasarit (artefact de medie orara).
- **Backtesting MAE/bias pentru prognoza PV.**
  `app/services/forecast_backtest_service.py` (nou) calculeaza eroarea medie
  absoluta (MAE) si bias-ul (eroare medie semnata: prognoza - real) intre
  prognoza PV "asa cum era cunoscuta la momentul respectiv" (aceeasi regula
  anti-look-ahead ca mai sus) si productia PV masurata (telemetrie agregata
  la 15 minute), pentru o statie si un interval date. Un interval fara
  prognoza validă sau fara telemetrie suficient de acoperita
  (`coverage['pv'] >= 0.9`) e raportat separat (`n_missing_forecast`/
  `n_missing_actual`), NICIODATA tratat ca eroare zero.
- **Teste deterministe noi**, toate cu fixtures explicite (nicio dependinta
  de ceasul real sau de un provider extern):
  `tests/unit/test_solar_geometry_service.py` (ambele treceri DST 2026,
  durata zilei vara/iarna, `is_daylight` la amiaza/miezul noptii),
  `tests/unit/test_pv_forecast_service.py` (clamp la 0 in afara ferestrei de
  lumina reale, eroare explicita fara meteo), `tests/unit/
  test_forecast_backtest_service.py` (MAE/bias pe fixture cunoscut,
  excluderea explicita a unei prognoze "din viitor" -- look-ahead --,
  raportarea separata a lipsei de prognoza/telemetrie/acoperire
  insuficienta).
  Intervalul cerut este semi-deschis `[start, end)`, trebuie sa fie
  timezone-aware si aliniat exact la 15 minute, pentru ca numarul de
  esantioane asteptate sa nu fie aproximat; limitele naive sau decalate sunt
  refuzate explicit.

**Ramas explicit in afara scopului acestui PR (documentat, nu ascuns):**
- **Interfata provider-agnostica formala** (un `Protocol`/clasa abstracta
  peste care s-ar putea plugini alt provider decat Open-Meteo) -- adaptorul
  actual e un singur modul concret; o abstractizare completa, cu al doilea
  provider real implementat si testat, ramane de facut cand exista un motiv
  concret sa schimbam providerul (ex. limita de rate atinsa in productie).
- **Worker retry/backoff/observabilitate dedicate.** `weather_and_forecast_task`
  ruleaza deja pe Celery beat (issue #10), dar o eroare per-statie e doar
  colectata intr-o lista si logata -- nu exista inca retry cu backoff
  exponential per provider, rate-limiting explicit catre Open-Meteo, sau
  metrici de observabilitate (latenta/rata de succes) expuse separat.
- **Backtesting complet (dashboard, segmentare pe conditii meteo, pret).**
  `forecast_backtest_service.py` e strict minimal -- MAE/bias pe puterea PV,
  fara UI, fara segmentare senin/inorat, fara metrici pe prognoza de consum
  sau de pret. Suficient sa dovedeasca ca metodologia e corecta si sa
  inceapa masurarea reala, nu un panou de raportare complet.
- **Optimizer/recomandari care sa consume explicit "ore utile de soare"
  ramase azi.** `solar_geometry_service.py` e integrat direct doar in
  `pv_forecast_service` (clamp de siguranta); niciun modul de "recomandari
  pentru maine" nu exista inca in acest repo (nu doar in afara acestui PR --
  nu exista deloc), deci nu exista inca un consumator pentru aceasta
  informatie in afara prognozei PV.
- **Control HVAC** -- exclus explicit de prompt-ul issue-ului, neatins.

## Addendum: Tip de contract explicit, izolare import/export si teste DST/rotunjire (issue #46, a doua iteratie)

Issue #46 a fost re-specificat mai detaliat dupa ce prima iteratie (vezi
sectiunea "Contracte tarifare: componente de cost distincte, TVA explicit,
preview numeric" de mai sus, PR #56) acoperise deja versionarea cu
valabilitate, componentele de cost distincte (energie/distributie/transport/
alte taxe reglementate/TVA/cost fix), separarea marginal-fix, formula
explicita de indexare OPCOM+marja, blocarea calculului la pret OPCOM lipsa
(fara fallback tacut) si preview-ul numeric pe pagina de tarife. Aceasta
iteratie a verificat concret ce mai lipsea fata de noul text al issue-ului si
a adaugat DOAR gaurile reale gasite:

**`Tariff.kind` guverneaza acum efectiv formula, nu doar eticheta afisata.**
Inainte, `compute_effective_price_lei_per_kwh` alegea ramura fix/indexat
dupa care camp (`fixed_price_lei_per_kwh` / `opcom_margin_lei_per_kwh`) era
completat, IGNORAND complet `kind` -- un tarif etichetat "indexat OPCOM"
caruia i s-ar fi completat din greseala si `fixed_price_lei_per_kwh` s-ar fi
comportat tacit ca fix. `app/models/tariff.py` defineste acum
`TARIFF_KIND_FIXED`/`TARIFF_KIND_DYNAMIC_INDEXED`/`TARIFF_KINDS`, iar
`tariff_service.validate_tariff_kind` (apelat din `get_or_create_tariff`)
respinge orice `kind` in afara acestor doua valori. `add_tariff_version`
valideaza acum, la SCRIERE, ca versiunea noua are EXACT campurile care
corespund tipului declarat -- `kind=fixed` cere `fixed_price_lei_per_kwh` si
interzice `opcom_margin_lei_per_kwh`; `kind=indexed_opcom` cere
`opcom_margin_lei_per_kwh` si interzice `fixed_price_lei_per_kwh` -- esec
explicit (`ValueError`), niciodata o alegere tacita intre cele doua campuri.
Exceptia explicita este o versiune cu `economic_calculation_disabled=True`:
pretul corespunzator tipului poate lipsi, deoarece platforma declara ca nu
poate calcula acea formula si va returna un rezultat necunoscut; campul de
pret al celuilalt tip ramane interzis.
Ruta `/stations/{id}/tariffs` (POST) prinde aceasta eroare si o afiseaza in
formular (macro-ul `_form_errors.html`, issue #48), fara sa creeze niciun
rand nou -- nu doar teste de model, comportament HTTP verificat capat-la-cap.
Tipul unui contract existent nu poate fi schimbat in loc: asta ar
reclasifica retroactiv toate versiunile istorice, deoarece `kind` apartine
in prezent lui `Tariff`, nu lui `TariffVersion`. Serviciul refuza explicit
tranzitia pana cand ea va fi modelata ca inchiderea contractului vechi si
crearea unuia nou, fara pierderea istoricului.
"provider"/"custom" din textul issue-ului raman doar etichete libere in
`Tariff.name`, nu tipuri de calcul distincte -- niciunul nu are o formula
proprie implementata (nu exista o formula "de provider" verificata de
adaugat fara sa fie inventata, vezi sectiunea de mai jos).

**Independenta import/export, verificata explicit cu test, nu doar presupusa
din arhitectura.** Directia (`import`/`export`) era deja un `Tariff` separat
inainte de acest PR (nicio schimbare de schema aici), deci exportul avea deja
structural propriul pret/formula, fara sa scada componente de import. Ce
lipsea era un test care sa demonstreze asta explicit (criteriul din issue:
"componentele nerecuperabile nu sunt scazute fictiv din import") --
`test_export_price_is_independent_of_import_components` creste drastic
componentele de import (distributie, abonament) DUPA calcularea pretului de
export si verifica ca pretul de export ramane identic, exact regresia pe
care o cere issue-ul daca cineva ar "optimiza" vreodata cele doua formule sa
partajeze cod.

**Teste DST adaugate -- niciunul nu exista inainte pentru versionarea
tarifelor.** `valid_from`/`valid_to` sunt deja `DateTime(timezone=True)`,
comparate ca instante absolute UTC (nicio schimbare de cod necesara), dar
issue-ul cere explicit teste numerice pentru tranzitiile de ora de vara/
iarna. `test_tariff_version_resolves_correctly_across_spring_forward_dst`
si `..._fall_back_dst` verifica `get_current_tariff_version` chiar la
instanta UTC a tranzitiei din Europe/Bucharest pentru 2026 (29 martie -- ora
locala 03:00-04:00 nu exista deloc; 25 octombrie -- ora locala 03:00-04:00
se repeta), confirmand ca rezolutia pe instanta absoluta nu produce o
selectie ambigua sau gresita a versiunii in niciunul din cele doua cazuri.

**Test de rotunjire cu Decimal.** `test_decimal_precision_avoids_float_rounding_drift`
insumeaza componente la limita de precizie a coloanei `NUMERIC(10,5)`
(inclusiv o zecimala a cincea nenula) si verifica rezultatul exact -- Decimal
nu introduce drift binar-float (ex. suma nu devine `0.30000999...`).

**Teste:** `tests/unit/test_tariff_service.py` include validarea `kind`
necunoscut, blocarea reclasificarii istoricului, 4 combinatii fix/dinamic cu camp
lipsa/strain, o regresie ca versiunile corect formate tot trec, independenta
export/import, 2 teste DST, 1 test de rotunjire). `tests/integration/
test_tariffs_routes.py` are acum 6 (4 preexistente + 2 noi: contract fix cu
marja straina si contract dinamic fara marja sunt respinse cu eroare
afisata in formular si NU salveaza niciun rand).

**Ramas in afara scopului (deliberat, nu ascuns, neschimbat fata de prima
iteratie -- vezi si sectiunea de mai sus):**
- **Wizard-ul dedicat cu preseturi explicabile** (issue #41) -- la momentul
  acestui PR, #41 inca nu era mergeat pe `main` (dezvoltat in paralel, pe
  `feature/setup-wizard-issue-41`). Configurarea ramane prin pagina de
  tarife existenta (`/stations/{id}/tariffs`, extinsa acum cu validarea de
  tip de contract si un rezumat de erori consistent cu restul aplicatiei),
  NU o experienta ghidata pas-cu-pas cu preseturi "explicabile" (ex. "Enel
  standard", "OMV Petrom dinamic") -- niciun asemenea preset comercial
  verificat nu exista in acest cod, ca sa nu fie inventat. Cand #41 va fi
  mergeat, pagina de tarife ramane reutilizabila ca pas de wizard (acelasi
  serviciu `tariff_service`, aceleasi validari), dar integrarea explicita
  (navigare pas-cu-pas, preseturi) e responsabilitatea acelui issue.
- **Reguli legale/comerciale romanesti verificate** (cote OPANAF/ANRE reale,
  formule reglementate de distributie/transport, preseturi de furnizor) --
  deliberat NEINVENTATE, neschimbat fata de prima iteratie: operatorul
  introduce valorile reale din contractul/factura lui.
- **Constrangere la nivel de baza de date (CHECK constraint)** pentru
  consistenta `kind`/campuri -- validarea noua e doar la nivel de serviciu
  (`tariff_service`), singurul punct de scriere folosit de aplicatie; un
  `INSERT` SQL direct in `tariff_versions`, ocolind `add_tariff_version`,
  tot ar putea crea o versiune inconsistenta. Nu exista alt cod in acest
  repo care sa scrie in acest tabel altfel decat prin acest serviciu.
- **Decontare neta ora-cu-ora in preview, formula "provider"/"custom"
  distincta** -- neschimbate fata de prima iteratie (vezi mai sus).
