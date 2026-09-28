# Notificari in pagina statiei

Clopotelul din bara de sus deschide un dialog cu rezumatul pentru ieri si
incidentele statiei. Dialogul pastreaza pagina deschisa, are filtre pentru
tip si necitite, paginare, contor de necitite, stari de eroare/reincercare si
marcare individuala ca citit. Escape inchide dialogul si intoarce focusul la
clopotel. Include tema intunecata si afisare pe mobil.

## Date si generare

Jobul existent `notifications_task` genereaza si actualizeaza cardul pentru
ieri, dupa 00:20 in fusul statiei, pentru membrii activi ai organizatiilor
ne-arhivate. Este necesar workerul Celery si schedulerul existente. GET-ul
inboxului nu genereaza date si nu efectueaza scrieri.

Se citeste exclusiv agregatul `day` cu limitele exacte ale zilei locale.
Zilele DST au 23/25 ore; toate instantele se pastreaza in UTC. Valorile
energetice raman Decimal, serializate ca siruri; NULL ramane necunoscut.
Cardul arata productia, consumul, importul si exportul cu acoperirea fiecarui
indicator si provenienta agregatului. Lipsa unui agregat produce un card
explicit fara date, nu zerouri. Statia trebuie sa fi existat inainte de
sfarsitul zilei raportate.

Evidentierile sunt calculate conservator:

- Recordul compara productia cu zilele anterioare masurate, cu acoperire PV
  100%. Exista cel putin o zi comparabila; mesajul precizeaza cate zile
  complete sunt disponibile, nu sustine un record anterior istoricului EMS.
- O zi fara import necesita consum pozitiv, import exact zero si valori
  masurate cu acoperire 100% pentru toti cei patru indicatori. Nu atribuie
  provenienta energiei din baterie si nu estimeaza economii financiare.

Cheia unica `(user_id, station_id, source_key)` si blocarea randurilor
statiilor serializeaza workerii concurenti. Reprocesarea actualizeaza acelasi
card pentru ieri daca datele s-au modificat, pastrand ID-ul si `read_at`.
Cardurile mai vechi sunt instantanee ale datelor disponibile cand au fost
generate; nu sunt recalculate retrospectiv la un backfill ulterior.

Rezumatele sunt in-app. Rutarea email/push, escaladarile si rezumatul extern
saptamanal continua sa primeasca doar notificari asociate unui AlertEvent.
Preferintele existente de livrare a incidentelor nu se schimba.

## Avertizarea de tensiune

Regula `grid_voltage_high` foloseste doar `diagnostics.phases` pentru
`circuit=grid`, cu provenienta masurata si telemetrie proaspata. Fiecare faza
L1/L2/L3 are propriul ciclu de incident: >253 V declanseaza avertizarea;
revenirea necesita doua ferestre sanatoase <=248 V, conform ciclului health
existent. Senzorii absenti, simulati, derivati sau intarziati dau verdict
necunoscut, nu recuperare. Fazele de consum nu declanseaza aceasta regula.

253 V este pragul operational de avertizare al acestei reguli pentru
tensiunile de faza raportate; nu este o constatare de conformitate legala,
un diagnostic al cauzei sau dovada unei abateri conform unei medieri
reglementate. Nu se genereaza automat o reclamatie si nu exista comenzi
catre hardware. Adaptoarele trebuie sa raporteze tensiunea faza-neutru in
acest camp, nu tensiuni intre faze.

Mesajul pastreaza valoarea din `HealthEvaluation` de la activarea evenimentului,
chiar daca alerta a primit intre timp alte masuratori. Numarul din ultimele
30 de zile numara incidentele pe faze, nu esantioanele si nici incalcari legale.

## Contract si migrare

- `GET /stations/{id}/notifications?kind=all|summary|alert&unread=false&offset=0`
  intoarce pana la 30 `items`, `unread_count`, `has_more`, `timezone`, `today`.
  Rezumatul pentru ieri apare primul; celelalte carduri sunt ordonate dupa
  creare, apoi ID. Raspunsul are `Cache-Control: no-store`.
- `POST /stations/{id}/notifications/{notice_id}/read` necesita CSRF si acces
  viewer la statie. Se poate marca doar notificarea utilizatorului curent
  din statia ceruta. Actiunea este idempotenta si auditata.

Migrarea `d91e62b48c03` adauga `source_key` si `payload` la notificari si
permite un `event_id` NULL exclusiv pentru surse non-eveniment. Constrangerea
CHECK impune exact una dintre cele doua surse. Downgrade-ul arhiveaza toate
datele noi in `legacy_day_notifications`, reface contractul vechi fara a
converti NULL in zero, iar upgrade-ul restaureaza istoricul cu parinti valizi.

Validare: `tests/integration/test_station_notifications.py` exercita PostgreSQL,
concurenta, DST/an bisect, NULL/zero, provenienta, RBAC/CSRF, migrare dus-intors,
paginare si praguri. `tests/e2e/test_station_notifications.py` verifica fluxul
real in browser, filtre, citire persistenta, tastatura, mobil, teme si retry.
