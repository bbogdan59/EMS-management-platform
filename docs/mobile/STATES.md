# Stări de ecran și integrare

[Specificație](README.md) · [Parcursuri](JOURNEYS.md) · [Prototip](wireframes.html)

## Reguli comune

Stările se aplică separat pe resursă: o eroare HA nu ascunde PV Deye; o
eroare de grafic nu golește cardul curent. Skeletonul nu conține valori
numerice fictive. `null` se afișează „—” cu motiv; `0` se afișează ca zero.

- **Loading**: skeleton la prima încărcare; conținut etichetat anterior +
  progres discret la refresh. Retry anulează cererea precedentă.
- **Empty**: niciun rezultat/sursă/perioadă eligibilă, cu un pas următor util.
  Nu este același lucru cu eșecul unei cereri.
- **Error**: mesaj specific și retry local. 401 → login; 403/revocare →
  șterge cache-ul resursei și cere o stație autorizată; 429 → respectă retry;
  5xx → păstrează ultima copie permisă și arată ora ei. Fără stack trace.
- **Stale**: există date, dar politica serverului le consideră vechi. Arată
  sursă + ora observației + eticheta „Date vechi”. Nu anima fluxul ca live.
- **Offline**: telefonul nu poate contacta EMS. Copia locală eligibilă are
  eticheta „Offline · copie locală din …”; fără copie, empty offline.
  Nu deduce că instalația este oprită. Nicio scriere sensibilă offline.

Calitatea și conectivitatea sunt axe diferite: o estimare poate fi veche,
o simulare poate fi recentă. Etichetele nu se suprascriu reciproc.
Precedență vizuală: acces revocat elimină datele; fără valori arată lipsă;
offline/stale etichetează valorile rămase; measured/estimated/simulated/
declared și acoperirea rămân lângă fiecare metrică. Un total mixt moștenește
proveniența tuturor segmentelor care îl compun.

## Matrice pe ecran

„N/A” înseamnă că starea de date nu se aplică acelui ecran static; nu este
omisiune. Prototipul afișează în acest caz explicația în locul unui fals
timestamp. Conținutul de exemplu disponibil este starea **Ready**.

| ID / ecran | Loading | Empty | Error | Stale | Offline |
|---|---|---|---|---|---|
| S01 Bun venit | N/A, conținut local imediat | N/A, fără date personale | Link politici indisponibil → retry în browser | N/A, text local versionat | Explicații disponibile; continuarea online indicată |
| S02 Cont EMS | Submit în progres, buton blocat contra dublării | Formular gol, câmpuri etichetate | Credențialele invalide/recuperare au mesaje neutre; retry limitat | Sesiune expirată → login; fără date vechi ale altui cont | Nu păstrează parole în coadă; formular fără confirmare de succes |
| S03 Deye | Deschidere browser / stații / prima sincronizare distincte | Neconectat, zero stații eligibile sau fără observații: motiv specific | Anulat, regiune/config nesuportată, auth expirată, quota, provider căzut | Ultima sincronizare cu oră; „Reconectează” numai dacă serverul cere | Starea conexiunii salvată poate fi citită; connect/select/disconnect indisponibile |
| S04 Pairing HA | Emitere cod / așteptare confirmare locală | Neconectat → începe / mai târziu | Cod invalid/folosit/expirat → cod nou; permisiune insuficientă explicată | Tranzacție expirată; nu păstrează codul ca valid | Fără generare/confirmare; la revenire recitește statusul |
| S05 Entități HA | Încarcă doar candidații autorizați local | Niciun candidat → autorizează în HA; niciun selectat → CTA dezactivat | Incompatibilitate domain/unit/device class; versiune în conflict → recitește | Observație veche lângă entitate; unitate schimbată cere remapare | Listă informativă nesensibilă dacă permisă, fără salvare; occupancy ascuns |
| S06 Acasă | Skeleton prima dată; refresh fără a șterge conținutul | Zero stații / așteaptă Deye / metrică lipsă / fără baterie au mesaje distincte | Eroare card locală; acces revocat golește stația | Valorile rămân cu ora reală; fluxul static | Ultimul snapshot permis + oră; fără cache → conectează; HA occupancy ascuns |
| S07 Metrică / istoric | Skeleton doar grafic, păstrează perioada | „Fără date în perioada aleasă”; oferă altă dată | Retry doar seria; nu inventează o curbă | Eticheta perioadei și acoperirea; datele istorice încheiate nu „îmbătrânesc” ca live | Cache pentru perioada disponibilă; altele cer rețea |
| S08 Analiză | Carduri independente | Fără istoric/tarif/prognoză: motiv pe card | Retry pe card | Rezumate cu ediția/ora lor | Rezumate salvate etichetate; navigare numai în copii disponibile |
| S09 Prognoză PV | Skeleton la prima ediție, nu amestecă rulări | Fără forecast/meteo; fără profil → necalibrat, nu forecast absent | Retry forecast; măsurările existente rămân | „Prognoză veche”, issued_at; exclude comparația neeligibilă | Copie etichetată, orizont expirat explicit |
| S10 Estimare factură | Păstrează luna, skeleton valori | Tarif/date lipsă sau calcul dezactivat → total indisponibil, motiv | Retry; fără estimare locală improvizată | As-of și ipoteze vechi; nu redenumește perioada la trecerea lunii | Ultima estimare + luna/as-of; nu recalculează după ceasul telefonului |
| S11 Inbox | Skeleton inițial / progres la paginare | „Nu ai notificări” versus „Niciun rezultat pentru filtre” | Retry pagina, păstrează cardurile existente | Ultima sincronizare inbox; incidentul rămâne la data raportată | Copia permisă, fără decrement permanent necitite; fără copy → explică |
| S12 Setări | Profil/capabilități în progres | Fără stație → cont/privacy rămân disponibile | Retry secțiune; nu revine la valori implicite prezentate ca salvate | Capabilitățile/conexiunile se revalidează înainte de editare | Tema locală funcționează; mutațiile server nu pretind salvare |
| S13 Confidențialitate | Încarcă inventar/consents/export status | Fără integrări/export → explică; contul are totuși date de identitate | Retry; retragere/export confirmate numai de server | Politică schimbată sau consent version vechi → revizuire explicită | Politică locală cu versiune; fără trimitere export/retragere consent |
| S14 Ștergere cont | Reauth / cerere trimisă / status procesare distincte | Nicio cerere activă → explică efectele | Respinsă/ultimul admin/timeout: motiv + status, fără retry orb | Reauth expirată → reautentificare, nu confirmare automată | Acțiune indisponibilă; nu pune cererea în coadă |
| S15 Preferințe notificări | Citește preferințe; afișează progres salvare | Push neactivat → explică + activare opțională | Permisiune OS refuzată → link setări OS; salvare eșuată marcată | Revizuiește ultima versiune înainte de suprascriere | Citește copia permisă; fără modificări server/OS prezentate ca aplicate |
| S16 Detaliu notificare | Încarcă după acces/deep link | Eliminată/indisponibilă → înapoi la inbox | Retry sau acces revocat → golește detaliul | Momentul incidentului și ultima actualizare rămân distincte; fără „acum” | Detaliu permis salvat; citirea se confirmă după reconnect |
| S17 Profil / sesiuni | Listă sesiuni / revocare în progres | Nicio altă sesiune; nu confundă lipsa listei cu zero sesiuni | Retry; revocarea nu este confirmată optimist | Reautentificare înainte de revocare sensibilă | Logout local posibil, revocarea remote cere rețea |

## Stările integrărilor

| Integrare / stare | Semnificație și pas următor |
|---|---|
| Deye disconnected | Fără legătură activă; admin poate conecta. Istoricul autorizat poate exista. |
| Deye connecting / pending selection | Browser/tranzacție în curs sau alegere stație; anularea nu creează o instalare fantomă. |
| Deye connected, awaiting data | Credenziale acceptate și stație asociată; încă nu există măsurători. |
| Deye fresh / stale | Vârsta măsurătorii, nu momentul deschiderii ecranului, decide eticheta. |
| Deye reauth / rate limited / unavailable | Acțiuni diferite: reconectare, așteaptă până la retry_at, retry normal. |
| HA disconnected | Nu apare pe Acasă; Setări oferă conectarea opțională. |
| HA pairing / expired | Cod activ cu termen server; expirat/folosit nu poate autoriza din nou. |
| HA connected, unmapped | Bridge autorizat; încă nu există entități selectate; CTA către S05. |
| HA mapped, awaiting observation | Configurație salvată; nu pretinde existența unei măsurători. |
| HA fresh / stale per entity | Fiecare entitate are propriul timestamp/TTL; puterea proaspătă nu face occupancy proaspăt. |
| HA bridge offline / reauth | Statusul transportului este distinct de telefon offline; reconectare doar dacă este cerută. |
| HA unavailable / incompatible | Valoare necunoscută sau mapare invalidă, niciodată zero/off/home implicit. |
| HA consent withdrawn / disconnected | Oprește colectarea conform confirmării backend; elimină contextul din UI/cache și explică retenția. |

## Exemple obligatorii pentru QA ulterioară

- PV `null`, consum măsurat și SOC exact zero: trei reprezentări corecte.
- Import zero măsurat versus import necunoscut: numai primul poate susține
  un sumar „fără import”, cu condițiile conservative ale backendului.
- PV simulat plus consum măsurat și carry-in vechi: totalul păstrează toate
  limitele de calitate; nu devine „măsurat” după agregare.
- Telefon în alt fus, schimbare DST 23/25 ore, 29 februarie, luni de lungimi
  diferite și trecerea lunii în timp ce utilizatorul consultă un cache.
- Membership revocată în timpul unui request/deep link; cont schimbat în
  timp ce răspunde seria fostului cont; ultimul administrator la ștergere.
- HA revocat, entitate redenumită/unitate schimbată, cod expirat/rejucat,
  app reinstalat; Deye-only rămâne funcțional în toate erorile HA.

Acestea sunt cerințe pentru #199–#210, nu teste native sau hardware efectuate
în PR-ul documentar #197.
