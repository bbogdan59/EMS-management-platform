# Standarde de cod

Acest document descrie conventiile de stil/lint aplicate proiectului, ca sa
fie cunoscute si aplicate consecvent la fiecare schimbare (nu doar "stiute"
informal). Sunt impuse mecanic de `ruff` (configurat in `pyproject.toml`,
sectiunile `[tool.ruff]`), rulat local si in CI (`.github/workflows/ci.yml`).

## Baza: PEP 8, prin ruff

`ruff check` acopera:

- **pycodestyle** (`E`, `W`) + **pyflakes** (`F`) -- baza PEP 8 (indentare,
  spatiere, importuri neutilizate, variabile nefolosite, comparatii
  incorecte etc.).
- **isort** (`I`) -- ordinea si gruparea importurilor (stdlib / terte parti /
  proiect), auto-fixabil (`ruff check --fix`).
- **pyupgrade** (`UP`) -- sintaxa moderna pentru Python 3.11 (ex.
  `datetime.UTC` in loc de `timezone.utc`, `X | Y` in loc de `Union[X, Y]`).
- **flake8-bugbear** (`B`) -- tipare reale de bug-uri: argumente implicite
  mutabile, `except` care inghite eroarea originala fara `raise ... from`,
  `zip()` fara `strict=` (poate ascunde liste cu lungimi diferite) etc.
- **flake8-comprehensions** (`C4`) -- comprehensions/literali mai simpli
  (`dict(...)`  -> `{...}`, etc.).
- **flake8-simplify** (`SIM`) -- simplificari de control flow (if-uri
  imbricate care pot fi combinate, `try/except/pass` -> `contextlib.suppress`).
- **RUF** (reguli specifice ruff) -- `noqa` neutilizate, variabile din
  destructurare nefolosite etc.

Configuratia completa e in `pyproject.toml` (`[tool.ruff.lint]`). Doua
exceptii documentate acolo, cu motiv:

- `E501` (linie prea lunga) e dezactivata: codul prefera functii de serviciu
  dense, pe o singura linie, in loc de wrap fortat la o coloana fixa. Un
  reviewer poate cere in continuare sa se sparga o linie daca ii scade
  lizibilitatea -- nu e o interdictie, doar nu e impusa mecanic.
- `B008` (apel de functie ca valoare implicita) e permisa explicit pentru
  `fastapi.Depends`/`Query`/`Path`/`Body`/`Cookie`/`Header`/`Form`/`File`
  (`[tool.ruff.lint.flake8-bugbear] extend-immutable-calls`), pentru ca e
  idiomul standard FastAPI de dependency injection, nu un bug.
- `RUF059` (variabila nefolosita dintr-o destructurare) e ignorata in
  `tests/*`: testele destructureaza des un tuplu de fixture/context comun
  (`now, station, config, pref, device, plan, interval, db = context()`)
  chiar daca un test anume nu foloseste toate elementele -- consecventa intre
  teste conteaza mai mult decat prefixarea cu `_` de fiecare data.

## Cum se ruleaza local

```bash
ruff check .            # lint
ruff check --fix .      # aplica fix-urile sigure automat
ruff format --check .   # verifica formatarea (vezi nota de mai jos)
pytest tests/ -q --ignore=tests/e2e   # unit + integration
pytest tests/e2e -q                   # Playwright (necesita Postgres+Redis pornite)
mypy app                              # optional, tipare (nu e inca impus in CI)
```

**Nota despre `ruff format`:** codul existent NU respecta inca stilul
canonic al formatter-ului `ruff format` (multe fisiere ar fi reformatate
masiv -- indentare de argumente, sparte linii lungi etc.). Aplicarea in bloc
ar produce un diff urias, nelegat de nicio schimbare functionala, greu de
revizuit. Pana la o decizie explicita de a face acel pas (ca schimbare
dedicata, izolata), `ruff format` NU e impus in CI -- doar `ruff check`.
Fisiere noi/atinse ar trebui totusi scrise cat mai aproape de stilul
canonic (spatiere consecventa, trailing commas rezonabile), fara sa se
forteze o reformatare a codului deja existent din jur.

## Conventii specifice acestui proiect (dincolo de ce verifica ruff)

Aceste reguli nu sunt (inca) verificabile mecanic, dar sunt asteptate la
code review, pe baza unor bug-uri reale gasite si corectate in acest
repository:

1. **Bani si energie: intotdeauna `Decimal`, niciodata `float`.** Toate
   coloanele monetare/energetice din modele sunt `Numeric`/`Decimal`.
   Amestecul cu `float` introduce erori de rotunjire greu de depistat in
   facturi/optimizare.
2. **Limite/praguri configurabile: verificare explicita `is not None`, nu
   `if x:` sau `x or default`.** Un `Decimal("0")` explicit (ex. "export
   interzis complet", "fara buget EFC azi") e o valoare valida si diferita
   de "nesetat" -- pattern-ul `x or default` il trateaza gresit ca
   "nesetat" si il ignora silentios. Acesta a fost un bug real, corectat in
   PR #3 (`optimization_service.py`) si e verificat activ la review.
3. **Data/ora locala a pietei (PZU) se calculeaza in `Europe/Bucharest`, nu
   `UTC`.** `datetime.now(timezone.utc).date()` produce ziua gresita in
   fereastra ~21:00-23:59 UTC (deja alta zi la Bucuresti). Foloseste
   `datetime.now(ZoneInfo("Europe/Bucharest")).date()` (vezi
   `opcom_service.BUCHAREST` / `market_analytics_service.BUCHAREST`) pentru
   orice logica de "azi"/"maine" legata de piata sau de afisare catre
   utilizator. Bug real, corectat in PR #3 si PR #7.
4. **Date sintetice/fixture nu se amesteca niciodata implicit cu date
   reale** in analytics/rapoarte/export. Orice sursa care poate produce
   date de test (`ImportRun.is_synthetic_fixture`, etc.) trebuie exclusa
   implicit din agregari si predictii, cu un parametru explicit
   (`include_synthetic=True`) pentru diagnostic, si cu provenienta
   propagata mai departe (nu doar filtrata silentios).
5. **Fara comentarii care explica CE face codul** (numele bune de
   variabile/functii fac asta) -- doar DE CE, cand nu e evident (o
   constrangere ascunsa, un workaround pentru un bug specific, un
   comportament care ar surprinde un cititor).
6. **Nicio abstractie/parametru speculativ** ("poate va trebui candva").
   Cod pentru cerinta de azi; refactorizare cand apare a doua/a treia
   utilizare reala, nu inainte.
7. **Excepțiile relansate in interiorul unui `except` pastreaza lantul
   cauzal** (`raise NouaEroare(...) from exc`, sau `from None` daca
   ascunderea e intentionata) -- altfel se pierde stack trace-ul original
   la debugging.

## Ce nu e (inca) acoperit

- `mypy` e instalat ca dependinta de dezvoltare dar nu e impus in CI (codul
  foloseste `from __future__ import annotations` si adnotari peste tot, dar
  n-a fost verificat exhaustiv static). Poate fi adaugat ca pas separat.
- `ruff format` (vezi nota de mai sus) -- decizie explicita, separata.
