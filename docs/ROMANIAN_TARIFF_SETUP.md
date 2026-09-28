# Configurare de prosumator din factura

Pagina `/stations/{id}/tariffs` include formularul **Prosumator Romania** pentru
contracte cu energie activa fixa si compensare lunara 1:1. Se completeaza
o singura data energia activa, distributia, serviciile de sistem, TG, TL,
cogenerarea, CfD, certificatele verzi si acciza, toate in lei/kWh fara TVA.
TVA pentru import, TVA pentru export si abonamentul lunar sunt separate.
Virgula si punctul sunt acceptate ca separator zecimal. Campurile lipsa sunt
respinse; zero este o valoare explicita valida.

Butonul **Completeaza exemplul** incarca valorile din factura furnizata de
utilizator in cerinta: energie activa 0.45, distributie 0.355340, sistem
0.014700, TG 0.003630, TL 0.036450, cogenerare 0.014500, CfD 0.000144,
certificate verzi 0.07401920 si acciza 0.007680. Acestea sunt valori de
exemplu, nu un nomenclator national actualizat automat. Nu se aplica unui
contract pana cand un administrator salveaza formularul.

## Formule

Cu `A` energia activa, `G` TG, `R` suma celorlalte componente de import:

- TG separat la import: pret import fara TVA = `A + G + R`.
- TG inclus deja in A: pret import fara TVA = `A + R`.
- Pret export fara TVA = `A - G`, indiferent de prezentarea TG la import.
- TVA se aplica independent pretului de import, respectiv pretului de export,
  conform celor doua cote introduse de utilizator.

Scaderea TG la export este formula ceruta explicit pentru acest model de
contract. Nu se deduce TVA-ul exportului din cota de consum. Valorile initiale
ale formularului sunt 21% la import si 0% la export, editabile si vizibile
inainte de salvare. Exemplul facturii nu modifica aceste doua cote.

Pentru o simulare lunara, `I` = kWh extrasi, `E` = kWh introdusi, `C=min(I,E)`:

- Factura de import = `I * pret_import_fara_TVA + abonament`, apoi TVA import.
- Credit aplicat = `C * (A - G)`, apoi TVA export configurat.
- Estimare de plata = factura de import minus creditul aplicat.
- Surplusul `max(E-I,0)` este afisat separat, evaluat la `A-G` fara TVA.

Defalcarea arata separat `C*A` (energie activa compensata), `C*G` (TG retinut)
si fiecare taxa aplicata intregului import I. Surplusul nu plateste automat
distributia, taxele sau abonamentul. TVA de pe factura de import nu dispare
implicit cand TVA export este zero.

De exemplu, la 100 kWh import si 100 kWh export, cu preturile din imagine,
TG separat, TVA import 21% si export 0%: importul fara TVA este 95.646320 lei,
TVA import 20.08572720 lei, creditul 44.637000 lei, iar diferenta este
71.09504720 lei (afisata 71.10). Calculele sunt Decimal; rotunjirea de afisare
nu modifica tarifele salvate.

Simularea este pentru cantitatile introduse si un singur set de tarife. Nu
este o factura emisa, o raportare automata din telemetrie sau un registru de
solduri reportate. Nu aplica solduri vechi, expirari, schimbari de pret in
cursul perioadei sau conditii comerciale specifice furnizorului. Pentru un
pret nou se creeaza o noua versiune cu data de inceput corespunzatoare.

## Persistenta si compatibilitate

`save_pair` proiecteaza defalcarea in cele doua contracte fixe existente:
importul pastreaza distributia, transportul TG/TL si celelalte taxe; exportul
primeste pretul `A-G` si cota proprie TVA, fara taxe de import. Toate calculele
existente de pret efectiv (dashboard, istoric, optimizare si EV) folosesc
aceeasi functie `compute_effective_price_lei_per_kwh`. Valorile marginale
de export sunt valori economice, nu promisiuni de plata imediata a surplusului.

Fiecare versiune salveaza si `invoice_breakdown`, un snapshot JSON cu valori
Decimal serializate ca siruri. `settlement_method=ro_prosumer_monthly` identifica
modelul. Campul legacy `settlement_interval_days=30` nu defineste limitele
unei luni calendaristice si nu este folosit de simulare pentru date.
Contractele fixe/dinamice si decontarile existente raman disponibile prin
formularul avansat. Reclasificarea unui contract OPCOM existent este respinsa,
pentru a nu modifica semnificatia versiunilor istorice.

Salvarea ambelor directii este atomica. Blocarea PostgreSQL a statiei si
tarifului serializeaza atat formularul nou, cat si cel avansat. Un token al
ultimelor versiuni detecteaza un formular devenit vechi intre incarcare si
salvare. Erorile pastreaza valorile introduse. Daca exportul nu poate fi
salvat, nici importul nu este modificat. Actiunea se auditeaza cu ambele ID-uri.

Data optionala de inceput este interpretata in fusul statiei si stocata in
UTC. Gol inseamna instantul salvarii. Orele inexistente/ambigue la DST sunt
respinse explicit. Versiunile noi nu se suprapun retroactiv peste o versiune
deschisa cu data egala sau ulterioara.

Migrarea `e62a19d04b73` largeste cele sase preturi pe kWh la `Numeric(16,8)`
si adauga snapshotul optional. Downgrade-ul arhiveaza snapshoturile in
`legacy_tariff_breakdowns` si pastreaza precizia marita (compatibila cu codul
vechi), pentru a nu rotunji ireversibil valori reale. NULL, zero si marjele
indexate negative sunt pastrate. Upgrade-ul restaureaza snapshoturile pentru
versiunile care mai exista. Codul anterior nu cunoaste decontarea noua si o
trateaza ca formula indisponibila, conform protectiei existente.

## HTTP si verificare

- `POST /stations/{id}/tariffs/romania/preview`: viewer, CSRF, JSON validat
  `RomanianTariffPreview`, raspuns fara cache, sume Decimal ca siruri; fara
  scrieri. `import_kwh` sau `export_kwh` lipsa => rezultat lunar indisponibil.
- `POST /stations/{id}/tariffs/romania`: organization_admin, CSRF, form,
  `expected_revision`, optional `effective_from`, audit; 303 la succes,
  422 cu formularul pastrat la eroare. Integrare cu pasul de wizard existent.

Testele acopera aritmetica exacta a exemplului, TG inclus/separat, cote TVA
independente, deficit/egalitate/surplus, NULL/zero, precizie, DST/an bisect,
RBAC/CSRF, istoricul, rollback atomic, concurenta, migrare dus-intors si fluxul
in browser pe desktop/mobil.

## Surse verificate la implementare (28 septembrie 2026)

- [Metodologia ANRE de comercializare a energiei prosumatorilor](https://legislatie.just.ro/Public/DetaliiDocument/252374): compensare, facturare, tratarea distincta a surplusului si aplicarea TVA dupa caz.
- [Oferta Hidroelectrica publicata pentru aprilie 2025](https://cdn.hidroelectrica.ro/cdn/furnizare/27_03/oferta_tip_casnic_prosumator_cp-0104-3004-25.2.pdf): exemplu documentat de pret al energiei active care include deja TG; nu este folosit ca tarif actual.
- [ANAF, modificari TVA](https://static.anaf.ro/static/10/Ploiesti/modificari_tva.pdf): cota standard 21% de la 1 august 2025. Formularul permite cota efectiva a contractului/perioadei.
- [ANRE, proiect tranzitoriu publicat la 21 septembrie 2026](https://anre.ro/proiect-de-ordin-privind-stabilirea-unor-masuri-tranzitorii-pentru-aplicarea-mecanismului-de-compensare-cantitativa-in-cazul-contractelor-de-vanzare-cumparare-a-energiei-electrice-aflate-in-derulare-2/): modificarile aflate in consultare nu sunt aplicate automat drept reguli in vigoare. Eligibilitatea si conditiile se verifica fata de contract.
