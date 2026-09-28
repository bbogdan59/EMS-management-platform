# Harta solara a statiei

Panoul statiei afiseaza o harta OpenStreetMap in tonuri de gri, pinul casei,
traseul solar al zilei locale, directia curenta, elevatia si orele de rasarit/apus.
Pozitia se recalculeaza la 30 de secunde si la revenirea in tab. Noaptea,
indicatorul devine gri si arata explicit ca soarele este sub orizont.

Harta se afla langa animatia fluxului energetic. Pe dashboard este doar
pentru vizualizare: fara deplasare, zoom, editarea pinului sau capturarea
scroll-ului. Casa ramane centrata inclusiv la redimensionarea ecranului.
Rezumatul „Astazi” apare sub cele doua panouri, pe un rand la desktop.

Cardurile de putere/SOC includ istoricul ultimelor 24 de ore, cu medii la
15 minute din acelasi API de telemetrie ca graficul principal. Istoricul se
actualizeaza la un minut si la revenirea in tab, independent de intervalul
selectat pentru analiza detaliata. Golurile raman intreruperi, zero ramane
valoare valida, iar schimbul cu reteaua pastreaza semnul import/export.
Provenienta simulata, estimata sau intarziata este afisata pe fiecare card;
lipsa datelor si erorile de incarcare au stari distincte.

Configurarea tehnica, asistentul de creare si formularul avansat de creare
permit alegerea coordonatelor prin click, tragerea pinului, centrul hartii sau
geolocalizarea browserului la cerere. Coordonatele raman editabile manual si
se persista numai la salvarea formularului existent, cu RBAC, CSRF, audit si
controlul versiunii existente. Precizia stocata ramane de sase zecimale.
Geolocalizarea dispozitivului poate necesita ajustarea pinului pe casa.

## API web

`GET /stations/{station_id}/data/sun` foloseste sesiunea utilizatorului si
`StationAccess(viewer)`. Raspunsurile au `Cache-Control: no-store`.

- `status`: `ok`, `missing_location` sau `invalid_location`.
- `source`: `calculated`; pozitia astronomica nu este telemetrie masurata.
- `location`: `latitude`/`longitude`, sau `null` daca nu sunt utilizabile.
- `timezone`, `calculated_at`: fusul statiei si momentul UTC al calculului.
- `current`: `at`, `azimuth`, `elevation`, `is_daylight`; `null` fara locatie.
- `local_date`, `day_start`, `day_end`: ziua statiei si limitele UTC exclusive
  la sfarsit; zilele DST pot avea 23/25 ore.
- `path`: pozitii la cinci minute plus momentele evenimentelor solare.
- `sunrise`, `sunset`, `transit`: pozitii la aceste evenimente; `null` daca
  evenimentul nu apare in ziua locala, inclusiv zilele/noptile polare.

Azimutul este in grade de la nord in sens orar (90° est, 180° sud).
Elevatia aparenta include refractia atmosferica standard. `is_daylight`
foloseste centrul geometric la minimum -0.8333°, aproximarea astronomica
standard pentru marginea superioara a soarelui la orizont. Calculul foloseste
NREL SPA prin pvlib; nu necesita un serviciu extern. Traseele zilnice sunt
memorate in cache limitat, separat pentru coordonate, fus si data.

Diagrama proiecteaza cerul peste harta: raza scade liniar intre orizont (0°)
si zenit (90°), cu centrul ancorat pe pin inclusiv la deplasarea hartii.
Nu reprezinta distante la sol sau o simulare de umbre. Cladirile, relieful,
norii si refractia reala pot schimba vizibilitatea. La esecul actualizarii,
indicatorul curent este eliminat si apare o stare de indisponibilitate.

Leaflet 1.9.4 este servit local, inclusiv CSS, imagini si licenta, prin
`npm run vendor`. Numai tile-urile sunt externe; browserul pastreaza cache-ul
HTTP si atribuirile OpenStreetMap. Hartile se initializeaza cand devin
vizibile. Nu exista prefetch/offline bulk sau geocodare de adrese.

Referinte: [pvlib solarposition](https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.solarposition.get_solarposition.html),
[Leaflet](https://leafletjs.com/reference.html),
[politica tile-urilor OpenStreetMap](https://operations.osmfoundation.org/policies/tiles/).

## Verificare locala

```sh
ruff check .
pytest tests/unit/test_sun_map_service.py tests/integration/test_station_sun_map.py -q
pytest tests/e2e/test_station_sun_map.py -q
```

Testele de browser folosesc tile-uri locale simulate si raspunsuri solare
deterministe; nu depind de disponibilitatea serviciului cartografic extern.
