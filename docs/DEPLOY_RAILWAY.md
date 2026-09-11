# Deploy pe Railway

Acest ghid presupune un cont Railway existent si acces la
[railway.app](https://railway.app) (dashboard) sau la `railway` CLI.
Informatiile despre `railway.json`/variabile de referinta/networking privat
provin din documentatia publica Railway (cautata explicit in timpul
dezvoltarii acestei platforme; sursele sunt citate in text). Deploy-ul
propriu-zis NU a fost efectuat in aceasta sesiune (fara acces la un
proiect/cont Railway) -- acest document e ghidul exact de urmat.

## 0. Ce e deja in repo (config-as-code)

| Fisier | Serviciu Railway tinta |
|---|---|
| `Dockerfile` | folosit de toate serviciile de mai jos |
| `railway.json` | serviciul **web** (config implicita la radacina repo) |
| `railway.worker.json` | serviciul **worker** |
| `railway.scheduler.json` | serviciul **scheduler** (Celery Beat) |
| `railway.migrate.json` | serviciul **migrate** (job unic, rulat manual) |

Railway citeste `railway.json` din radacina repo-ului **implicit doar pentru
un singur serviciu**. Pentru celelalte servicii (worker/scheduler/migrate),
trebuie sa indici manual, din dashboard, fisierul de config potrivit --
pasul exact e la sectiunea 3 mai jos (asta e un pas care NU poate fi
exprimat doar prin fisiere, Railway il cere din UI/CLI per serviciu).

## 1. Servicii de creat in proiect

Un singur proiect Railway, un singur environment (`production`), cu aceste
servicii:

1. **Postgres** -- din catalogul Railway ("+ New" -> "Database" -> "Add PostgreSQL").
2. **Redis** -- idem, "Add Redis".
3. **web** -- din acest repo Git (GitHub).
4. **worker** -- acelasi repo Git, serviciu separat.
5. **scheduler** -- acelasi repo Git, serviciu separat.
6. **migrate** -- acelasi repo Git, serviciu separat (folosit doar pentru a
   rula migratiile controlat, nu ramane "running" permanent).

Postgres si Redis **nu trebuie expuse public** -- Railway nu le expune
automat (nu au domeniu public implicit); foloseste doar `RAILWAY_PRIVATE_DOMAIN`
(retea privata `*.railway.internal`), nu genera un TCP proxy public pentru
ele decat daca ai un motiv explicit (nu e cazul aici).

## 2. Variabile de mediu

Pe fiecare din serviciile **web**, **worker**, **scheduler**, **migrate**,
seteaza (Settings -> Variables):

```
ENVIRONMENT=production
DEBUG=false
SECRET_KEY=<valoare unica, genereaza cu: python -c "import secrets; print(secrets.token_urlsafe(48))">
BASE_URL=https://<domeniul-tau-railway-sau-custom>
SESSION_COOKIE_SECURE=true

# Referinte catre celelalte servicii Railway (sintaxa ${{Serviciu.VAR}}):
DATABASE_URL=postgresql+psycopg://${{Postgres.PGUSER}}:${{Postgres.POSTGRES_PASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
REDIS_URL=redis://${{Redis.RAILWAY_PRIVATE_DOMAIN}}:6379

# Bootstrap -- seteaza o valoare, creeaza primul admin din /bootstrap-admin,
# apoi STERGE aceasta variabila (sau roteaz-o) ca sa nu ramana activa.
BOOTSTRAP_ADMIN_TOKEN=<valoare unica, o singura data>

EMAIL_BACKEND=smtp
SMTP_HOST=<provider SMTP>
SMTP_PORT=587
SMTP_USERNAME=<utilizator SMTP>
SMTP_PASSWORD=<secret SMTP>
SMTP_FROM_ADDRESS=<adresa expeditor>
SMTP_USE_TLS=true
DEMO_MODE_ENABLED=false  # OBLIGATORIU false in productie (config.py refuza pornirea altfel)
```

`config.py` refuza pornirea in productie daca cookie-ul de sesiune nu este
`Secure` sau daca backend-ul de email este `console`. Backend-ul console
redacteaza corpul mesajelor deoarece acesta contine tokenuri de invitatie/resetare.

> Nota despre `DATABASE_URL`: Railway furnizeaza propriul `DATABASE_URL` in
> formatul `postgresql://...` (fara driver). `app/config.py` normalizeaza
> automat acest format la `postgresql+psycopg://` daca il folosesti direct
> (`DATABASE_URL=${{Postgres.DATABASE_URL}}`) -- ambele variante de mai sus
> functioneaza; varianta explicita cu componente separate e recomandata
> pentru claritate.

Restul variabilelor din `.env.example` au valori implicite rezonabile si
sunt optionale.

## 3. Configurare per serviciu (pasi din dashboard, nu pot fi exprimati in fisiere)

Pentru fiecare serviciu **worker**, **scheduler**, **migrate**:

1. Deschide serviciul -> **Settings** -> **Config-as-code (Config File)**.
2. Seteaza calea catre fisierul corespunzator:
   - worker -> `railway.worker.json`
   - scheduler -> `railway.scheduler.json`
   - migrate -> `railway.migrate.json`
3. Salveaza -- Railway va folosi `deploy.startCommand` din acel fisier
   (`/entrypoint.sh worker`, `/entrypoint.sh scheduler`, `/entrypoint.sh migrate`)
   in loc de comanda implicita a serviciului **web**.

Pentru serviciul **web**, `railway.json` de la radacina se aplica automat
(nu necesita pasul de mai sus).

**IMPORTANT -- o singura instanta de scheduler:** in Settings -> serviciul
**scheduler**, verifica sectiunea Replicas/Regions si NU creste numarul de
replici peste 1. Doua instante de Celery Beat ar dubla toate joburile
planificate (importuri OPCOM, optimizari etc.).

## 4. Migratii -- pas controlat

Nu rula migratiile automat la fiecare deploy al serviciului **web**
(`RUN_MIGRATIONS_ON_START` ramane `false` implicit). In schimb:

1. Dupa primul deploy reusit al serviciului **migrate** (creat la pasul 1,
   cu config-as-code setat la `railway.migrate.json`), Railway il ruleaza o
   data; verifica logurile -- trebuie sa vezi `Running upgrade ... -> ...`.
2. Pentru migratii ulterioare (dupa un `git push` cu modele noi), redeploy
   manual doar al serviciului **migrate** (buton "Redeploy" din dashboard,
   sau `railway up --service migrate` din CLI) **inainte** de a redeploya
   **web**/**worker**/**scheduler**.
3. Alternativ, din `railway` CLI, poti rula ad-hoc:
   ```
   railway run --service web alembic upgrade head
   ```

### Backfill istoric de preturi OPCOM (optional, dupa migratii)

Pentru a popula istoricul de preturi PZU pornind de la 2024-01-01 (folosit de
pagina `/market/prices` -- grafice an-peste-an si predictie), ruleaza o data
ad-hoc:

```
railway run --service web python -m scripts.backfill_opcom_history --start 2024-01-01
```

sau creeaza temporar un serviciu cu config-as-code `railway.migrate.json`
dar cu `startCommand` inlocuit cu `/entrypoint.sh backfill-opcom` (vezi
variabilele `OPCOM_BACKFILL_START`/`OPCOM_BACKFILL_END`/`OPCOM_BACKFILL_CONCURRENCY`
din `docker/entrypoint.sh`), il rulezi o data, apoi il stergi. Dureaza de la
cateva zeci de secunde (daca majoritatea zilelor cad pe fallback sintetic) la
cateva minute (cu acces real la opcom.ro si concurenta implicita de 8).

## 5. Networking, PORT si healthcheck

- Serviciul **web** trebuie sa asculte pe `0.0.0.0:$PORT` -- deja configurat
  in `docker/entrypoint.sh` (`gunicorn ... --bind 0.0.0.0:${PORT}`); Railway
  injecteaza automat variabila `PORT`.
- Genereaza un domeniu public: Settings -> Networking -> "Generate Domain"
  (sau ataseaza un domeniu custom). **Doar serviciul web** are nevoie de
  domeniu public.
- `railway.json` seteaza `deploy.healthcheckPath=/health`; Railway asteapta
  un `200 OK` de la acest endpoint inainte sa considere deploy-ul sanatos.

## 6. Persistenta, backup, restaurare, rollback

- **Postgres**: Railway ataseaza automat un volum persistent bazei de date
  gestionate; datele supravietuiesc redeploy-urilor. Pentru backup, foloseste
  Railway's built-in backup (Settings -> Backups, daca planul tau il ofera)
  sau `pg_dump` periodic catre stocare externa (ex. un job Celery Beat
  suplimentar sau un cron extern) -- nu e inclus automat in acest repo.
- **Nu folosi filesystem-ul efemer al containerului** pentru date care
  trebuie pastrate: platforma nu scrie date persistente pe disc local
  (totul e in PostgreSQL); singura exceptie e volumul `simulator_data`
  (mod demo), explicit non-critic.
- **Rollback**: Railway pastreaza deploy-urile anterioare per serviciu --
  din tab-ul "Deployments", selecteaza un deploy anterior si "Redeploy".
  Pentru schema DB, `alembic downgrade -1` (rulat manual, cu grija) inversa
  ultima migratie daca e reversibila.

## 7. Modul demonstrativ -- dezactivat implicit in Railway

`DEMO_MODE_ENABLED=false` pe toate serviciile de productie. Daca vrei un
environment Railway separat pentru demo:

1. Creeaza un environment nou (`demo`), cu aceleasi servicii **plus**
   `seed-demo` (job unic, config-as-code `railway.migrate.json` adaptat cu
   `startCommand: "/entrypoint.sh seed-demo"`) si `simulator` (serviciu
   continuu, `startCommand: "/entrypoint.sh simulator"`).
2. Seteaza `DEMO_MODE_ENABLED=true` doar in acest environment.
3. Adauga un volum persistent montat la `/data` pe serviciile `seed-demo` si
   `simulator` (acelasi volum), pentru fisierul de seed si starea locala a
   simulatorului.

`config.py` blocheaza explicit pornirea daca `ENVIRONMENT=production` si
`DEMO_MODE_ENABLED=true` in acelasi timp -- foloseste `ENVIRONMENT=development`
sau `test` pentru environment-ul demo.

## 8. Ordinea recomandata la primul deploy

1. Creeaza Postgres si Redis.
2. Creeaza serviciul **web**, seteaza variabilele (sectiunea 2), asteapta
   primul build. Nu va porni corect inca (baza de date e goala) -- e ok.
3. Creeaza serviciul **migrate**, seteaza config-as-code + variabile, ruleaza-l o data.
4. Redeploy **web** (acum baza are schema).
5. Creeaza **worker** si **scheduler**, cu config-as-code respectiv.
6. Genereaza domeniul public pe **web**, verifica `https://<domeniu>/health`.
7. Acceseaza `https://<domeniu>/bootstrap-admin` cu `BOOTSTRAP_ADMIN_TOKEN`,
   creeaza primul cont `platform_admin`, apoi sterge/roteaza variabila.

## Surse

Detaliile despre config-as-code (`railway.json`, campurile
`healthcheckPath`/`restartPolicyType`/`numReplicas`), despre suprascrierea
comenzii de start (inlocuieste ENTRYPOINT-ul imaginii) si despre variabilele
de referinta/networking privat (`${{Serviciu.VAR}}`, `RAILWAY_PRIVATE_DOMAIN`)
au fost verificate prin cautare directa in timpul dezvoltarii acestei
platforme (acces direct la docs.railway.com blocat de politica de retea a
mediului de dezvoltare):

- https://docs.railway.com/config-as-code
- https://docs.railway.com/config-as-code/reference
- https://docs.railway.com/deployments/start-command
- https://docs.railway.com/builds/build-and-start-commands
- https://docs.railway.com/variables/reference
- https://docs.railway.com/databases/postgresql
- https://station.railway.com/questions/private-networking-service-cannot-reach-3d1be833

Recomandare: verifica aceste pagini direct inainte de deploy, in caz ca
formatul s-a schimbat intre timp.
