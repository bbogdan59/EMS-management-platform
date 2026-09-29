# Măsurare și predare pentru implementare

[Specificație](README.md) · [Parcursuri](JOURNEYS.md) · [Stări](STATES.md)

## Catalog analytics v1 — propunere pentru #209

Analytics de produs este **opt-in, oprit implicit**. Nu este necesar pentru
onboarding, funcționare, suport sau ștergerea contului. Niciun SDK nou în
acest PR. Evenimentele sunt trimise numai după consent verificat; retragerea
oprește colectarea și golește coada locală. Nu se păstrează coadă pre-consent.

Proprietăți comune allowlisted: `schema_version=1`, versiune app, platformă
(`ios|android`), limbă (`ro|en`), mediu (`staging|production`). Fără user,
tenant, installation sau station ID, hash al acestora ori fingerprint.
Pentru funnel în aceeași sesiune se poate folosi un nonce aleator în memorie,
rotit la restart/logout/retragere; nu corelează sesiuni sau alte servicii.
Ingestia nu păstrează IP/antete/URL-uri complete ca analytics. Agregare pe zi;
cohortele mici (<20 sesiuni) nu sunt expuse în dashboard.

| Eveniment | Moment (o dată pe tranziție) | Proprietăți specifice permise |
|---|---|---|
| `onboarding_started` | Intrare prima experiență după opt-in | `entry=new_account|existing_account|invitation` |
| `auth_result` | Rezultat autentificare | `method=signup|login|recovery`, `result=success|cancelled|failed`, cod eroare din enum |
| `deye_connect_started` | Lansare onboarding EMS în browser | `entry=onboarding|settings`, `existing_connection=yes|no` |
| `deye_connect_result` | Conexiune server confirmată/anulată/eșuată | `result`, `stage=browser|selection|sync`, `reason=auth|quota|provider|unsupported|unknown` |
| `first_measured_overview` | Prima afișare măsurată în sesiunea de onboarding | `elapsed_bucket=under_1m|1_5m|5_15m|over_15m`, `station_count=one|many` |
| `ha_pairing_result` | Pairing confirmat/expirat/anulat | `result`, `reason=expired|replayed|network|permissions|unknown` |
| `ha_mapping_saved` | Versiune salvată pe server | `count_bucket=1|2_3|4_plus`; fără categorii occupancy sau nume de entități |
| `overview_loaded` | Snapshot afișat | `mode=network|cache`, `result=complete|partial|empty|failed`, `latency_bucket=under_1s|1_2s|2_5s|over_5s` |
| `history_opened` | Perioadă selectată | `range=day|week|month`, `metric=pv|load|grid|battery`; fără data aleasă/valori |
| `forecast_opened` | Detaliu afișat | `result=available|missing|stale` |
| `bill_opened` | Detaliu afișat | `period=current|previous`, `result=available|partial|missing` |
| `notification_opened` | Destinație autorizată afișată | `entry=inbox|push`, `kind=summary|incident|forecast|cost|sync`; fără notice ID/payload |
| `push_permission_result` | Rezultat prompt contextual | `result=granted|denied|later` |
| `deletion_initiated` | Confirmare server a cererii, numai dacă acordul este încă valid | `result=accepted`; fără motiv introdus sau identitate |

`deletion_initiated` nu este registrul ștergerilor și nu poate condiționa
procesarea cererii. Auditul legal/operațional și revocarea rămân în backend,
cu acces/retenție proprii. Erorile tehnice sunt enumuri; niciodată mesaje
libere, emailuri, secrete, seriale, cod pairing, URL-uri callback, poziția
casei, starea occupancy, valori energetice/financiare sau payloaduri brute.
Request ID pentru depanare aparține observabilității sanitizate, separat de
analytics de produs; nu se conectează automat cu nonce-ul sesiunii.

Propunere retenție #209: evenimente de produs brute maximum 30 zile,
agregate fără nonce maximum 90 zile; acces limitat echipei de produs.
Inventarul real al SDK-urilor și consentul/retenția finală se verifică în
#207/#209 înainte de beta. Aceste valori sunt decizii de implementat, nu
afirmații despre o infrastructură analytics deja instalată.

## Criterii de succes propuse

Ținte de acceptare pentru beta, nu rezultate măsurate. Eșantionul analytics
include numai utilizatori cu opt-in; nu îl prezentăm ca întreaga bază.

| Obiectiv | Măsurare / prag de pornire |
|---|---|
| Înțelege energia de acum | Minimum 4 din 5 participanți RO identifică în 10 secunde producția, sensul rețelei și prospețimea, fără ajutor, pe ambele mărimi. Test de usability cu date fictive. |
| Înțelege limita estimării | Minimum 4 din 5 disting măsurat/estimat/lipsă și proiecție/factură emisă. Niciun participant nu interpretează „—” ca zero în scenariul de verificare. |
| Onboarding Deye utilizabil | ≥80% din sesiunile beta eligibile începute ajung la prima observație măsurată în aceeași sesiune; raportăm separat anulări/provider unavailable/credențiale lipsă. Nu pretindem un funnel cross-session. |
| HA rămâne opțional | 100% dintre scenariile Deye-only ating Overview fără HA sau permisiuni de rețea locală. |
| Prim conținut rapid | Pe device-urile de referință #210, p95 afișare snapshot după sesiune validă ≤2 s pe profilul de rețea stabilit; cache eligibil ≤1 s. Profilul/device-ul și dimensiunea payloadului se consemnează. |
| Istoric utilizabil | p95 schimbare perioadă ≤2 s pe aceleași condiții, serii agregate; graficul are alternativă tabel și nu blochează scrollul. |
| Privacy și izolare | Zero date cross-tenant în scenariile negative; zero secrete/PII/occupancy în capturile de analytics/push/loguri; consent oprit produce zero evenimente de produs. |
| Încredere în operații | Nicio confirmare de pairing/mapare/revocare/ștergere înainte de răspunsul serverului; reconnect/timeout nu dublează operațiile. |
| Accesibilitate | Toate fluxurile critice cu VoiceOver și TalkBack, font 200%, fără clipping pe 320; contrast și ținte tactile conform specificației. |

Pentru onboarding, timpul providerului se raportează separat de timpul UI;
nu se elimină erorile din raport doar pentru a îmbunătăți rata. Nu definim
D7/retention per persoană cu un catalog care nu identifică persoane.

## Verificarea acestui livrabil documentar

Se verifică linkurile locale, cheile/paritatea cataloagelor, sintaxa JS,
încărcarea fără erori în browser și navigarea prin ecranele/stările prototipului.
Se revizuiesc 320/430, ambele teme și limbile RO/EN, una/mai multe stații,
HA activ/inactiv, selectarea entităților fără preselectare și confirmările
explicit simulate. Rezultatele efectiv rulate sunt consemnate în PR.

Nu sunt revendicate teste de aplicație iOS/Android, TestFlight/Play,
VoiceOver/TalkBack native, pairing HA real, acces Deye live sau hardware.
Acestea aparțin issue-urilor de implementare și QA, nu #197.

## Scenariu de review (10 minute)

1. 320, RO, luminos, o stație, fără HA: S01 → cont → Deye → Acasă. Nu există
   selector inutil; „Mai târziu” în HA păstrează Overview.
2. S06 → producție → zi/săptămână/lună → tabel; S08 → prognoză → factură.
   Verifică unitățile, proveniența, acoperirea și etichetele estimărilor.
3. Setări → HA → pairing simulat → entități: nimic bifat; după selecție,
   context numai pentru entitățile selectate și consentul acordat.
4. Treci prin empty/loading/error/stale/offline în fiecare ecran folosind
   controlul de review. Revino la Ready pentru tranzițiile simulate.
5. 430, întunecat, EN, mai multe stații: selector, notificare, preferințe,
   privacy și ștergere. Confirmarea din prototip spune că nu s-a șters nimic.

Limită deliberată: prototipul prezintă date și grafice fixe pentru evaluarea
ierarhiei. Nu implementează selecție reală de calendar, permisiuni OS,
formulare de provider sau validarea backendului. Contractele finale,
retenția și capabilitățile se validează în issue-urile indicate în spec.
