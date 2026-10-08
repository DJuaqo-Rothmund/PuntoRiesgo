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
    L.control.scale({ imperial: false, position: "bottomright" }).addTo(S.map);

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
        // Color del plano si la capa lo trae; si no, dorado.
        const c = (f && f.properties && f.properties.color) || "#e9b949";
        return { color: c, weight: 1.6, opacity: 0.95, fillColor: c, fillOpacity: 0.09 };
      },
      onEachFeature: function (f, layer) {
        layer.bindTooltip(esc(f.properties.sector || f.properties._name), {
          permanent: true, direction: "center", className: "sector-label",
        });
        layer.bindPopup(sectorPopup(f), { maxWidth: 280, minWidth: 240 });
        layer.on("popupopen", function () { layer.setStyle({ weight: 3.2, fillOpacity: 0.22 }); });
        layer.on("popupclose", function () { S.layers.sectors.resetStyle(layer); });
      },
    }).addTo(S.map);

    S.layers.other = L.geoJSON(null, {
      style: function () { return { color: "#7fd3e6", weight: 2, opacity: 0.8, dashArray: "4 5" }; },
    }).addTo(S.map);

    S.layers.equipment = L.geoJSON(null, {
      pointToLayer: function (f, latlng) {
        return L.circleMarker(latlng, {
          radius: 7, color: "#fff", weight: 2.5, fillColor: "#3b9eff", fillOpacity: 1,
        });
      },
      onEachFeature: function (f, layer) {
        layer.bindTooltip(esc(f.properties._name), { direction: "top", offset: [0, -8], className: "sector-label" });
        layer.bindPopup(featurePopup(f, "Equipo de riego", "equipo"), { maxWidth: 280, minWidth: 240 });
      },
    }).addTo(S.map);

    S.layers.alerts = L.layerGroup().addTo(S.map);

    addControls();
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

  function row(icon, label, value) {
    if (value === undefined || value === null || value === "") return "";
    return '<div class="r"><span class="i">' + PRIcons.svg(icon, 17) + '</span><span>' +
      '<span class="k">' + esc(label) + '</span><span class="v">' + esc(value) + "</span></span></div>";
  }

  function popHead(color, icon, title, sub) {
    return '<div class="head"><span class="badge-ico" style="background:' + color + '">' +
      PRIcons.svg(icon, 21) + '</span><div><h4>' + esc(title) + '</h4><div class="sub">' +
      esc(sub || "") + "</div></div></div>";
  }

  function sectorPopup(f) {
    const p = f.properties || {};
    const color = p.color || "#e9b949";
    return '<div class="pop"><div class="body">' +
      popHead(color, "sector", p.sector || p._name, p.equipo_riego || "Sector") +
      '<div class="rows">' +
      row("sector", "Superficie", p.hectareas ? String(p.hectareas).replace(".", ",") + " ha" : "") +
      row("nota", "Hileras", p.hileras) +
      row("equipo", "Equipo de riego", p.equipo_riego) +
      row("nota", "Variedades", p.variedades) +
      row("nota", "Etiquetas del plano", p.etiquetas_plano) +
      "</div></div></div>";
  }

  function featurePopup(f, title, icon) {
    const p = f.properties || {};
    const rows = Object.keys(p)
      .filter(function (k) { return k[0] !== "_" && p[k] !== "" && p[k] != null && k !== "layer"; })
      .slice(0, 6)
      .map(function (k) { return row("nota", k.replace(/_/g, " "), p[k]); })
      .join("");
    return '<div class="pop"><div class="body">' + popHead("#3b9eff", icon || "nota", p._name, title) +
      '<div class="rows">' + rows + "</div></div></div>";
  }

  // ------------------------------------------------------------------ //
  // Controles
  // ------------------------------------------------------------------ //
  let locateBtn = null;

  function button(parent, icon, title, onClick) {
    const b = L.DomUtil.create("div", "ctrl-btn", parent);
    b.title = title;
    b.innerHTML = PRIcons.svg(icon, 22);
    L.DomEvent.on(b, "click", function (e) { L.DomEvent.stop(e); onClick(b); });
    return b;
  }

  function addControls() {
    const Ctl = L.Control.extend({
      options: { position: "topright" },
      onAdd: function () {
        const wrap = L.DomUtil.create("div", "");
        L.DomEvent.disableClickPropagation(wrap);
        L.DomEvent.disableScrollPropagation(wrap);

        const zoom = L.DomUtil.create("div", "ctrl-stack glass", wrap);
        button(zoom, "plus", "Acercar", function () { S.map.zoomIn(); });
        button(zoom, "minus", "Alejar", function () { S.map.zoomOut(); });

        const tools = L.DomUtil.create("div", "ctrl-stack glass ctrl-gap", wrap);
        locateBtn = button(tools, "locate", "Seguir mi posición", function () {
          setFollow(!S.follow);
          if (S.follow && S.userMarker) {
            S.map.setView(S.userMarker.getLatLng(), Math.max(S.map.getZoom(), 17));
          }
        });
        locateBtn.classList.toggle("active", S.follow);
        button(tools, "fit", "Ver todo el predio", function () { setFollow(false); fitAll(); });
        const layersBtn = button(tools, "layers", "Capas", function () {
          panel.classList.toggle("open");
          layersBtn.classList.toggle("active", panel.classList.contains("open"));
        });

        const panel = L.DomUtil.create("div", "layers-panel glass", wrap);
        [["Sectores", "sectors"], ["Equipos de riego", "equipment"], ["Alertas", "alerts"]]
          .forEach(function (item) {
            const r = L.DomUtil.create("div", "layer-row on", panel);
            r.innerHTML = '<span class="check"></span>' + esc(item[0]);
            L.DomEvent.on(r, "click", function () {
              const layer = S.layers[item[1]];
              if (S.map.hasLayer(layer)) S.map.removeLayer(layer); else S.map.addLayer(layer);
              r.classList.toggle("on", S.map.hasLayer(layer));
            });
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
    if (b.isValid()) S.map.fitBounds(b, { padding: [24, 24] });
  }

  const legendChips = {};
  function addLegend() {
    const Legend = L.Control.extend({
      options: { position: "bottomleft" },
      onAdd: function () {
        const div = L.DomUtil.create("div", "legend glass");
        L.DomEvent.disableClickPropagation(div);
        S.cfg.catalog.severities.slice().reverse().forEach(function (s) {
          const chip = L.DomUtil.create("div", "chip", div);
          chip.title = "Mostrar/ocultar severidad " + s.label;
          chip.innerHTML = '<span class="dot" style="background:' + s.color + '"></span>' +
            esc(s.label) + ' <span class="count">0</span>';
          legendChips[s.id] = chip;
          L.DomEvent.on(chip, "click", function () {
            if (S.hiddenSeverities.has(s.id)) S.hiddenSeverities.delete(s.id);
            else S.hiddenSeverities.add(s.id);
            chip.classList.toggle("off", S.hiddenSeverities.has(s.id));
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
      icon: L.divIcon({ className: "", html: '<div class="pick-marker"></div>', iconSize: [26, 26], iconAnchor: [13, 13] }),
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
  const PIN_PATH = "M19 44.5C19 44.5 4 29.6 4 18.5a15 15 0 1 1 30 0C34 29.6 19 44.5 19 44.5Z";

  function pinIcon(p) {
    const pending = p.sync_state !== "synced";
    return L.divIcon({
      className: "",
      html: '<div class="pin ' + esc(p.severity) + '">' +
        '<svg class="shape" viewBox="0 0 38 46" width="38" height="46"><path d="' + PIN_PATH +
        '" fill="' + p.color + '" stroke="#fff" stroke-width="2.2"/></svg>' +
        '<span class="ico">' + PRIcons.svg(p.risk_type, 20) + "</span>" +
        (pending ? '<span class="sync"></span>' : "") + "</div>",
      iconSize: [38, 46],
      iconAnchor: [19, 45],
      popupAnchor: [0, -42],
    });
  }

  function fmtDate(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString("es-CL", { day: "numeric", month: "short" }) + " · " +
      d.toLocaleTimeString("es-CL", { hour: "2-digit", minute: "2-digit" });
  }

  function alertPopup(p) {
    const sev = S.sevById[p.severity] || { color: "#999", label: p.severity };
    const synced = p.sync_state === "synced";
    const sector = p.sector_name
      ? p.sector_name + (!p.sector_inside && p.sector_distance_m != null
        ? " (a " + Math.round(p.sector_distance_m) + " m)" : "")
      : "Fuera de sectores";
    const equipo = p.equipment_name
      ? p.equipment_name + (p.equipment_distance_m != null ? " · " + Math.round(p.equipment_distance_m) + " m" : "")
      : "";
    let html = '<div class="pop">';
    if (p.has_photo) {
      html += '<img class="photo" loading="lazy" src="photos/' + encodeURIComponent(p.id) + '.jpg">';
    }
    html += '<div class="body">' +
      popHead(sev.color, p.risk_type, p.risk_label, fmtDate(p.created_at)) +
      '<div class="tags">' +
      '<span class="tag"><span class="d" style="background:' + sev.color + '"></span>' + esc(sev.label) + "</span>" +
      '<span class="tag">' + PRIcons.svg(synced ? "synced" : "pending", 14) +
      (synced ? "Sincronizada" : "Por enviar") + "</span>" +
      "</div>" +
      '<div class="rows">' +
      row("sector", "Sector", sector) +
      row("equipo", "Equipo de riego", equipo) +
      row("gps", "Precisión GPS", p.accuracy_m ? "± " + Math.round(p.accuracy_m) + " m" : "") +
      "</div>" +
      (p.description ? '<div class="note">' + esc(p.description) + "</div>" : "") +
      "</div></div>";
    return html;
  }

  function renderAlerts() {
    if (!S.alertsData) return;
    S.layers.alerts.clearLayers();
    S.alertMarkers = {};
    const counts = {};
    S.alertsData.features.forEach(function (f) {
      counts[f.properties.severity] = (counts[f.properties.severity] || 0) + 1;
    });
    Object.keys(legendChips).forEach(function (id) {
      legendChips[id].querySelector(".count").textContent = counts[id] || 0;
    });
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
      }).bindPopup(alertPopup(p), { maxWidth: 280, minWidth: 260 });
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
        radius: pos.accuracy || 0, color: "#3b9eff", weight: 1, opacity: 0.6,
        fillColor: "#3b9eff", fillOpacity: 0.1, interactive: false,
      }).addTo(S.map);
      S.userMarker = L.marker(ll, {
        icon: L.divIcon({
          className: "", html: '<div class="me"><span class="halo"></span><span class="core"></span></div>',
          iconSize: [20, 20], iconAnchor: [10, 10],
        }),
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
    if (!sync || S.cfg.embedded) { el.classList.add("hidden"); return; }
    const offline = !sync.backend_reachable;
    const pending = sync.pending || 0;
    if (!offline && pending === 0) { el.classList.add("hidden"); return; }
    el.classList.remove("hidden");
    el.classList.toggle("offline", offline);
    el.classList.toggle("pending", !offline && pending > 0);
    el.innerHTML = '<span class="d"></span>' + (offline ? "Sin conexión" : "En línea") +
      (pending ? ' <span style="color:var(--muted)">· ' + pending + " por enviar</span>" : "");
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
