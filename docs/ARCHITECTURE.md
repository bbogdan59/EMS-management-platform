# Arhitectura si decizii tehnice

## Vedere de ansamblu

Monolit modular Python, o singura baza de cod (`app/`), trei tipuri de
proces la runtime:

- **web** (`uvicorn`/`gunicorn` + `app.main:app`) -- FastAPI, servite UI
  server-side (Jinja2/HTMX) si API-ul public de dispozitive (`/api/v1`).
- **worker** (`celery -A app.celery_app worker`) -- executa taskurile
  asincrone (agregare telemetrie, import OPCOM, prognoze, optimizare,
  dispatch comenzi, alerte, retentie).
- **scheduler** (`celery -A app.celery_app beat`) -- planifica taskurile de
  mai sus. **O singura instanta** ruleaza acest proces (nu se scaleaza) --
  altfel joburile s-ar dubla.

Toate trei impart acelasi cod si aceeasi baza PostgreSQL/Redis, dar ruleaza
in containere/procese separate (vezi `docker-compose.yml` / Railway).

```
                 +-------------------+
   Browser  ---> |   web (FastAPI)   | <---> PostgreSQL
                 +-------------------+          ^
                         ^                       |
   Dispozitiv ---------> | /api/v1               |
   (extern, viitor)      +-----------------------+
                         v
                 +-------------------+
                 |  worker (Celery)  | <---> Redis (broker/backend, rate limit, cache, lock-uri)
                 +-------------------+
                         ^
                 +-------------------+
                 | scheduler (Beat)  |  (o singura instanta)
                 +-------------------+
```

## Module principale

| Modul | Responsabilitate |
|---|---|
| `app/models` | SQLAlchemy 2, 29 tabele (vezi mai jos) |
| `app/core` | securitate (Argon2, token-uri), CSRF, RBAC, rate limiting, audit, email |
| `app/api/deps.py` | dependinte FastAPI pentru auth/RBAC pe partea web (sesiune cookie) |
| `app/api/v1` | API-ul public de dispozitive (autentificare Bearer separata) |
| `app/web` | rute HTML + template-uri (Jinja2), SSE |
| `app/services` | logica de business, testabila independent de FastAPI |
| `app/workers/tasks.py` | taskuri Celery, idempotente, cu lock Redis |
| `simulator/` | client HTTP independent, vorbeste doar cu `/api/v1` |
| `scripts/seed_demo.py` | seed privilegiat (acces DB direct) pentru modul demo |

## Modelul de date (rezumat)

29 de tabele, printre care:

- `users`, `organizations`, `memberships` -- multi-tenant: un user poate
  apartine mai multor organizatii, cu rol diferit in fiecare.
- `stations`, `station_config_versions`, `panel_groups` -- o statie are un
  istoric complet, imutabil, de configuratii tehnice (fiecare editare
  creeaza o versiune noua, niciodata UPDATE in loc).
- `preference_versions` -- la fel, versionat; separa explicit constrangerile
  obligatorii de preferintele flexibile (vezi docstring-ul modelului).
- `devices`, `device_credentials`, `claim_codes` -- ciclul de viata al
  asocierii unui dispozitiv.
- `telemetry_raw`, `telemetry_aggregates` -- date brute deduplicate +
  agregate pe 15min/ora/zi/luna (retentie configurabila separata pentru
  fiecare).
- `tariffs`, `tariff_versions` -- tarife cu valabilitate, separate pe
  import/export, cu distinctie explicita fix vs. indexat OPCOM.
- `market_price_intervals`, `import_runs` -- preturi PZU cu revizii,
  idempotente, cu raw response + hash pastrate pentru audit.
- `weather_forecasts`, `pv_forecasts`, `consumption_forecasts` -- prognoze
  cu moment de emitere, sursa, versiune si nivel de incredere separate de
  valoarea propriu-zisa.
- `optimization_runs`, `plans`, `plan_intervals` -- separarea ceruta intre
  scenariul calculat, planul publicat si intervalele lui.
- `commands`, `command_events` -- comenzi semantice cu jurnal imutabil de
  tranzitii.
- `alerts`, `audit_logs` -- monitorizare si audit.

### De ce NUMERIC (Decimal) si nu FLOAT pentru bani/energie

Toate valorile monetare (`lei`) si de energie (`kWh`) folosesc coloane
`NUMERIC` (mapate pe `decimal.Decimal` in Python), niciodata `FLOAT`. Motiv:
aceste valori se **insumeaza** pe mii de intervale de 15 minute, pe luni de
zile, in agregate si in rapoarte financiare -- erorile de rotunjire binara
specifice float-urilor s-ar acumula si ar produce diferente vizibile si
inconsistente in functie de ordinea operatiilor. Puterile instantanee (kW/W)
folosesc de asemenea `NUMERIC` pentru consistenta cu energia (sunt afisate
impreuna in aceleasi grafice si comparate direct in "prognoza vs. realizat").

Prognozele meteo brute (radiatie, temperatura, viteza vant) folosesc
`FLOAT`: sunt marimi fizice continue, aproximative prin definitie, nu se
insumeaza financiar si nu au nevoie de precizie zecimala exacta -- pretul in
folosirea NUMERIC acolo ar fi cosmetic, nu ar aduce beneficiu real.

## EFC (cicluri echivalente) -- definitie si conventii

`EFC = energia cumulata descarcata la nivelul bateriei / capacitatea de
referinta a bateriei`. Energia descarcata e masurata pe partea DC a
bateriei (raportata de dispozitiv ca `battery_power_w < 0`); conversia in AC
(ce se vede la contor) implica randamentul de descarcare, documentat separat
in `StationConfigVersion.battery_discharge_efficiency`. Capacitatea de
referinta (`battery_reference_capacity_kwh`) e valoarea nominala de la
instalare si NU se actualizeaza automat -- degradarea reala se reflecta in
`battery_available_capacity_kwh`, editabila manual de un administrator.

## Autentificare si autorizare

- Parole: Argon2 (`argon2-cffi`), cu re-hash automat la login daca
  parametrii impliciti s-au schimbat.
- Sesiuni: token opac generat cu `secrets.token_urlsafe`, doar hash-ul
  (SHA-256) e stocat in baza de date; cookie `HttpOnly`, `Secure` in
  productie, `SameSite=Lax`. Sesiunile sunt revocabile individual sau in
  masa (ex. la resetarea parolei).
- CSRF: cookie cu verificare dubla (double-submit). Middleware-ul genereaza
  tokenul **inainte** de randarea paginii (nu dupa), astfel incat formularul
  afisat la prima vizita a unui utilizator sa contina deja tokenul corect
  (bug prins si corectat printr-un test Playwright real -- vezi
  `tests/e2e/test_ui_flows.py`).
- RBAC: patru roluri (`platform_admin`, `organization_admin`, `operator`,
  `viewer`), verificate **pe server** pentru fiecare resursa (inclusiv SSE,
  export CSV, joburi, comenzi) prin dependinte FastAPI dedicate
  (`app/api/deps.py`), nu doar ascunse in UI.
- Rate limiting: fereastra fixa in Redis, pentru login si pentru API-ul de
  dispozitive (per IP, respectiv per `device_id`).

## Motorul de optimizare

Vezi docstring-ul din `app/services/optimization_service.py` pentru detalii
complete. Rezumat:

- Model Pyomo, rezolvat cu HiGHS (`appsi_highs`), orizont implicit 36h,
  interval de 15 minute.
- Variabile: import/export retea, incarcare/descarcare baterie, SOC, incarcare EV,
  doua perechi de variabile binare (Big-M) care interzic explicit incarcarea
  si descarcarea simultana a bateriei, respectiv importul si exportul
  simultan.
- Constrangeri obligatorii: limite SOC, limite de putere, permisiuni
  incarcare din retea / export din baterie, buget EFC zilnic si lunar
  (lunar calculat ca "buget ramas" folosind consumul deja inregistrat in
  luna curenta).
- Preferinte flexibile (penalizate in functia obiectiv, nu constrangeri
  dure): tinte SOC recurente, necesarul de energie EV pana la ora de
  plecare, prioritate (cost/autonomie/protejarea bateriei -- implementata ca
  ponderi diferite pe termenii din obiectiv).
- Valoare pentru energia ramasa la final: un termen terminal in obiectiv,
  proportional cu pretul de import curent -- evita ca optimizatorul sa
  "goleasca" bateria doar pentru ca orizontul se termina.
- Fallback conservator: daca lipsesc complet prognozele, daca solverul
  raporteaza infezabil/timeout, sau apare orice eroare neasteptata, se
  publica un plan de asteptare (baterie in hold, fara actiune), niciodata un
  plan bazat pe presupuneri riscante. Motivul e stocat explicit
  (`OptimizationRun.fallback_reason`) si afisat in panoul de administrare.
- Lock per statie (Redis) impotriva rularilor concurente.
- Modul `shadow` (implicit) -- planul e calculat si afisat, dar
  `Plan.execution_mode` ramane `shadow`; comenzile catre dispozitiv nu sunt
  generate deloc in acest mod (vezi `command_dispatch_service.py`).

## Adaptorul OPCOM -- limitare documentata

Vezi [LIMITATIONS.md](LIMITATIONS.md) pentru detalii. Pe scurt: reteaua din
mediul in care a fost dezvoltata aceasta platforma blocheaza accesul la
`opcom.ro`, deci schema exacta a CSV-ului nu a putut fi verificata direct.
Adaptorul foloseste o schema implicita configurabila, valideaza explicit
structura gasita (nu presupune ca se potriveste), si cade pe date sintetice
marcate clar daca sursa reala e inaccesibila.

## Frontend fara CDN

Tailwind CSS e compilat local (`npm run build:css`) intr-un singur fisier
`app/web/static/css/app.css`, comis/generat la build. HTMX si Apache ECharts
sunt vendorizate local (`scripts/vendor_assets.js` copiaza din
`node_modules` in `app/web/static/js/vendor/`) si servite de FastAPI --
nicio dependenta de CDN pentru functionalitatile principale.

## De ce aceste alegeri (rezumat deciziilor reversibile)

- **UUID ca cheie primara** (nu auto-increment): evita scurgerea de
  informatie despre volum/ordine, e portabil intre medii (dev/test/prod nu
  ajung sa aiba ID-uri suprapuse).
- **Versionare prin randuri noi** (config/preferinte/tarife), nu UPDATE:
  istoric complet, audit natural, posibilitate de a compara ce s-a schimbat.
- **Sesiuni server-side** (nu JWT stateless): permite revocare imediata,
  cerinta explicita din specificatie.
- **Cookie CSRF cu verificare dubla** (nu token legat de sesiune): simplu de
  rationat, functioneaza uniform pentru pagini autentificate si
  neautentificate (login, reset parola).
