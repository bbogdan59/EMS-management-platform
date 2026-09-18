# Matricea de capabilitati RBAC (issue #23)

Sursa de adevar pentru autorizare este intotdeauna serverul, verificat la
FIECARE endpoint (HTML, API, SSE, export, joburi, comenzi) prin
`app/api/deps.py` (`StationAccess`/`OrganizationAccess`, ambele parametrizate
cu `min_role`) si `app/core/rbac.py` (`role_at_least` + helperele
`can_*`, folosite pentru randare conditionala in UI -- NICIODATA singurul
mecanism de blocare). Acest document descrie ce verifica deja codul, nu
introduce roluri noi.

## Roluri

Patru roluri, ordonate strict (`app/core/rbac.py::_RANK`):

`viewer` (0) < `operator` (1) < `organization_admin` (2) < `platform_admin` (3)

- **`viewer`**, **`operator`**, **`organization_admin`** sunt roluri DE
  ORGANIZATIE (`Membership.role`, un utilizator poate avea membership-uri
  diferite in organizatii diferite). Validate server-side printr-un
  allowlist explicit (`auth_service.ORGANIZATION_ROLES`), nu un camp liber
  -- o valoare in afara acestui set e respinsa (invitatie sau schimbare de
  rol deopotriva).
- **`platform_admin`** e un flag GLOBAL pe `User` (`User.is_platform_admin`),
  independent de orice organizatie -- nu poate fi acordat printr-o
  invitatie de organizatie (`ORGANIZATION_ROLES` nu il contine deloc; o
  incercare de a-l seta printr-o schimbare de rol e respinsa explicit de
  `membership_service.change_role`). Se acorda doar prin bootstrap
  (`auth_service.bootstrap_first_admin`, o singura data) sau direct in baza
  de date.

O `Membership` are si un flag `is_active` (issue #23) -- `is_active=False`
e tratat PESTE TOT unde e verificata apartenenta (acces organizatie/statie,
SSE) identic cu absenta completa a membership-ului: fara acces, indiferent
de rol.

## Matricea (rezumat operational)

| Capabilitate | viewer | operator | organization_admin | platform_admin |
|---|---|---|---|---|
| Vede statia/organizatia proprie (dashboard, KPI, grafice) | DA | DA | DA | DA (orice organizatie) |
| Export CSV date proprii | DA | DA | DA | DA |
| Modifica preferinte, declanseaza comenzi manuale, suspenda automatizarea | - | DA | DA | DA |
| Re-optimizare manuala | - | DA | DA | DA |
| Configurare tehnica statie/invertor, tarife, creare statie | - | - | DA | DA |
| Administrare membri (listare/invitare/schimbare rol/dezactivare/eliminare) proprie organizatie | - | - | DA | DA |
| Suspendare/arhivare/restaurare ORGANIZATIE (issue #24) | - | - | - | DA (doar platform_admin) |
| Acces la ORICE organizatie/statie (panou admin, cross-tenant) | - | - | - | DA |
| Atribuire/revocare `platform_admin` | - | - | - | (doar bootstrap/DB directa) |

Note:
- O organizatie `suspended` blocheaza pentru non-platform_admin orice
  actiune care cere `operator`+ (scriere/operare), dar pastreaza citirea
  (`viewer`) -- vezi `app/api/deps.py::_check_organization_status` si
  `docs/LIMITATIONS.md` sectiunea 17 (issue #24). O organizatie `archived`
  blocheaza TOT accesul non-platform_admin, inclusiv citirea.
  `platform_admin` nu e afectat de niciuna dintre stari.
- Ultimul `organization_admin` ACTIV al unei organizatii nu poate fi
  retrogradat, dezactivat sau eliminat (`membership_service._assert_not_last_admin`)
  -- ar lasa organizatia fara niciun manager capabil sa administreze
  membrii/statiile.
- "Transferul" rolului de manager (organization_admin) se face prin
  schimbare de rol: promoveaza intai un alt membru la `organization_admin`,
  apoi (optional) retrogradeaza-l pe cel vechi -- protectia "ultimul admin"
  de mai sus garanteaza ca acest pas doi ramane mereu posibil dupa primul.
- `platform_admin` poate atribui/revoca manageri din panoul de backoffice
  (`/admin/organizations/{id}/members/...`) FARA impersonare -- actioneaza
  explicit ca platform_admin (auditat cu `actor_label=platform_admin.email`),
  nu "ca si cum ar fi" un membru al organizatiei respective.

## Verificare server-side pe endpoint (nu doar UI)

| Ruta | Gate |
|---|---|
| `GET /organizations/{id}`, export, dashboard read-only | `OrganizationAccess`/`StationAccess(min_role="viewer")` |
| `POST /stations/{id}/preferences`, comenzi manuale, suspendare automatizare, re-optimizare | `StationAccess(min_role="operator")` |
| `POST /organizations/{id}/stations` (creare statie), configurare invertor/tehnica, tarife | `StationAccess`/`OrganizationAccess(min_role="organization_admin")` |
| `POST /organizations/{id}/invitations`, `.../members/*`, `.../invitations/*` (issue #23) | `OrganizationAccess(min_role="organization_admin")` |
| `GET /stations/{id}/integrations/deye` (stare conector Deye Cloud) | `StationAccess(min_role="viewer")` |
| `POST /stations/{id}/integrations/deye/connect\|select\|disconnect\|import-history` (issue #43) | `StationAccess(min_role="organization_admin")` -- acelasi prag ca gestiunea dispozitivelor |
| `/admin/*` (backoffice cross-tenant, inclusiv lifecycle organizatie si administrare membri) | `require_platform_admin` (verifica `User.is_platform_admin`, nu un rol de organizatie) |
| `GET /stations/{id}/sse` (flux live) | `StationAccess(min_role="viewer")`, re-verificat la FIECARE ciclu de polling (nu doar la deschiderea conexiunii) -- o membership dezactivata/eliminata sau o sesiune revocata inchide fluxul in cel mult `POLL_INTERVAL_SECONDS` |

Niciun endpoint din tabelul de mai sus nu se bazeaza pe ascunderea unui
buton in UI -- fiecare verifica explicit resursa/tenantul/rolul, confirmat
prin teste cross-tenant negative (`tests/integration/test_org_isolation.py`,
`tests/integration/test_organization_members_routes.py`,
`tests/unit/test_control_safety.py`).

## Re-autentificare la schimbari sensibile

Aplicatia nu are (inca) un flux dedicat de "re-confirmare cu parola" pentru
actiuni sensibile (schimbare de rol, dezactivare/eliminare membership,
suspendare/arhivare organizatie) -- CSRF-ul existent + sesiunea autentificata
raman baza de autorizare a cererii. Ce exista concret, ca substitut
functional pentru "efectul" unei re-autentificari:

- Orice schimbare de rol, dezactivare sau eliminare a unei membership
  REVOCA imediat toate sesiunile web ale utilizatorului afectat
  (`auth_service.revoke_all_sessions_for_user`) -- noul nivel de acces (sau
  lipsa lui) se aplica din urmatoarea cerere, nu abia la expirarea naturala
  a sesiunii; utilizatorul trebuie sa se re-autentifice pentru a continua,
  moment in care orice noua sesiune reflecta deja starea curenta.
- Suspendarea/arhivarea unei organizatii intregi revoca la fel sesiunile
  TUTUROR membrilor ei (issue #24, `revoke_all_sessions_for_users_in_organization`).

O reconfirmare explicita cu parola/al doilea factor (MFA) ramane in afara
scopului acestei versiuni (aplicatia nu are MFA deloc inca) -- documentat
aici ca decizie deliberata, nu omisiune ascunsa.

## In afara scopului (deliberat)

- **Impersonare** platform_admin -> utilizator dintr-o organizatie: NU
  exista -- platform_admin are deja acces direct (bypass explicit in
  `StationAccess`/`OrganizationAccess`), nu are nevoie sa "devina" un
  membru pentru a vedea/administra o organizatie.
- **MFA/re-autentificare cu parola** pentru actiuni sensibile -- vezi
  sectiunea de mai sus.
- **Migrarea datelor existente** la noul flag `Membership.is_active` --
  migrarea Alembic seteaza toate membership-urile existente ca active
  (`server_default=true`), comportament identic cu inainte de acest issue.
