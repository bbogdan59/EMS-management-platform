# Discovery: Spark Solar Assistant -> EMS "date -> verdict -> actiune"

Consultat: 2026-09-15.

Surse publice folosite:
- GreenLead Spark landing page: https://www.greenlead.ro/spark-solar-assistant?from=nav
- Google Play listing: https://play.google.com/store/apps/details?id=ro.greenlead.app
- Apple App Store listing: https://apps.apple.com/ro/app/spark-solar-assistant/id6788822421

Acest document separa strict afirmatiile publice despre Spark de inferentele
pentru EMS. Nu copiaza UI, texte comerciale sau presupuse API-uri interne.

## Afirmatii din surse

| Domeniu | Afirmatie publica | Sursa | Nota de risc |
| --- | --- | --- | --- |
| Conectori | Spark declara conectare la cloud-ul invertorului pentru Sungrow, Deye, Growatt, Huawei FusionSolar si Solis. | GreenLead landing page, Google Play, App Store | Compatibilitatea exacta poate depinde de model si accesul contului; nu presupune API-uri interne. |
| Monitorizare | Afiseaza productie, consum, baterie si schimb cu reteaua, traduse in limbaj non-tehnic. | GreenLead landing page, Google Play, App Store | "Tradus" este pozitionare de produs; EMS trebuie sa faca reguli verificabile, nu copy. |
| Alarme | Promite detectie de probleme, notificare si explicarea severitatii/semnificatiei. | GreenLead landing page, Google Play | Nu cunoastem catalogul real de reguli sau praguri. |
| Verdict | Promite o verificare de rutina cu 9 metrici si o concluzie unica. | GreenLead landing page, Google Play, App Store | Numarul "9" este o decizie de produs Spark, nu o obligatie EMS. |
| Chat contextual | Spark+ raspunde folosind datele reale ale sistemului utilizatorului. | GreenLead landing page, Google Play, App Store | EMS trebuie sa impuna grounding si citarea inputurilor inainte de orice assistant conversational. |
| Troubleshooting | Flux ghidat: tip dispozitiv, cod eroare, poza display, diagnostic, rezumat pentru instalator. | Google Play, App Store | Pozele/codurile sunt capabilitati de produs distincte; in EMS pot intra mai tarziu ca handoff, nu ca prima regula. |
| Recomandari zilnice | Dimineata recomanda cand merita folositi consumatorii mari; seara ofera recap. | Google Play, App Store | Necesita forecast, contract, consumatori flexibili si provenienta; nu trebuie activat fara aceste inputuri. |
| Pret contract | Calculeaza energia in banii contractului real, nu in preturi spot irelevante facturii. | Google Play, App Store, GreenLead landing page | EMS are deja `TariffVersion`/`compute_effective_price_lei_per_kwh`; trebuie propagat in verdicturi. |
| Siguranta | Declara read-only pentru monitorizare/diagnoza si ca nu inlocuieste instalator/electrician. | GreenLead landing page | EMS trebuie sa pastreze aceeasi separare: recomandare/verdict separat de control fizic. |
| Planuri | Free include dashboard, o verificare, un troubleshooting si mesaje limitate; Pro adauga nelimitat si raport saptamanal. | GreenLead landing page, app stores | Nu implica nimic despre monetizarea EMS. |

## Matrice EMS: paritate si gap

| Capabilitate | Exista in EMS acum | Gap principal | PR recomandat |
| --- | --- | --- | --- |
| One-station dashboard | Da: dashboard station-first, carduri de energie/cost/provenienta. | Lipseste un verdict agregat "ce inseamna" cu confidence si actiuni. | Health engine v1. |
| Telemetrie canonica | Da: `TelemetryRaw`, agregari cu coverage, Deye Cloud read-only. | Lipsesc reguli care combina freshness, coverage si sursa in concluzii explicabile. | Health engine v1. |
| Weather/PV forecast | Da: Open-Meteo, PV forecast versionat, backtest. | Lipsesc verdicturi care compara productia reala cu asteptarea PV pe ferestre robuste. | PV underperformance rule. |
| Contract/cost efectiv | Da: tarife fixe/indexate OPCOM, export separat, provenienta. | Dashboard-ul financiar nu produce inca "actiune" prioritizata. | Cost anomaly/contract insight. |
| Alarme | Partial: tabel `Alert`, alerte active in admin, unele importuri genereaza alert. | Nu exista lifecycle complet: regula, deduplicare, explicatie, dismiss/snooze, resolved evidence. | Alert lifecycle v1. |
| Installer handoff | Nu. | Lipseste export compact cu context, timeline, masuratori, pasi deja incercati. | Installer pack PDF/CSV/JSON. |
| Chat contextual | Nu. | Necesita corpus grounded, surse, permisiuni, guardrails; nu trebuie sa comande dispozitive. | Later, dupa health rules. |
| Weekly report | Nu. | Necesita sumar health/cost/PV, baseline si metrici de produs. | Weekly report dupa alert lifecycle. |
| Multi-cloud connectors | Partial: Deye Cloud read-only. | Sungrow/Growatt/Huawei/Solis necesita research si contracte API separate. | Connector roadmap separat. |

## Health checks initiale propuse

Fiecare regula trebuie versionata (`rule_id`, `rule_version`), sa produca
`status` (`ok`, `warning`, `critical`, `unknown`), `confidence`, `inputs`,
`evidence_window`, `reason`, `recommended_action`, `escalation_required` si
`resolved_when`. `unknown` este rezultat valid si nu trebuie ascuns.

| ID | Formula | Date necesare | Prag initial | Confidence | Actiune sigura |
| --- | --- | --- | --- | --- | --- |
| data_freshness | ultima telemetrie per sursa si metrica | `TelemetryRaw.measured_at`, sursa, device status | warning >15 min, critical >60 min fara date | ridicat daca device activ, mediu pentru Deye Cloud latent | Verifica internet/device; nu recomanda comenzi. |
| coverage_quality | coverage orar/zi pe pv/load/grid/battery/soc | agregari telemetry coverage | warning <0.8, critical <0.5 | proportional cu coverage | Marcheaza verdicturile dependente ca `unknown`. |
| pv_vs_forecast | productie PV reala / PV forecast pe daylight | agregari PV, `PvForecast`, sun window | warning sub 70% pe 3h daylight, critical sub 40% | mediu; scade cand cloud forecast lipseste/stale | Verifica umbrire, sigurante, erori invertor; escaladeaza daca persista. |
| inverter_silent | productie, grid si battery lipsesc simultan | telemetry freshness, device heartbeat | critical dupa 60 min in daylight | ridicat daca exista heartbeat lipsa | Verifica alimentare/conexiune; installer daca nu revine. |
| grid_export_unexpected | export mare cand `grid_export_limit_kw=0` sau contract export lipsa | telemetry grid, config, tariff export | warning la >0.2 kW peste 15 min | mediu; depinde de semnul grid validat | Verifica setari invertor/contor; nu modifica limite automat. |
| battery_not_following_plan | SOC/putere baterie nu se misca dupa plan shadow/live | plan, command ACK, telemetry battery/SOC | warning dupa doua intervale planificate | mediu; ridicat doar cu telemetry fresh | Afiseaza diferenta plan vs masurat; cere verificare mod invertor. |
| soc_stale_or_invalid | SOC lipsa, vechi sau in afara 0..100 | telemetry SOC | critical daca live control ar depinde de el | ridicat | Blocheaza live optimization; cere reconectare senzor/BMS. |
| tariff_exactness | cost efectiv necunoscut pentru intervale indexate | tariff version, OPCOM interval, provenance | warning cand pret lipsa/sintetic | ridicat | Explica de ce costul exact lipseste; recomanda import OPCOM/backfill. |
| negative_price_opportunity | pret OPCOM negativ + baterie/EV disponibil | OPCOM, tariff, config, preferences, forecast | informational daca calcul economic exact | mediu | Recomandare manuala, nu comanda directa. |
| repeated_alert | aceeasi regula in warning/critical in N ferestre | alert history | escalare dupa 3 recurente/7 zile | ridicat | Pregateste installer pack. |
| cloud_connector_error | Deye Cloud auth/API error | `DeyeCloudConnection.status`, sync message | warning imediat, critical >24h | ridicat pentru auth error | Reconectare cont; nu fallback la date fabricate. |
| command_verification_gap | comanda ACK fara confirmare in telemetry | command, ACK, telemetry after window | critical pentru live commands | ridicat cand exista comanda live | Marcheaza "neverificat"; cere verificare fizica/installer. |

## Wireflow: alerta -> rezolvare

1. Rule engine ruleaza in background pe o fereastra determinista si scrie un
   eveniment de health cu versiunea regulii si snapshot de inputuri.
2. Daca statusul este `unknown`, UI-ul afiseaza "nu pot da verdict" si lista
   inputurilor lipsa/stale. Nu se transforma in `ok`.
3. Daca statusul este `warning`/`critical`, sistemul cauta o alerta deschisa cu
   aceeasi cheie de deduplicare (`station_id`, `rule_id`, resursa afectata).
4. O alerta noua primeste severitate, titlu, explicatie, evidence window,
   confidence si actiuni sigure. O alerta existenta primeste `last_seen_at` si
   counter, fara spam.
5. UI-ul arata intai verdictul, apoi "de ce credem asta", "ce poti verifica
   singur" si "cand chemi instalatorul".
6. Utilizatorul poate marca "nu e relevant" sau "am verificat"; feedback-ul nu
   sterge evidenta, doar intra in calibrari si raportul de precision.
7. Rezolvarea automata cere o conditie explicita `resolved_when`, de exemplu
   doua ferestre consecutive `ok` cu coverage suficient.
8. Escalarea genereaza un installer pack: date statie, config relevant,
   firmware/model daca exista, timeline, alerte, comenzi/ACK/verificare,
   snapshot meteo/PV si ce a incercat utilizatorul.

## Criterii de siguranta si escaladare

- Nicio regula nu are voie sa recomande desfacerea tabloului, cablare,
  modificari de protectii sau interventii AC/DC. Acestea merg direct la
  electrician/instalator autorizat.
- Actiunile software sunt recomandari pana cand exista capabilitati declarate,
  compatibilitate model/firmware si verificare post-comanda.
- `unknown`, `stale`, `synthetic`, `estimated` si `shadow` trebuie propagate in
  verdict si in installer pack.
- Pentru live control, command ACK si aplicarea verificata raman stari diferite.
- False-positive feedback nu dezactiveaza global o regula; propune doar praguri
  per statie dupa review.

## Metrici produs

| Metrica | Definitie | Anti-pattern de evitat |
| --- | --- | --- |
| Time-to-understand | Timp de la deschiderea alertei pana la prima actiune/ack util. | Nu optimiza prin copy alarmist sau ascunderea detaliilor. |
| Alert precision | Alarme confirmate utile / alarme actionate. | Nu numara alarme ignorate ca rezolvate. |
| Dismissed rate | Procent alarme inchise ca nerelevante, pe regula si statie. | Nu penaliza utilizatorul pentru dismiss. |
| Unknown rate | Verdicturi blocate de date lipsa/stale/sintetice. | Nu converti unknown in ok pentru KPI-uri frumoase. |
| Resolved without installer | Alarme inchise prin actiuni sigure si confirmare ulterioara. | Nu atribui rezolvare EMS fara evidenta post-actiune. |
| Installer pack usefulness | Pachete exportate care duc la o interventie fara cereri suplimentare de date. | Nu include parole, tokenuri sau date irelevante. |
| Recommendation acceptance | Recomandari urmate manual si efect observat ulterior. | Nu folosi dark patterns sau presiune de tip "pierzi bani". |

## Backlog propus in PR-uri mici

1. Health rule schema si runner determinist read-only.
   - Tabele: `health_rule_runs`/`health_rule_findings` sau extindere controlata
     a `alerts`, cu snapshot JSON de inputuri.
   - Reguli initiale: `data_freshness`, `coverage_quality`, `soc_stale_or_invalid`.

2. Alert lifecycle v1.
   - Deduplicare, `first_seen_at`/`last_seen_at`, `resolved_at`, feedback
     dismiss/snooze si resolved evidence.

3. Dashboard verdict card.
   - Un singur sumar station-first: `ok`, `needs_attention`, `unknown`.
   - Afiseaza freshness/provenance si link catre evidenta, nu doar text.

4. PV performance checks.
   - `pv_vs_forecast` cu ferestre daylight, coverage si forecast issued_at.
   - Backtest/MAE intra in confidence, nu in copy promotional.

5. Financial insight checks.
   - `tariff_exactness`, cost anomaly, negative-price opportunity, toate
     bazate pe `TariffVersion` si OPCOM real/sintetic marcat.

6. Installer handoff pack.
   - Export fara secrete: config, device inventory, telemetry summary, alerte,
     command verification gap si actiuni incercate.

7. Weekly health report.
   - Rezumat health/cost/PV, unknown rate si alerte recurente.

8. Assistant conversational grounded.
   - Doar dupa health/alerts/report; raspunsuri cu surse interne si fara
     comenzi directe.

9. Connector research pentru Sungrow/Growatt/Huawei/Solis.
   - Cate un ticket per vendor, cu termeni API, securitate, rate limits,
     consent si model de date.

## Recomandare

Primul PR de implementare ar trebui sa fie "Health rule schema si runner
determinist read-only", nu chat si nu conectori noi. Acesta creeaza contractul
stabil pentru verdicturi, alerte, rapoarte si viitorul assistant fara sa amestece
date sintetice, stale sau neverificate cu fapte masurate.
