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
