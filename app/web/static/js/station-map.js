/* global L */
(() => {
  "use strict";

  const pinIcon = () => L.divIcon({
    className: "house-map-pin", iconSize: [30, 30], iconAnchor: [15, 15],
    html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 10 9-7 9 7v10H3Z M9 20v-7h6v7"/></svg>',
  });

  function createMap(root, location, zoom) {
    const canvas = root.querySelector("[data-map-canvas]");
    const interactive = root.hasAttribute("data-location-picker");
    const map = L.map(canvas, {
      scrollWheelZoom: false, zoomAnimation: false, maxZoom: 20,
      dragging: interactive, touchZoom: interactive, doubleClickZoom: interactive,
      boxZoom: interactive, keyboard: interactive, zoomControl: interactive,
      tapHold: interactive,
    })
      .setView(location, zoom);
    const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxNativeZoom: 19, maxZoom: 20, referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
    }).addTo(map);
    const error = root.querySelector("[data-map-error]");
    tiles.on("tileerror", () => { error.hidden = false; });
    tiles.on("loading", () => { error.hidden = true; });
    const resizeObserver = new ResizeObserver(() => map.invalidateSize({animate: false}));
    resizeObserver.observe(canvas);
    map.on("unload", () => resizeObserver.disconnect());
    return map;
  }

  function initPicker(root) {
    const form = root.closest("form");
    const latitude = form.elements.latitude;
    const longitude = form.elements.longitude;
    const status = root.querySelector("[data-location-status]");
    const readLocation = () => {
      if (!latitude.value.trim() || !longitude.value.trim()) return null;
      const lat = Number(latitude.value), lon = Number(longitude.value);
      return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? [lat, lon] : null;
    };
    let location = readLocation();
    const map = createMap(root, location || [45.9, 24.9], location ? 19 : 5);
    let marker;
    function setPin(latlng, updateFields) {
      const lat = Math.max(-90, Math.min(90, latlng.lat));
      const lon = ((latlng.lng + 180) % 360 + 360) % 360 - 180;
      if (!marker) {
        marker = L.marker([lat, lon], {icon: pinIcon(), draggable: true, title: "Locatia casei", autoPan: true}).addTo(map);
        marker.on("dragend", () => setPin(marker.getLatLng(), true));
      } else marker.setLatLng([lat, lon]);
      if (updateFields) {
        latitude.value = lat.toFixed(6);
        longitude.value = lon.toFixed(6);
        latitude.dispatchEvent(new Event("input", {bubbles: true}));
        longitude.dispatchEvent(new Event("input", {bubbles: true}));
        status.textContent = "Pin selectat. Salveaza formularul pentru a pastra locatia.";
      }
    }
    if (location) setPin(L.latLng(location), false);
    map.on("click", event => setPin(event.latlng, true));
    for (const field of [latitude, longitude]) field.addEventListener("change", () => {
      location = readLocation();
      if (location) {
        setPin(L.latLng(location), false);
        map.setView(location, 19);
      } else if (marker) {
        marker.remove();
        marker = null;
      }
    });
    root.querySelector("[data-pin-center]").addEventListener("click", () => setPin(map.getCenter(), true));
    const locate = root.querySelector("[data-locate]");
    locate.addEventListener("click", () => {
      if (!navigator.geolocation) {
        status.textContent = "Localizarea nu este disponibila. Alege punctul pe harta sau introdu coordonatele.";
        return;
      }
      locate.disabled = true;
      status.textContent = "Se cauta locatia dispozitivului...";
      navigator.geolocation.getCurrentPosition(position => {
        const point = L.latLng(position.coords.latitude, position.coords.longitude);
        setPin(point, true);
        map.setView(point, 19);
        status.textContent = `Locatie estimata la ±${Math.round(position.coords.accuracy)} m. Ajusteaza pinul pe casa si salveaza formularul.`;
        locate.disabled = false;
      }, () => {
        status.textContent = "Locatia nu a putut fi obtinuta. Alege punctul pe harta sau introdu coordonatele.";
        locate.disabled = false;
      }, {enableHighAccuracy: true, timeout: 10000, maximumAge: 0});
    });
    root.dataset.mapReady = "true";
  }

  function svgNode(tag, attributes, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function initSun(root) {
    let map, marker, overlay, data, timer, pending = false;
    const write = (key, value) => { root.querySelector(`[data-sun-${key}]`).textContent = value; };
    const formatTime = at => new Intl.DateTimeFormat("ro-RO", {
      timeZone: data.timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(new Date(at));
    const direction = angle => ["N", "NE", "E", "SE", "S", "SV", "V", "NV"][Math.round(angle / 45) % 8];

    function draw() {
      if (!overlay) return;
      overlay.replaceChildren();
      if (!data || data.status !== "ok") return;
      const center = map.latLngToContainerPoint(marker.getLatLng());
      const size = map.getSize();
      overlay.setAttribute("viewBox", `0 0 ${size.x} ${size.y}`);
      const radius = Math.min(size.x, size.y) * 0.37;
      const point = (azimuth, elevation = 0) => {
        const distance = radius * (1 - Math.max(0, Math.min(90, elevation)) / 90);
        const radians = azimuth * Math.PI / 180;
        return {x: center.x + distance * Math.sin(radians), y: center.y - distance * Math.cos(radians)};
      };
      const append = (tag, attrs, text) => overlay.appendChild(svgNode(tag, attrs, text));
      append("circle", {cx: center.x, cy: center.y, r: radius, class: "sun-horizon"});
      append("circle", {cx: center.x, cy: center.y, r: radius / 2, class: "sun-altitude-ring"});
      for (const [azimuth, label] of [[0, "N"], [90, "E"], [180, "S"], [270, "V"]]) {
        const p = point(azimuth, -1);
        const dx = (p.x - center.x) / radius * 17, dy = (p.y - center.y) / radius * 17;
        append("text", {x: p.x + dx, y: p.y + dy + 4, class: "sun-compass"}, label);
      }
      let path = "", started = false;
      const eventTimes = new Set([data.sunrise?.at, data.sunset?.at]);
      for (const sample of data.path) {
        if (!sample.is_daylight && !eventTimes.has(sample.at)) { started = false; continue; }
        const p = point(sample.azimuth, sample.elevation);
        path += `${started ? "L" : "M"}${p.x},${p.y} `;
        started = true;
      }
      append("path", {d: path, class: "sun-day-path"});
      for (const [key, label] of [["sunrise", "Rasarit"], ["sunset", "Apus"]]) {
        if (!data[key]) continue;
        const p = point(data[key].azimuth);
        append("line", {x1: center.x, y1: center.y, x2: p.x, y2: p.y, class: "sun-event-ray"});
        append("circle", {cx: p.x, cy: p.y, r: 4, class: "sun-event-dot"});
        // Event labels face inward so they remain readable at narrow viewport sizes.
        append("text", {x: p.x + (p.x < center.x ? 9 : -9), y: p.y - 12,
          "text-anchor": p.x < center.x ? "start" : "end", class: "sun-event-label"}, `${label} ${formatTime(data[key].at)}`);
      }
      if (data.current) {
        const current = data.current;
        const p = point(current.azimuth, current.elevation);
        const group = svgNode("g", {class: current.is_daylight ? "sun-current" : "sun-current is-night"});
        group.appendChild(svgNode("line", {x1: center.x, y1: center.y, x2: p.x, y2: p.y, class: "sun-current-ray"}));
        group.appendChild(svgNode("circle", {cx: p.x, cy: p.y, r: 18, class: "sun-glow"}));
        group.appendChild(svgNode("circle", {cx: p.x, cy: p.y, r: 8, class: "sun-disc"}));
        if (current.is_daylight) for (let i = 0; i < 8; i++) {
          const angle = i * Math.PI / 4;
          group.appendChild(svgNode("line", {x1: p.x + 12 * Math.sin(angle), y1: p.y + 12 * Math.cos(angle),
            x2: p.x + 15 * Math.sin(angle), y2: p.y + 15 * Math.cos(angle), class: "sun-disc-ray"}));
        }
        overlay.appendChild(group);
      }
    }

    function render() {
      if (data.status !== "ok") {
        if (map) { map.remove(); map = null; marker = null; overlay = null; }
        write("status", data.status === "invalid_location" ? "Locatie invalida" : "Locatie lipsa");
        write("condition", "Fixeaza casa pe harta din configurarea statiei.");
        for (const key of ["clock", "azimuth", "elevation", "sunrise", "sunset"]) write(key, "—");
        root.querySelector("[data-map-canvas]").textContent = "Alege locatia casei pentru a vedea traseul soarelui.";
        return;
      }
      const location = [data.location.latitude, data.location.longitude];
      if (!map) {
        root.querySelector("[data-map-canvas]").textContent = "";
        map = createMap(root, location, 19);
        marker = L.marker(location, {icon: pinIcon(), title: "Casa ta", interactive: false, keyboard: false}).addTo(map);
        overlay = svgNode("svg", {class: "sun-overlay", "aria-hidden": "true"});
        map.getContainer().appendChild(overlay);
        map.on("move zoom resize", draw);
      } else if (!marker.getLatLng().equals(L.latLng(location))) {
        marker.setLatLng(location);
        map.setView(location, map.getZoom());
      }
      write("date", new Intl.DateTimeFormat("ro-RO", {timeZone: data.timezone, day: "numeric", month: "long", year: "numeric"}).format(new Date(data.calculated_at)) + " · " + data.timezone);
      write("clock", formatTime(data.calculated_at));
      write("status", "Live · 30 s");
      write("condition", data.current.is_daylight ? "Soarele este deasupra orizontului" : "Soarele este sub orizont");
      write("azimuth", `${data.current.azimuth.toFixed(1)}° · ${direction(data.current.azimuth)}`);
      write("elevation", `${data.current.elevation.toFixed(1)}°`);
      for (const event of ["sunrise", "sunset"]) write(event, data[event] ? formatTime(data[event].at) : "Nu are loc astazi");
      root.dataset.sunReady = "true";
      draw();
    }

    async function refresh() {
      clearTimeout(timer);
      if (pending || document.hidden) return;
      pending = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 12000);
      try {
        const response = await fetch(root.dataset.url, {headers: {Accept: "application/json"}, cache: "no-store", signal: controller.signal});
        if (!response.ok) throw new Error("Sun position unavailable");
        data = await response.json();
        render();
      } catch (_) {
        if (data) data = {...data, current: null};
        draw();
        write("status", "Actualizare indisponibila");
        write("condition", "Nu putem actualiza pozitia. Reincercam automat.");
        for (const key of ["azimuth", "elevation"]) write(key, "—");
      } finally {
        clearTimeout(timeout);
        pending = false;
        timer = setTimeout(refresh, 30000);
      }
    }
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) clearTimeout(timer);
      else refresh();
    });
    refresh();
  }

  function initialize(root, init) {
    if (!window.L) {
      const error = root.querySelector("[data-map-error]");
      error.textContent = "Harta nu a putut fi incarcata. Reincarca pagina sau introdu coordonatele manual in configurare.";
      error.hidden = false;
      return;
    }
    // Hidden forms and off-screen maps must not request background tiles.
    const observer = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return;
      observer.disconnect();
      init(root);
    });
    observer.observe(root);
  }
  document.querySelectorAll("[data-location-picker]").forEach(root => initialize(root, initPicker));
  document.querySelectorAll("[data-sun-map]").forEach(root => initialize(root, initSun));
})();
