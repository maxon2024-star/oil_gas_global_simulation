// frontend_3d/globe_renderer.js

let worldGlobe = null;
let pyBridge = null;
let latestState = null;
let selectedEntityId = null;

const layerVis = {
  pipes: true,
  routes: true,
  storms: true,
};

function createFallbackEarthTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 1024;
  canvas.height = 512;
  const ctx = canvas.getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, 512);
  grad.addColorStop(0, "#0b192c");
  grad.addColorStop(0.5, "#06283d");
  grad.addColorStop(1, "#0b192c");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 1024, 512);

  ctx.strokeStyle = "rgba(56, 189, 248, 0.14)";
  ctx.lineWidth = 1;
  for (let x = 0; x < 1024; x += 32) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, 512); ctx.stroke();
  }
  for (let y = 0; y < 512; y += 32) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(1024, y); ctx.stroke();
  }
  return canvas.toDataURL();
}

function init3DGlobe() {
  const container = document.getElementById("globeViz");
  if (typeof Globe === "undefined") {
    container.innerHTML = "<div style='padding:40px;color:#38bdf8;'>Загрузка WebGL 3D-ядра...</div>";
    return;
  }

  worldGlobe = Globe()(container)
    .globeImageUrl("https://unpkg.com/three-globe/example/img/earth-blue-marble.jpg")
    .bumpImageUrl("https://unpkg.com/three-globe/example/img/earth-topology.png")
    .backgroundImageUrl("https://unpkg.com/three-globe/example/img/night-sky.png")
    .showAtmosphere(true)
    .atmosphereColor("#38bdf8")
    .atmosphereAltitude(0.18)
    .pointsData([])
    .pointLat("lat")
    .pointLng("lon")
    .pointAltitude((d) => {
      if (d.type === "CHOKEPOINT") return 0.025;
      if (d.type === "WAYPOINT") return 0.008;
      return Math.max(0.02, (d.fill_pct / 100.0) * 0.09);
    })
    .pointRadius((d) => {
      if (d.type === "WAYPOINT") return 0.22;
      if (d.type === "CHOKEPOINT") return d.is_blocked ? 0.75 : 0.42;
      if (d.type === "STS_ZONE") return 0.48;
      return 0.58;
    })
    .pointColor((d) => {
      if (d.is_blocked || d.well_status === "SHUT_IN_TANK_TOP") return "#ef4444";
      if (d.type === "EXTRACTION_FIELD") return "#f59e0b";
      if (d.type === "EXPORT_TERMINAL") return "#38bdf8";
      if (d.type === "IMPORT_TERMINAL") return "#22c55e";
      if (d.type === "STS_ZONE") return "#e879f9";
      if (d.type === "CHOKEPOINT") return "#fbbf24";
      return "#64748b";
    })
    .pointLabel((d) => `
      <div style="background:rgba(2,6,23,0.92);border:1px solid #38bdf8;padding:8px 10px;border-radius:6px;font-size:12px;">
        <b style="color:#38bdf8">${d.name} (${d.country})</b><br/>
        Тип: ${d.type} | Погода: ${(d.weather * 100).toFixed(0)}%<br/>
        Запасы в парке: <b>${d.total_cur_mbbl} / ${d.total_cap_mbbl} млн барр. (${d.fill_pct}%)</b><br/>
        Причалы: ${d.occupied_berths}/${d.berth_count} | Очередь на рейде: <b>${d.queued_vessels}</b><br/>
        Демередж: <span style="color:#f87171">$${d.demurrage_kusd}k</span>
      </div>
    `)
    .onPointClick((d) => {
      selectedEntityId = d.id;
      showInspectorForNode(d);
      if (pyBridge) pyBridge.notifyEntitySelected(d.id);
    })
    .arcsData([])
    .arcStartLat("start_lat")
    .arcStartLng("start_lon")
    .arcEndLat("end_lat")
    .arcEndLng("end_lon")
    .arcColor("color")
    .arcAltitude("alt")
    .arcStroke("stroke")
    .arcDashLength("dashLen")
    .arcDashGap("dashGap")
    .arcDashAnimateTime("animTime")
    .arcLabel("label")
    .polygonsData([])
    .polygonGeoJsonGeometry("geometry")
    .polygonCapColor((d) => d.capColor)
    .polygonSideColor(() => "rgba(239, 68, 68, 0.18)")
    .polygonStrokeColor((d) => d.strokeColor)
    .polygonAltitude(0.015)
    .polygonLabel((d) => `
      <div style="background:rgba(69,10,10,0.92);border:1px solid #ef4444;padding:8px;border-radius:6px;font-size:12px;">
        <b style="color:#fca5a5">⚡ ${d.name}</b><br/>
        Тип: ${d.type} | Интенсивность: ${(d.severity * 100).toFixed(0)}%<br/>
        Осталось: <b>${d.remaining_hr.toFixed(0)} ч</b> | Радиус: ${d.radius_nm} NM
      </div>
    `)
    .ringsData([])
    .ringLat("lat")
    .ringLng("lon")
    .ringColor(() => (t) => `rgba(239, 68, 68, ${1 - t})`)
    .ringMaxRadius("maxR")
    .ringPropagationSpeed("speed")
    .ringRepeatPeriod("repeat")
    .htmlElementsData([])
    .htmlLat("lat")
    .htmlLng("lon")
    .htmlAltitude(0.018)
    .htmlElement((v) => {
      const el = document.createElement("div");
      const color =
        v.alignment === "WESTERN_INSURED"
          ? "#38bdf8"
          : v.alignment === "SHADOW_FLEET"
          ? "#f97316"
          : "#10b981";
      const isIdle = v.state === "IDLE";
      el.style.cssText = `
        cursor: pointer;
        pointer-events: auto;
        display: flex;
        align-items: center;
        gap: 3px;
        padding: 2px 5px;
        border-radius: 4px;
        background: rgba(2, 6, 23, 0.86);
        border: 1px solid ${color};
        color: #f8fafc;
        font-size: 9.5px;
        font-weight: 600;
        box-shadow: 0 0 6px ${color}66;
        transform: translate(-50%, -50%);
      `;
      const icon = v.cargo === "LNG" ? "❄️" : isIdle ? "⚓" : "🚢";
      el.innerHTML = `<span>${icon}</span><span style="color:${color}">${v.id}</span>`;
      el.title = `${v.name} (${v.vessel_class}) | ${v.state} -> ${v.dest_name}`;
      el.onclick = (e) => {
        e.stopPropagation();
        selectedEntityId = v.id;
        showInspectorForVessel(v);
        if (pyBridge) pyBridge.notifyEntitySelected(v.id);
      };
      return el;
    });

  worldGlobe.pointOfView({ lat: 28.0, lng: 45.0, altitude: 1.95 }, 0);

  window.addEventListener("resize", () => {
    if (worldGlobe) {
      worldGlobe.width(window.innerWidth);
      worldGlobe.height(window.innerHeight);
    }
  });
}

function updateSceneFromTelemetry(state) {
  latestState = state;
  if (!worldGlobe) return;

  worldGlobe.pointsData(state.nodes || []);

  const arcs = [];
  if (layerVis.pipes && state.pipelines) {
    for (const p of state.pipelines) {
      const isDown = p.bottleneck === "SHUTDOWN" || p.integrity_pct <= 0;
      const isBackpressure = p.bottleneck === "TARGET_FULL_BACKPRESSURE";
      const color = isDown
        ? ["#ef4444", "#7f1d1d"]
        : isBackpressure
        ? ["#f59e0b", "#ef4444"]
        : ["#a855f7", "#38bdf8"];

      arcs.push({
        start_lat: p.start_lat,
        start_lon: p.start_lon,
        end_lat: p.end_lat,
        end_lon: p.end_lon,
        color: color,
        alt: 0.03,
        stroke: isDown ? 0.9 : 1.15,
        dashLen: isDown ? 1.0 : 0.45,
        dashGap: isDown ? 0.0 : 0.2,
        animTime: isDown ? 0 : Math.max(900, 2600 - (p.flow_bbl_hr / 100)),
        label: `<b>Трубопровод: ${p.name}</b><br/>Сорт: ${p.commodity} | Поток: ${p.flow_bbl_hr.toLocaleString()} барр/ч<br/>Статус: ${p.bottleneck} (${p.integrity_pct}%)`,
      });
    }
  }

  if (layerVis.routes && state.vessels) {
    for (const v of state.vessels) {
      if (v.route_coords && v.route_coords.length >= 2 && v.state !== "IDLE") {
        const dest = v.route_coords[v.route_coords.length - 1];
        arcs.push({
          start_lat: v.lat,
          start_lon: v.lon,
          end_lat: dest[0],
          end_lon: dest[1],
          color: v.alignment === "SHADOW_FLEET" ? ["rgba(249,115,22,0.55)", "rgba(249,115,22,0.15)"] : ["rgba(56,189,248,0.45)", "rgba(16,185,129,0.15)"],
          alt: 0.01,
          stroke: 0.35,
          dashLen: 0.25,
          dashGap: 0.25,
          animTime: 3200,
          label: `Курс ${v.name} -> ${v.dest_name}`,
        });
      }
    }
  }
  worldGlobe.arcsData(arcs);

  const polys = [];
  const rings = [];
  if (layerVis.storms && state.cataclysms) {
    for (const c of state.cataclysms) {
      if (c.polygon && c.polygon.length >= 3) {
        const geoRing = c.polygon.map((pt) => [pt[1], pt[0]]);
        polys.push({
          name: c.name,
          type: c.type,
          severity: c.severity,
          remaining_hr: c.remaining_hr,
          radius_nm: c.radius_nm,
          capColor: c.type === "ARCTIC_ICE_LOCK" ? "rgba(56, 189, 248, 0.22)" : "rgba(239, 68, 68, 0.26)",
          strokeColor: c.type === "ARCTIC_ICE_LOCK" ? "#38bdf8" : "#ef4444",
          geometry: {
            type: "Polygon",
            coordinates: [geoRing],
          },
        });
      }
      rings.push({
        lat: c.lat,
        lon: c.lon,
        maxR: Math.max(4, c.radius_nm / 95),
        speed: 2.5,
        repeat: 700,
      });
    }
  }
  worldGlobe.polygonsData(polys);
  worldGlobe.ringsData(rings);

  worldGlobe.htmlElementsData(state.vessels || []);

  if (selectedEntityId) {
    const vMatch = (state.vessels || []).find((x) => x.id === selectedEntityId);
    if (vMatch) showInspectorForVessel(vMatch);
    const nMatch = (state.nodes || []).find((x) => x.id === selectedEntityId);
    if (nMatch) showInspectorForNode(nMatch);
  }
}

function showInspectorForVessel(v) {
  const card = document.getElementById("inspector-card");
  card.classList.remove("hidden");
  document.getElementById("insp-title").innerText = `🚢 ${v.name} (${v.id})`;
  document.getElementById("insp-body").innerHTML = `
    <div><b>Класс / Флаг:</b> ${v.vessel_class} (${v.flag}) | ${v.alignment}</div>
    <div><b>Статус СМО:</b> <span style="color:#38bdf8">${v.state}</span></div>
    <div><b>Назначение:</b> ${v.dest_name}</div>
    <div><b>Скорость / Курс:</b> ${v.speed} узл. (${v.heading}°)</div>
    <div><b>Груз на борту:</b> <b>${v.cargo}</b> (${v.cargo_mbbl} млн барр. / ${v.fill_pct}%)</div>
    <div><b>Рейсов / Разворотов:</b> ${v.voyages} / <span style="color:#fbbf24">${v.reroutes}</span></div>
    <div><b>Демередж / P&L:</b> <span style="color:#f87171">$${v.demurrage_kusd}k</span> | <span style="color:#4ade80">$${v.net_profit_musd}M</span></div>
  `;
}

function showInspectorForNode(n) {
  const card = document.getElementById("inspector-card");
  card.classList.remove("hidden");
  document.getElementById("insp-title").innerText = `🏭 ${n.name} (${n.country})`;
  document.getElementById("insp-body").innerHTML = `
    <div><b>Тип узла:</b> ${n.type}</div>
    <div><b>Статус скважин:</b> <span style="color:${n.is_blocked ? '#ef4444' : '#4ade80'}">${n.well_status}</span></div>
    <div><b>Погодное окно:</b> ${(n.weather * 100).toFixed(0)}%</div>
    <div><b>Резервуарный парк:</b> <b>${n.total_cur_mbbl} / ${n.total_cap_mbbl} млн барр. (${n.fill_pct}%)</b></div>
    <div><b>Добыча / НПЗ:</b> +${n.prod_rate.toLocaleString()} / -${n.cons_rate.toLocaleString()} барр/ч</div>
    <div><b>Причалы СМО:</b> занято ${n.occupied_berths} из ${n.berth_count} (очередь: <b>${n.queued_vessels}</b>)</div>
    <div><b>Начисленный демередж:</b> <span style="color:#f87171">$${n.demurrage_kusd}k</span></div>
  `;
}

function closeInspector() {
  selectedEntityId = null;
  document.getElementById("inspector-card").classList.add("hidden");
}

function focusCameraOnCoords(lat, lon, alt = 1.15) {
  if (worldGlobe) {
    worldGlobe.pointOfView({ lat: lat, lng: lon, altitude: alt }, 900);
  }
}

function resetCamera() {
  focusCameraOnCoords(28.0, 45.0, 1.95);
}

function toggleLayer(layerKey) {
  layerVis[layerKey] = !layerVis[layerKey];
  const btn = document.getElementById(`btn-toggle-${layerKey}`);
  if (btn) btn.classList.toggle("active", layerVis[layerKey]);
  if (latestState) updateSceneFromTelemetry(latestState);
}

function connectQtWebChannelBridge() {
  if (typeof QWebChannel !== "undefined" && window.qt && window.qt.webChannelTransport) {
    new QWebChannel(window.qt.webChannelTransport, function (channel) {
      pyBridge = channel.objects.globeBridge;
      document.getElementById("ipc-status-text").innerText =
        "IPC АКТИВЕН: QtWebChannel + OS SharedMemory (Zero-Socket)";

      pyBridge.telemetryFrameReady.connect(function (jsonStr) {
        try {
          const state = JSON.parse(jsonStr);
          updateSceneFromTelemetry(state);
        } catch (err) {
          console.error("IPC parse error:", err);
        }
      });

      pyBridge.cameraFocusRequested.connect(function (lat, lon, entityId) {
        selectedEntityId = entityId;
        focusCameraOnCoords(lat, lon, 0.95);
      });
    });
  } else {
    document.getElementById("ipc-status-text").innerText = "Режим автономного 3D-просмотра";
  }
}

window.addEventListener("DOMContentLoaded", () => {
  init3DGlobe();
  connectQtWebChannelBridge();
});
