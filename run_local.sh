#!/usr/bin/env bash
# Script de pornire locala (fara Docker) pentru EMS Platform.
#
# Ce face:
#   1. Verifica uneltele necesare (python3, node, psql/redis-cli pentru
#      verificari de conectivitate).
#   2. Creeaza/actualizeaza .venv si instaleaza dependentele din
#      requirements.lock.txt (doar daca lipsesc sau lockfile-ul s-a schimbat).
#   3. Instaleaza dependentele npm si compileaza asset-urile frontend
#      (Tailwind CSS + vendorizare htmx/echarts) daca lipsesc.
#   4. Creeaza .env din .env.example daca nu exista deja.
#   5. Verifica accesul la PostgreSQL si Redis (nu porneste servicii de
#      sistem automat -- fiecare mediu difera; doar avertizeaza clar daca
#      nu sunt accesibile si cum sa le pornesti).
#   6. Ruleaza migratiile Alembic.
#   7. Porneste serverul web (uvicorn --reload). Optional, cu flag-uri,
#      porneste si worker-ul si scheduler-ul Celery in fundal.
#
# Utilizare:
#   ./run_local.sh                    # doar web
#   ./run_local.sh --with-worker       # web + celery worker
#   ./run_local.sh --with-worker --with-beat   # web + worker + scheduler
#   ./run_local.sh --skip-install      # sare peste pip/npm install (rulare rapida repetata)
#
# Script idempotent: poate fi rulat de mai multe ori fara efecte adverse.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="$ROOT_DIR/.venv"
WITH_WORKER=false
WITH_BEAT=false
SKIP_INSTALL=false
WEB_PORT="${PORT:-8000}"

for arg in "$@"; do
  case "$arg" in
    --with-worker) WITH_WORKER=true ;;
    --with-beat) WITH_BEAT=true ;;
    --skip-install) SKIP_INSTALL=true ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
      exit 0
      ;;
    *)
      echo "Argument necunoscut: $arg (vezi --help)" >&2
      exit 1
      ;;
  esac
done

log() { printf '\033[1;32m[run_local]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[run_local]\033[0m %s\n' "$1" >&2; }
err() { printf '\033[1;31m[run_local]\033[0m %s\n' "$1" >&2; }

BACKGROUND_PIDS=()
cleanup() {
  if [ "${#BACKGROUND_PIDS[@]}" -gt 0 ]; then
    log "Opresc procesele de fundal (worker/scheduler)..."
    for pid in "${BACKGROUND_PIDS[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
  fi
}
trap cleanup EXIT INT TERM

# --- 1. Unelte necesare ---
require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Comanda '$1' nu a fost gasita in PATH. $2"
    exit 1
  fi
}

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  err "Python 3.11 nu a fost gasit. Instaleaza-l si reincearca (sau seteaza PYTHON_BIN=/cale/python3.11)."
  exit 1
fi

require_cmd node "Necesar pentru build-ul asset-urilor frontend (Tailwind/htmx/echarts). Instaleaza Node.js 20+."
require_cmd npm "Instaleaza Node.js 20+ (npm vine impreuna cu el)."

log "Folosesc interpretorul Python: $($PYTHON_BIN --version)"

# --- 2. Mediu virtual + dependente Python ---
if [ ! -d "$VENV_DIR" ]; then
  log "Creez mediul virtual in .venv/ ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

INSTALL_MARKER="$VENV_DIR/.requirements.lock.sha256"
CURRENT_HASH="$(sha256sum requirements.lock.txt | awk '{print $1}')"
if [ "$SKIP_INSTALL" = false ] && { [ ! -f "$INSTALL_MARKER" ] || [ "$(cat "$INSTALL_MARKER" 2>/dev/null)" != "$CURRENT_HASH" ]; }; then
  log "Instalez dependentele Python din requirements.lock.txt ..."
  pip install --upgrade pip -q
  pip install -r requirements.lock.txt -q
  echo "$CURRENT_HASH" > "$INSTALL_MARKER"
else
  log "Dependentele Python sunt deja instalate si la zi (--skip-install sau lockfile neschimbat)."
fi

# --- 3. Asset-uri frontend ---
if [ "$SKIP_INSTALL" = false ]; then
  if [ ! -d "$ROOT_DIR/node_modules" ]; then
    log "Instalez dependentele npm ..."
    npm install --no-fund --no-audit
  fi
  if [ ! -f "$ROOT_DIR/app/web/static/css/app.css" ] || [ ! -f "$ROOT_DIR/app/web/static/js/vendor/htmx.min.js" ]; then
    log "Compilez asset-urile frontend (Tailwind CSS + vendorizare htmx/echarts) ..."
    npm run build
  else
    log "Asset-urile frontend exista deja (foloseste 'npm run build' manual daca ai modificat CSS/templates)."
  fi
else
  log "--skip-install: sar peste npm install/build."
fi

# --- 4. Fisier .env ---
if [ ! -f "$ROOT_DIR/.env" ]; then
  log "Nu exista .env -- il creez din .env.example."
  cp .env.example .env
  warn "Editeaza .env si seteaza cel putin SECRET_KEY si BOOTSTRAP_ADMIN_TOKEN inainte de a continua in productie."
fi

set -a
# shellcheck disable=SC1091
source "$ROOT_DIR/.env"
set +a

# --- 5. Verificare PostgreSQL si Redis ---
check_postgres() {
  if ! command -v psql >/dev/null 2>&1; then
    warn "psql nu e instalat -- sar peste verificarea de conectivitate la PostgreSQL (alembic va esua clar daca nu e accesibil)."
    return
  fi
  local db_url="${DATABASE_URL:-postgresql+psycopg://ems:ems@localhost:5432/ems}"
  local pg_url="${db_url/postgresql+psycopg:/postgresql:}"
  if ! psql "$pg_url" -c "SELECT 1;" >/dev/null 2>&1; then
    err "Nu ma pot conecta la PostgreSQL folosind DATABASE_URL din .env ($db_url)."
    err "Porneste PostgreSQL local (ex: 'brew services start postgresql' / 'sudo service postgresql start')"
    err "sau ruleaza 'docker compose up postgres -d' daca preferi containerul din acest repo."
    exit 1
  fi
  log "PostgreSQL accesibil."
}

check_redis() {
  if ! command -v redis-cli >/dev/null 2>&1; then
    warn "redis-cli nu e instalat -- sar peste verificarea de conectivitate la Redis."
    return
  fi
  local redis_url="${REDIS_URL:-redis://localhost:6379/0}"
  if ! redis-cli -u "$redis_url" ping >/dev/null 2>&1; then
    err "Nu ma pot conecta la Redis folosind REDIS_URL din .env ($redis_url)."
    err "Porneste Redis local (ex: 'redis-server --daemonize yes') sau 'docker compose up redis -d'."
    exit 1
  fi
  log "Redis accesibil."
}

check_postgres
check_redis

# --- 6. Migratii ---
log "Rulez migratiile Alembic ..."
alembic upgrade head

# --- 7. Pornire procese ---
if [ "$WITH_WORKER" = true ]; then
  log "Pornesc Celery worker in fundal ..."
  celery -A app.celery_app worker --loglevel=info &
  BACKGROUND_PIDS+=($!)
fi

if [ "$WITH_BEAT" = true ]; then
  log "Pornesc Celery beat (scheduler) in fundal -- o singura instanta, nu rula in paralel cu alta!"
  celery -A app.celery_app beat --loglevel=info &
  BACKGROUND_PIDS+=($!)
fi

log "Pornesc serverul web pe http://localhost:${WEB_PORT}"
if [ -z "${BOOTSTRAP_ADMIN_TOKEN:-}" ]; then
  warn "BOOTSTRAP_ADMIN_TOKEN nu e setat in .env -- /bootstrap-admin nu va functiona pana nu-l setezi si repornesti."
else
  log "Primul admin: http://localhost:${WEB_PORT}/bootstrap-admin (foloseste BOOTSTRAP_ADMIN_TOKEN din .env)"
fi

# Nu folosim `exec` aici: vrem ca acest script (bash) sa ramana procesul
# parinte, ca trap-ul de cleanup de mai sus sa opreasca worker/beat-ul din
# fundal cand serverul web e oprit (Ctrl+C) sau iese cu eroare.
uvicorn app.main:app --reload --host 0.0.0.0 --port "$WEB_PORT"
