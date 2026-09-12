# ADR 0001: Transport pentru actualizarea live a dashboard-ului

Status: Acceptat -- issue #50.

## Context

Dashboard-ul stației (`/stations/{id}`) trebuie să reflecte în timp real
valorile de telemetrie/preț/plan fără ca utilizatorul să reîncarce pagina.
Platforma are deja un flux SSE (`GET /stations/{id}/sse`,
`app/web/routes/sse.py`, issue #6): reautentifică sesiunea/membership-ul la
FIECARE ciclu de polling (nu doar la deschidere) și trimite un rezumat
complet al stației la interval fix (`POLL_INTERVAL_SECONDS = 5`).

Issue #50 cere o evaluare explicită SSE vs. WebSocket înainte de a alege.

## Opțiuni

**WebSocket** ar aduce: canal bidirecțional (util pentru control live al
device-ului), un singur handshake reutilizabil pentru mesaje foarte
frecvente. Costuri: infrastructură nouă (nu există încă niciun handler
WebSocket în platformă), reautentificare/revocare mid-conexiune trebuie
reimplementată de la zero (SSE o are deja, verificată), niciun beneficiu
pentru cazul de față -- browserul doar CITEȘTE actualizări, nu trimite
comenzi prin acest canal (comenzile către device folosesc deja un flux
separat, `Command`/`CommandEvent`, prin `app/api/v1/commands.py`, nu
dashboard-ul de vizualizare).

**SSE (păstrat, extins)**: unidirecțional, exact ce cere acest issue;
reconectare automată nativă în `EventSource` (browser), deja folosită
(`app.js::emsConnectSSE`); autentificare prin cookie de sesiune (fără token
in query-string, cerință explicită a issue-ului) -- deja adevărat.

## Decizie

**Rămânem pe SSE.** Nu există niciun avantaj clar al WebSocket pentru
telemetrie unidirecțională, iar introducerea lui ar însemna reconstruirea
de la zero a hardening-ului de autorizare/revocare deja verificat pe SSE
(issue #6/#23). WebSocket rămâne o opțiune viitoare STRICT dacă apare o
cerință reală de control bidirecțional prin dashboard (nu doar vizualizare)
-- nu e cazul acestui issue.

## Ce s-a schimbat totuși (fără fluxul de transport)

Fluxul SSE existent trimite un singur bloc JSON nediferențiat ("rezumat").
Issue #50 cere un contract per-metrică versionat. Am extins payload-ul,
NU transportul:

- Fiecare mesaj (`snapshot`/`delta`) conține o listă de metrici, fiecare cu
  `metric`, `value`, `unit`, `measured_at`, `received_at`, `quality`,
  `source` -- vezi `dashboard_service.get_live_metrics`.
- Fiecare eveniment SSE poartă un `id:` (folosit nativ de `EventSource` ca
  `Last-Event-ID` la reconectare) egal cu un `sequence` monoton per
  conexiune, inclus și în payload.
- **Contract de resume, documentat explicit (nu ascuns):** serverul NU
  păstrează un jurnal de evenimente trecute -- nu există buffer de replay.
  Prima emisie a FIECĂREI conexini (inclusiv la reconectare) e un eveniment
  `snapshot` (stare completă curentă), nu un `delta`. Aceasta ESTE
  mecanismul de "refresh la gap" cerut de issue: reconectarea însăși aduce
  starea completă prin SSE, fără o cerere HTTP separată. Clientul nu
  trebuie să presupună niciodată continuitate de secvență peste o
  reconectare -- doar în cadrul aceleiași conexiuni TCP (unde SSE
  garantează deja ordinea, deci "reorder" nu se poate produce practic).
- **Rate limiting/coalescing:** fiecare tur de polling (5s) recalculează
  toate metricile și trimite DOAR pe cele schimbate față de ultima emisie
  -- mai multe schimbări fizice între doi timpi de poll se contopesc
  automat într-un singur `delta` (nu se retrimite fiecare eșantion brut).
  Nu a fost nevoie de un token-bucket separat: intervalul de 5s e deja
  bugetul de rată.
- **Stări de conexiune** (`connecting`/`live`/`stale`/`offline`) afișate
  explicit în UI (`dashboard.js`), derivate din callback-urile
  `emsConnectSSE` (`onopen`/`onerror`) plus un watchdog client-side care
  marchează `stale` daca nu s-a primit niciun mesaj (inclusiv heartbeat) in
  ultimele `3 x POLL_INTERVAL_SECONDS`.

## Consecințe

- Nicio schimbare de infrastructură (fără worker WebSocket, fără canal nou
  in Nginx/proxy).
- `dashboard_service.get_summary` (folosit la randarea inițială a paginii)
  rămâne NESCHIMBAT -- `get_live_metrics` e o funcție nouă, separată, ca sa
  nu riște nicio regresie pe testele existente ale randării HTTP inițiale.
- Rămas în afara scopului (documentat și in `docs/LIMITATIONS.md`): un
  jurnal de evenimente persistat pentru replay real peste un gap de
  reconectare de ordinul minutelor -- inutil aici, pentru ca reconectarea
  oricum aduce un `snapshot` complet, la fel de ieftin ca orice replay
  parțial ar fi fost.
