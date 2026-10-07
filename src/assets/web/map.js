/*
 * PuntoRiesgo · Mapa Leaflet (corre dentro del WebView de Flet)
 *
 * Comunicación con Python vía el servidor local (rutas relativas al token):
 *   GET  api/config   configuración + catálogo de riesgos
 *   GET  api/layers   sectores y equipos de riego (GeoJSON)
 *   GET  api/alerts   alertas (GeoJSON)
 *   GET  api/state    polling: posición GPS, versiones, estado de sync, comandos
 *   POST api/events   eventos del mapa -> Python (map_pick, alert_click, ready)
 */
(function () {
  "use strict";

  const POLL_MS = 1000;
  const LONG_PRESS_MS = 650;
  const LABEL_MIN_ZOOM = 16;

  const S = {
    map: null,
    cfg: null,
    sevById: {},
    typeById: {},
    layers: {},
    userMarker: null,
    accuracyCircle: null,
    follow: true,
    lastAlertsVersion: -1,
    lastLayersVersion: -1,
    lastCmd: 0,
    alertsData: null,
    hiddenSeverities: new Set(),
    pickMarker: null,
    alertMarkers: {},
  };

  // ------------------------------------------------------------------ //
  // Utilidades
  // ------------------------------------------------------------------ //
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
    return r.json();
  }

  function postEvent(evt) {
    return fetch("api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(evt),
    }).catch(function (e) { console.error("postEvent", e); });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  let toastTimer = null;
  function toast(msg, ms) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.add("hidden"); }, ms || 2500);
  }

  // ------------------------------------------------------------------ //
  // Inicialización
  // ------------------------------------------------------------------ //
  async function init() {
    S.cfg = await getJSON("api/config");
    S.cfg.catalog.severities.forEach(function (s) { S.sevById[s.id] = s; });
    S.cfg.catalog.risk_types.forEach(function (t) { S.typeById[t.id] = t; });

    S.map = L.map("map", {
      zoomControl: false,
      zoomSnap: 0.5, // encuadre más ajustado del predio en pantallas angostas
      maxZoom: S.cfg.max_zoom,
      minZoom: 3,
      attributionControl: true,
      preferCanvas: false,
    });
    L.control.zoom({ position: "topright" }).addTo(S.map);
    L.control.scale({ imperial: false, position: "bottomleft" }).addTo(S.map);

    // Mapa base satelital servido por Python (caché offline -> red -> overzoom)
    L.tileLayer("tiles/{z}/{x}/{y}", {
      maxZoom: S.cfg.max_zoom,
      maxNativeZoom: S.cfg.max_native_zoom,
      attribution: S.cfg.attribution,
      keepBuffer: 4,
      updateWhenIdle: true,
    }).addTo(S.map);

    S.layers.sectors = L.geoJSON(null, {
      style: function (f) {
        // Color del plano si la capa lo trae; si no, amarillo.
        const c = (f && f.properties && f.properties.color) || "#ffeb3b";
        return { color: c, weight: 2, fillColor: c, fillOpacity: 0.12 };
      },
      onEachFeature: function (f, layer) {
        layer.bindTooltip(esc(f.properties._name), {
          permanent: true, direction: "center", className: "sector-label",
        });
        layer.bindPopup(featurePopup(f, "Sector"));
      },
    }).addTo(S.map);

    S.layers.other = L.geoJSON(null, {
      style: function () { return { color: "#80deea", weight: 2, dashArray: "4 4" }; },
    }).addTo(S.map);

    S.layers.equipment = L.geoJSON(null, {
      pointToLayer: function (f, latlng) {
        return L.circleMarker(latlng, {
          radius: 7, color: "#fff", weight: 2, fillColor: "#0288d1", fillOpacity: 1,
        });
      },
      onEachFeature: function (f, layer) {
        layer.bindTooltip("💧 " + esc(f.properties._name), { direction: "top", offset: [0, -6] });
        layer.bindPopup(featurePopup(f, "Equipo de riego"));
      },
    }).addTo(S.map);

    S.layers.alerts = L.layerGroup().addTo(S.map);

    L.control.layers(null, {
      "Sectores": S.layers.sectors,
      "Equipos de riego": S.layers.equipment,
      "Alertas": S.layers.alerts,
    }, { position: "topright", collapsed: true }).addTo(S.map);

    addLocateControl();
    addLegend();
    setupLongPress();

    const c = S.cfg.center || [-33.45, -70.66];
    S.map.setView(c, S.cfg.initial_zoom || 15);
    if (S.cfg.bounds) S.map.fitBounds(S.cfg.bounds, { padding: [20, 20] });

    // Con muchos sectores, las etiquetas sólo se muestran de cerca.
    const toggleLabels = function () {
      S.map.getContainer().classList.toggle("hide-labels", S.map.getZoom() < LABEL_MIN_ZOOM);
    };
    S.map.on("zoomend", toggleLabels);
    toggleLabels();

    // Si el usuario arrastra el mapa, dejamos de seguir el GPS.
    S.map.on("dragstart", function () { setFollow(false); });

    postEvent({ type: "ready" });
    poll();
  }

  function featurePopup(f, title) {
    const p = f.properties || {};
    const rows = Object.keys(p)
      .filter(function (k) { return k[0] !== "_" && p[k] !== "" && p[k] != null; })
      .slice(0, 8)
      .map(function (k) { return "<b>" + esc(k) + ":</b> " + esc(p[k]); })
      .join("<br>");
    return '<div class="popup"><h4>' + esc(title) + ": " + esc(p._name) + "</h4>" +
      '<div class="meta">' + rows + "</div></div>";
  }

  // ------------------------------------------------------------------ //
  // Controles
  // ------------------------------------------------------------------ //
  let locateBtn = null;
  function addLocateControl() {
    const Ctl = L.Control.extend({
      options: { position: "topright" },
      onAdd: function () {
        const wrap = L.DomUtil.create("div", "leaflet-bar");
        locateBtn = L.DomUtil.create("div", "ctrl-btn active", wrap);
        locateBtn.title = "Seguir mi posición";
        locateBtn.innerHTML = "◎";
        L.DomEvent.disableClickPropagation(wrap);
        L.DomEvent.on(locateBtn, "click", function () {
          setFollow(!S.follow);
          if (S.follow && S.userMarker) {
            S.map.setView(S.userMarker.getLatLng(), Math.max(S.map.getZoom(), 17));
          }
        });
        const fitBtn = L.DomUtil.create("div", "ctrl-btn", wrap);
        fitBtn.title = "Ver todo el predio";
        fitBtn.innerHTML = "⤢";
        L.DomEvent.on(fitBtn, "click", function () {
          setFollow(false);
          fitAll();
        });
        return wrap;
      },
    });
    new Ctl().addTo(S.map);
  }

  function setFollow(v) {
    S.follow = v;
    if (locateBtn) locateBtn.classList.toggle("active", v);
  }

  function fitAll() {
    const b = S.layers.sectors.getBounds();
    if (b.isValid()) S.map.fitBounds(b, { padding: [20, 20] });
  }

  function addLegend() {
    const Legend = L.Control.extend({
      options: { position: "bottomleft" },
      onAdd: function () {
        const div = L.DomUtil.create("div", "legend");
        L.DomEvent.disableClickPropagation(div);
        div.innerHTML = "<b>Severidad</b>";
        S.cfg.catalog.severities.slice().reverse().forEach(function (s) {
          const row = L.DomUtil.create("div", "row", div);
          row.innerHTML = '<span class="sw" style="background:' + s.color + '"></span>' + esc(s.label);
          L.DomEvent.on(row, "click", function () {
            if (S.hiddenSeverities.has(s.id)) S.hiddenSeverities.delete(s.id);
            else S.hiddenSeverities.add(s.id);
            row.classList.toggle("off", S.hiddenSeverities.has(s.id));
            renderAlerts();
          });
        });
        return div;
      },
    });
    new Legend().addTo(S.map);
  }

  // ------------------------------------------------------------------ //
  // Mantener presionado = marcar un punto manualmente
  // (el FAB de Flet usa el GPS; esto permite marcar un riesgo visto a distancia)
  // ------------------------------------------------------------------ //
  function setupLongPress() {
    let timer = null;
    let start = null;
    const el = S.map.getContainer();

    function cancel() { clearTimeout(timer); timer = null; }

    el.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1) return cancel();
      const t = e.touches[0];
      start = { x: t.clientX, y: t.clientY };
      cancel();
      timer = setTimeout(function () {
        const rect = el.getBoundingClientRect();
        const pt = L.point(start.x - rect.left, start.y - rect.top);
        pick(S.map.containerPointToLatLng(pt));
      }, LONG_PRESS_MS);
    }, { passive: true });
    el.addEventListener("touchmove", function (e) {
      if (!start || !timer) return;
      const t = e.touches[0];
      if (Math.abs(t.clientX - start.x) > 10 || Math.abs(t.clientY - start.y) > 10) cancel();
    }, { passive: true });
    el.addEventListener("touchend", cancel, { passive: true });
    el.addEventListener("touchcancel", cancel, { passive: true });

    // Escritorio: clic derecho
    S.map.on("contextmenu", function (e) {
      if (timer === null) pick(e.latlng);
    });
  }

  function pick(latlng) {
    if (navigator.vibrate) navigator.vibrate(40);
    if (S.pickMarker) S.map.removeLayer(S.pickMarker);
    S.pickMarker = L.marker(latlng, {
      icon: L.divIcon({ className: "", html: '<div class="pick-marker"></div>', iconSize: [22, 22], iconAnchor: [11, 11] }),
      interactive: false,
    }).addTo(S.map);
    setTimeout(function () {
      if (S.pickMarker) { S.map.removeLayer(S.pickMarker); S.pickMarker = null; }
    }, 6000);
    postEvent({ type: "map_pick", lat: latlng.lat, lon: latlng.lng });
  }

  // ------------------------------------------------------------------ //
  // Render de alertas
  // ------------------------------------------------------------------ //
  function pinIcon(p) {
    const sync = p.sync_state === "synced" ? "" : " pending";
    return L.divIcon({
      className: "",
      html: '<div class="risk-pin ' + esc(p.severity) + sync + '" style="--c:' + p.color + '"><span>' +
        esc(p.risk_glyph) + "</span></div>",
      iconSize: [34, 34],
      // El cuadrado rotado -45° deja su esquina aguda abajo al centro:
      // centro (17) + media diagonal (34·√2/2 ≈ 24) = 41 px.
      iconAnchor: [17, 41],
      popupAnchor: [0, -26],
    });
  }

  function alertPopup(p) {
    const sev = S.sevById[p.severity] || { color: "#999", label: p.severity };
    const date = new Date(p.created_at).toLocaleString("es-CL");
    let html = '<div class="popup"><h4>' + esc(p.risk_glyph) + " " + esc(p.risk_label) + "</h4>" +
      '<span class="badge" style="background:' + sev.color + '">' + esc(sev.label) + "</span> " +
      '<span class="badge" style="background:' + (p.sync_state === "synced" ? "#2e7d32" : "#607d8b") + '">' +
      (p.sync_state === "synced" ? "Sincronizada" : "Pendiente sync") + "</span>" +
      '<div class="meta">' +
      "<b>Sector:</b> " + esc(p.sector_name || "Fuera de sectores") +
      (p.sector_name && !p.sector_inside ? " (a " + Math.round(p.sector_distance_m) + " m)" : "") + "<br>" +
      "<b>Equipo cercano:</b> " + esc(p.equipment_name || "-") +
      (p.equipment_distance_m != null ? " (" + Math.round(p.equipment_distance_m) + " m)" : "") + "<br>" +
      "<b>Fecha:</b> " + esc(date) + "<br>" +
      (p.accuracy_m ? "<b>Precisión GPS:</b> ±" + Math.round(p.accuracy_m) + " m<br>" : "") +
      (p.description ? "<b>Obs.:</b> " + esc(p.description) : "") +
      "</div>";
    if (p.has_photo) html += '<img loading="lazy" src="photos/' + encodeURIComponent(p.id) + '.jpg">';
    return html + "</div>";
  }

  function renderAlerts() {
    if (!S.alertsData) return;
    S.layers.alerts.clearLayers();
    S.alertMarkers = {};
    // Las más graves al final para que queden encima.
    const feats = S.alertsData.features.slice().sort(function (a, b) {
      return (S.sevById[a.properties.severity] || {}).rank - (S.sevById[b.properties.severity] || {}).rank;
    });
    feats.forEach(function (f) {
      const p = f.properties;
      if (S.hiddenSeverities.has(p.severity)) return;
      const c = f.geometry.coordinates;
      const m = L.marker([c[1], c[0]], {
        icon: pinIcon(p),
        zIndexOffset: ((S.sevById[p.severity] || {}).rank || 0) * 100,
      }).bindPopup(alertPopup(p), { maxWidth: 260 });
      m.on("click", function () { postEvent({ type: "alert_click", id: p.id }); });
      m.addTo(S.layers.alerts);
      S.alertMarkers[p.id] = m;
    });
  }

  async function refreshAlerts() {
    S.alertsData = await getJSON("api/alerts");
    renderAlerts();
  }

  async function refreshLayers() {
    const gj = await getJSON("api/layers");
    ["sectors", "equipment", "other"].forEach(function (k) { S.layers[k].clearLayers(); });
    gj.features.forEach(function (f) {
      const kind = f.properties._kind;
      if (kind === "sector") S.layers.sectors.addData(f);
      else if (kind === "equipment") S.layers.equipment.addData(f);
      else S.layers.other.addData(f);
    });
  }

  // ------------------------------------------------------------------ //
  // Posición del usuario
  // ------------------------------------------------------------------ //
  function updatePosition(pos) {
    if (!pos) return;
    const ll = [pos.lat, pos.lon];
    if (!S.userMarker) {
      S.accuracyCircle = L.circle(ll, {
        radius: pos.accuracy || 0, color: "#1e88e5", weight: 1, fillOpacity: 0.12, interactive: false,
      }).addTo(S.map);
      S.userMarker = L.marker(ll, {
        icon: L.divIcon({ className: "", html: '<div class="user-dot"></div>', iconSize: [16, 16], iconAnchor: [8, 8] }),
        zIndexOffset: 10000,
        interactive: false,
      }).addTo(S.map);
      S.map.setView(ll, Math.max(S.map.getZoom(), 17));
    } else {
      S.userMarker.setLatLng(ll);
      S.accuracyCircle.setLatLng(ll).setRadius(pos.accuracy || 0);
      if (S.follow) S.map.panTo(ll, { animate: true });
    }
  }

  // ------------------------------------------------------------------ //
  // Comandos Python -> JS
  // ------------------------------------------------------------------ //
  function runCommand(cmd) {
    const a = cmd.args || {};
    switch (cmd.name) {
      case "center":
        setFollow(false);
        S.map.setView([a.lat, a.lon], a.zoom || Math.max(S.map.getZoom(), 17));
        break;
      case "follow":
        setFollow(true);
        if (S.userMarker) S.map.setView(S.userMarker.getLatLng(), Math.max(S.map.getZoom(), 17));
        break;
      case "fit":
        fitAll();
        break;
      case "open_alert":
        if (S.alertMarkers[a.id]) {
          setFollow(false);
          S.map.setView(S.alertMarkers[a.id].getLatLng(), 18);
          S.alertMarkers[a.id].openPopup();
        }
        break;
      case "toast":
        toast(a.text, a.ms);
        break;
      case "clear_pick":
        if (S.pickMarker) { S.map.removeLayer(S.pickMarker); S.pickMarker = null; }
        break;
      default:
        console.warn("Comando desconocido", cmd.name);
    }
  }

  function updateStatus(sync) {
    const el = document.getElementById("statusbar");
    if (!sync) return;
    const offline = !sync.backend_reachable;
    const pending = sync.pending || 0;
    if (!offline && pending === 0) { el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    el.classList.toggle("offline", offline);
    el.textContent = (offline ? "● Sin conexión" : "● En línea") +
      (pending ? " · " + pending + " pendiente(s) de sincronizar" : "");
  }

  // ------------------------------------------------------------------ //
  // Polling del estado
  // ------------------------------------------------------------------ //
  let lastVersion = -1;
  async function poll() {
    try {
      const st = await getJSON("api/state?cmd=" + S.lastCmd);
      if (st.version !== lastVersion) {
        lastVersion = st.version;
        if (st.layers_version !== S.lastLayersVersion) {
          S.lastLayersVersion = st.layers_version;
          await refreshLayers();
        }
        if (st.alerts_version !== S.lastAlertsVersion) {
          S.lastAlertsVersion = st.alerts_version;
          await refreshAlerts();
        }
        updatePosition(st.position);
        updateStatus(st.sync);
      }
      (st.commands || []).forEach(function (c) {
        S.lastCmd = Math.max(S.lastCmd, c.seq);
        runCommand(c);
      });
    } catch (e) {
      console.error("poll", e);
    } finally {
      setTimeout(poll, POLL_MS);
    }
  }

  // API mínima por si se prefiere WebView.run_javascript() en vez de polling.
  window.PuntoRiesgo = { runCommand: runCommand, refreshAlerts: refreshAlerts, toast: toast };

  init().catch(function (e) {
    console.error(e);
    toast("Error iniciando el mapa: " + e.message, 8000);
  });
})();
