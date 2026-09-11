# EMS Platform

Platforma web pentru monitorizarea si optimizarea energetica a instalatiilor
fotovoltaice cu invertoare Deye si baterii. Monolit modular Python/FastAPI,
cu procese separate pentru web, worker si scheduler, UI server-side
(Jinja2 + HTMX + Tailwind + ECharts), API versionat pentru viitorul dispozitiv
local, si un simulator software pentru evaluare fara echipamente.

Aceasta platforma implementeaza **exclusiv** partea web/cloud. Nu contine cod
pentru Raspberry Pi/ESP32, drivere Modbus/RS485, registre Deye sau integrare
Home Assistant -- acestea fac parte dintr-un proiect separat (viitorul
"controler local"), care va vorbi cu aceasta platforma prin `/api/v1`
(documentat in [docs/API.md](docs/API.md)).

## Cuprins

- [Stack tehnologic](#stack-tehnologic)
- [Arhitectura](#arhitectura) -- vezi [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [Instalare locala (fara Docker)](#instalare-locala-fara-docker)
- [Instalare cu Docker Compose](#instalare-cu-docker-compose)
- [Mod demonstrativ](#mod-demonstrativ)
- [Teste](#teste)
- [Deploy pe Railway](#deploy-pe-railway) -- vezi [docs/DEPLOY_RAILWAY.md](docs/DEPLOY_RAILWAY.md)
- [Limitari cunoscute](#limitari-cunoscute) -- vezi [docs/LIMITATIONS.md](docs/LIMITATIONS.md)

## Stack tehnologic

| Componenta | Tehnologie |
|---|---|
| API si web | FastAPI, Jinja2, HTMX, Tailwind CSS (compilat local), Apache ECharts (vendorizat local), SSE |
| Baza de date | PostgreSQL 16, SQLAlchemy 2, Alembic |
| Joburi | Celery + Redis (worker si beat separate) |
| Optimizare | Pyomo + HiGHS (`appsi_highs`) |
| Prognoza PV | pvlib |
| Autentificare | Argon2, sesiuni server-side revocabile, CSRF double-submit cookie |
| Teste | pytest (unit/integrare), Playwright (E2E) |
| Container | Docker, docker-compose, configuratie Railway |

Versiune Python: `3.11` (fixata in `pyproject.toml`; vezi si `requirements.lock.txt`
pentru lockfile-ul complet, generat cu `pip freeze` intr-un venv curat).

## Arhitectura

Detalii complete si decizii tehnice in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Pe scurt: monolit modular `app/` cu:

- `app/models` -- SQLAlchemy 2 (29 tabele, versionare pentru configuratie/preferinte/tarife)
- `app/api/v1` -- API-ul public pentru dispozitive (asociere, telemetrie, planuri, comenzi)
- `app/web` -- rute + template-uri server-side pentru UI
- `app/services` -- logica de business (auth, statii, tarife, OPCOM, meteo, PV, consum, optimizare, dashboard)
- `app/workers` -- taskuri Celery (agregare, importuri, prognoze, optimizare, comenzi, alerte, retentie)
- `simulator/` -- simulator de protocol, proces separat, vorbeste doar prin API-ul public
- `alembic/` -- migratii
- `tests/` -- pytest (unit, integration, e2e/Playwright)

## Instalare locala (fara Docker)

Necesita Python 3.11, PostgreSQL 16, Redis 7, Node.js 20+ (doar pentru build-ul CSS/JS).

### Rapid: `run_local.sh`

```bash
cp .env.example .env   # editeaza SECRET_KEY / BOOTSTRAP_ADMIN_TOKEN
./run_local.sh                       # doar web
./run_local.sh --with-worker         # web + celery worker
./run_local.sh --with-worker --with-beat   # web + worker + scheduler (o singura instanta de beat!)
```

Scriptul e idempotent: creeaza `.venv` si instaleaza dependentele Python doar
daca lipsesc sau `requirements.lock.txt` s-a schimbat, instaleaza si compileaza
asset-urile frontend doar daca lipsesc, creeaza `.env` din `.env.example` daca
nu exista, verifica explicit accesul la PostgreSQL/Redis (cu instructiuni
clare daca nu sunt pornite), ruleaza migratiile Alembic, apoi porneste
`uvicorn --reload`. Foloseste `--skip-install` pentru porniri repetate rapide
si `./run_local.sh --help` pentru toate optiunile.

### Manual, pas cu pas

```bash
# 1. Dependente Python
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock.txt

# 2. Asset-uri frontend (Tailwind CSS + vendorizare htmx/echarts, servite local)
npm install
npm run build

# 3. Configurare
cp .env.example .env
# editeaza .env: SECRET_KEY, DATABASE_URL, BOOTSTRAP_ADMIN_TOKEN etc.

# 4. Baza de date
createdb ems   # sau: psql -c "CREATE DATABASE ems OWNER <user>;"
alembic upgrade head

# 5. Porneste procesele (in terminale separate)
uvicorn app.main:app --reload --port 8000
celery -A app.celery_app worker --loglevel=info
celery -A app.celery_app beat --loglevel=info

# 6. Bootstrap primul admin
# Acceseaza http://localhost:8000/bootstrap-admin cu token-ul din BOOTSTRAP_ADMIN_TOKEN.
```

## Piata energie -- istoric si predictii de preturi PZU (OPCOM)

Pe langa importul zilnic automat (azi + maine, cron Celery Beat, idempotent),
platforma poate popula un istoric complet de preturi PZU pornind de la
`01.01.2024`:

```bash
python -m scripts.backfill_opcom_history --start 2024-01-01
# reia automat doar zilele lipsa daca il rulezi din nou (idempotent)
# --concurrency 8 (implicit) controleaza cate zile se preiau simultan
# --force reimporta si zilele deja reusite (creeaza o noua revizie)
```

Rezultatul apare in UI la `/market/prices`: evolutia pretului (linie
punctata pentru "maine", nepublicat inca la momentul cererii), suprapunere
an-peste-an (2024/2025/2026, configurabil), predictie pana la 31 decembrie
(metoda simpla, documentata -- vezi `app/services/market_analytics_service.py`
si `docs/LIMITATIONS.md`) si medii lunare. Export CSV disponibil.

Daca sursa OPCOM nu e accesibila (ex. retea restrictionata), zilele
respective sunt populate cu date **sintetice**, marcate explicit
(`import_runs.is_synthetic_fixture=true`, badge vizibil in UI) -- ruleaza din
nou scriptul dintr-un mediu cu acces la internet ca sa le inlocuiasca cu date
reale.

## Instalare cu Docker Compose

```bash
cp .env.example .env
# editeaza SECRET_KEY, BOOTSTRAP_ADMIN_TOKEN in .env

docker compose up --build
# migrate ruleaza automat o data (serviciu separat), apoi web/worker/scheduler pornesc.
```

Acceseaza `http://localhost:8000/bootstrap-admin` pentru a crea primul cont
`platform_admin`, apoi autentifica-te la `/login`.

Servicii: `postgres`, `redis`, `migrate` (o singura rulare), `web`, `worker`,
`scheduler`. Vezi comentariile din `docker-compose.yml` pentru healthchecks,
volume persistente si retry la conectare.

## Mod demonstrativ

Pentru evaluare fara echipamente fizice (simulator software, date reproductibile
pentru 2 statii -- una cu EV, una fara):

```bash
docker compose --profile demo up --build
```

Aceasta porneste suplimentar:
- `seed-demo` -- creeaza 2 organizatii/statii demo si coduri de asociere
- `simulator` -- se asociaza prin API-ul public, genereaza istoric reproductibil
  (implicit 14 zile) si apoi trimite telemetrie live, cu scenarii ocazionale
  de offline/date intarziate/comenzi respinse

Toate datele simulate sunt marcate explicit (`is_simulated=true` in telemetrie,
`is_demo=true` pe organizatii/statii, badge vizibil in UI). Modul demo nu
porneste niciodata implicit si e blocat explicit daca `ENVIRONMENT=production`
(vezi `app/config.py`).

### Un singur dispozitiv simulat, configurabil (testare rapida)

Pentru un test rapid al unei statii proprii create manual din UI (fara
fisierul de seed multi-statie de mai sus):

1. Creeaza o organizatie + o statie din UI (admin).
2. Din pagina statiei, genereaza un cod de asociere pentru un dispozitiv nou.
3. Ruleaza (din radacina proiectului, cu `.venv` activat):

   ```bash
   python -m simulator.run_mock_device --claim-code ABCD1234 \
       --pv-kwp 5 --inverter-kw 5 --battery-kwh 10
   ```

   Toti parametrii fizici (PV/invertor/baterie/EV/consum) au valori implicite
   rezonabile -- ruleaza si doar cu `--claim-code`. Vezi
   `python -m simulator.run_mock_device --help` pentru lista completa.
   Opreste cu Ctrl+C.

## Teste

```bash
# Unit + integrare (necesita Postgres si Redis locale/Docker, vezi tests/conftest.py)
createdb ems_test
pytest tests/ --ignore=tests/e2e

# End-to-end (Playwright; porneste automat un server + o baza de date dedicata ems_e2e)
pytest tests/e2e
```

Rezultatul ultimei rulari complete: **48/48 teste trecute** (45 unit/integrare + 3 E2E).

## Deploy pe Railway

Ghid complet, pas cu pas (inclusiv ce NU poate fi exprimat in fisiere de
configurare si trebuie facut manual din dashboard) in
[docs/DEPLOY_RAILWAY.md](docs/DEPLOY_RAILWAY.md).

## Documentatie API pentru viitorul dispozitiv

[docs/API.md](docs/API.md) -- scheme complete, exemple JSON, conventii de semn,
autentificare, idempotenta, ciclul de viata al comenzilor.

## Limitari cunoscute

[docs/LIMITATIONS.md](docs/LIMITATIONS.md) -- lista onesta a limitarilor reale
(schema CSV OPCOM neverificata direct din cauza retelei indisponibile in mediul
de dezvoltare, model PV simplificat, arbitraj fara pairing explicit etc.)
