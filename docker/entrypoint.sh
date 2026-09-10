#!/bin/sh
set -e

ROLE="${1:-web}"
PORT="${PORT:-8000}"

wait_for_postgres() {
  echo "Astept PostgreSQL..."
  python - <<'PYEOF'
import sys, time
import sqlalchemy
from app.config import get_settings

settings = get_settings()
engine = sqlalchemy.create_engine(settings.database_url)
for attempt in range(30):
    try:
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        print("PostgreSQL disponibil.")
        sys.exit(0)
    except Exception as exc:
        print(f"PostgreSQL indisponibil (incercarea {attempt + 1}/30): {exc}")
        time.sleep(2)
sys.exit(1)
PYEOF
}

wait_for_redis() {
  echo "Astept Redis..."
  python - <<'PYEOF'
import sys, time
import redis
from app.config import get_settings

settings = get_settings()
for attempt in range(30):
    try:
        redis.from_url(settings.redis_url).ping()
        print("Redis disponibil.")
        sys.exit(0)
    except Exception as exc:
        print(f"Redis indisponibil (incercarea {attempt + 1}/30): {exc}")
        time.sleep(2)
sys.exit(1)
PYEOF
}

case "$ROLE" in
  web)
    wait_for_postgres
    wait_for_redis
    if [ "${RUN_MIGRATIONS_ON_START:-false}" = "true" ]; then
      echo "RUN_MIGRATIONS_ON_START=true -- aplic migratiile Alembic..."
      alembic upgrade head
    fi
    exec gunicorn app.main:app \
      --workers "${WEB_CONCURRENCY:-2}" \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind "0.0.0.0:${PORT}" \
      --access-logfile - \
      --error-logfile -
    ;;
  worker)
    wait_for_postgres
    wait_for_redis
    exec celery -A app.celery_app worker --loglevel=info --concurrency="${CELERY_CONCURRENCY:-2}"
    ;;
  scheduler)
    wait_for_postgres
    wait_for_redis
    exec celery -A app.celery_app beat --loglevel=info
    ;;
  migrate)
    wait_for_postgres
    echo "Aplic migratiile Alembic (pas controlat)..."
    exec alembic upgrade head
    ;;
  seed-demo)
    wait_for_postgres
    exec python scripts/seed_demo.py
    ;;
  simulator)
    exec python -u -m simulator.run
    ;;
  shell)
    exec python
    ;;
  *)
    echo "Rol necunoscut: $ROLE (asteptat: web|worker|scheduler|migrate|seed-demo|simulator|shell)"
    exit 1
    ;;
esac
