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
