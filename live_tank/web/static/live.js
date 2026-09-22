(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const pad2 = (n) => String(n).padStart(2, "0");
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  let label = "LMB";
  let boot = null;
  let layersSynced = false;
  let history = [];

  // ---- where the video comes from -----------------------------------------
  // This page may be served by a host that cannot see the tank. /api/config
  // then points at the tracker's own URL and hands over a ticket to open it
  // with, so the video never makes a detour through the public site.
  let videoOrigin = "";
  let ticket = "";
  let planLoaded = false;
  const videoUrl = (path) =>
    videoOrigin + path + (ticket ? (path.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(ticket) : "");

  async function loadStreamPlan(path = "/api/config") {
    try {
      const res = await fetch(path, { cache: "no-store" });
      if (!res.ok) return;
      const plan = await res.json();
      if (plan.origin) videoOrigin = plan.origin;
      if (typeof plan.ticket === "string") ticket = plan.ticket;
    } catch { /* same-origin stream is the sensible default */ }
  }

  // Panel sizes and minimized state are remembered per browser.
  const prefs = (() => {
    try { return JSON.parse(localStorage.getItem("lmb-vision-panels") || "{}"); } catch { return {}; }
  })();
  const savePrefs = () => {
    try { localStorage.setItem("lmb-vision-panels", JSON.stringify(prefs)); } catch { /* ignore */ }
  };

  // ---- polling ------------------------------------------------------------
  async function poll() {
    try {
      const res = await fetch("/api/vision", { cache: "no-store" });
      if (res.ok) render(await res.json());
    } catch {
      showOffline("App server unreachable", "Is the LMB Vision process still running?");
    }
    setTimeout(poll, 500);
  }

  function render(s) {
    // The server restarted with a newer version: load it.
    if (boot && s.boot !== boot) { location.reload(); return; }
    boot = s.boot;

    label = s.label;
    const src = s.source, v = s.vision, c = s.counts;

    setBadge("b-tracked", s.tracks.length);
    setBadge("b-missing", c.missing);
    $("spark-now").textContent = c.onscreen;

    $("p-capture").textContent = src.width ? `${src.width}×${src.height} @ ${src.fps.toFixed(0)} fps` : "—";
    $("p-detect").textContent = `${c.detections} fish blob${c.detections === 1 ? "" : "s"}`;
    $("p-filter").textContent = `${c.edges} edge · ${c.reflections} reflection · ${c.dark} dark object`;
    $("p-track").textContent = `${c.candidates} detecting · ${c.tracks_created} tracks`;
    $("p-reid").textContent = `${c.reids} returns`;
    $("p-known").textContent = `${c.known} (${c.missing} out of view)`;
    $("p-fps").textContent = `${v.fps.toFixed(1)} fps`;
    $("p-latency").textContent = `${v.latency_ms} ms`;

    const streaming = ["LIVE", "PLAYBACK"].includes(src.status);
    $("learning").hidden = !(v.phase === "LEARNING" && streaming);
    $("learning-bar").style.width = `${Math.round(v.progress * 100)}%`;

    if (["RECONNECTING", "STALLED", "SOURCE ERROR"].includes(src.status)) {
      showOffline(src.status === "SOURCE ERROR" ? "Cannot open Camera 1" : "Reconnecting to Camera 1",
                  src.status === "STALLED" ? "No new frames from the camera" : "Retrying automatically");
    } else if (src.status === "CONNECTING") {
      showOffline("Connecting to Camera 1", "Opening the camera stream");
    } else {
      $("offline").hidden = true;
    }

    renderRoster(s.tracks);
    renderMissing(s.missing);
    history = s.history;
    if (!$("spark").matches(":hover")) renderSpark();

    if (!layersSynced) {
      document.querySelectorAll("[data-layer]").forEach((el) => { el.checked = !!s.layers[el.dataset.layer]; });
      layersSynced = true;
    }
  }

  function setBadge(id, n) {
    const el = $(id);
    el.hidden = !n;
    el.textContent = n;
  }

  function showOffline(title, text) {
    $("offline-title").textContent = title;
    $("offline-text").textContent = text;
    $("offline").hidden = false;
    $("learning").hidden = true;
  }

  function renderRoster(tracks) {
    $("roster-empty").hidden = tracks.length > 0;
    $("roster").replaceChildren(...tracks.map((t) => {
      const li = document.createElement("li");
      li.className = "fish" + (t.matched ? "" : " coasting");
      li.innerHTML = `
        <span class="tag">${label} ${pad2(t.id)}</span>
        <span class="fish-meta">${fmtDuration(t.age_s)} tracked · ${t.size[0]}×${t.size[1]} px</span>
        <span class="fish-speed">${t.speed} px/s</span>
        <span class="conf" title="Detection confidence ${Math.round(t.conf * 100)}%"><i style="width:${Math.round(t.conf * 100)}%"></i></span>`;
      return li;
    }));
  }

  function renderMissing(missing) {
    $("missing-empty").hidden = missing.length > 0;
    $("missing").replaceChildren(...missing.map((m) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="tag">${label} ${pad2(m.id)}</span>
        <span class="muted">seen ${fmtDuration(m.seen_s)}</span>
        <span class="gone">gone ${fmtDuration(m.gone_s)}</span>`;
      return li;
    }));
  }

  function fmtDuration(s) {
    if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${pad2(Math.floor(s % 60))}s` : `${Math.floor(m / 60)}h ${pad2(m % 60)}m`;
  }

  // ---- graph: fish on screen, one sample per second, last 2 minutes -------------
  // Drawn in real pixels so markers stay round at any panel size.
  const SLOTS = 120;
  function sparkSize() {
    const el = $("spark");
    return [Math.max(40, el.clientWidth), Math.max(20, el.clientHeight)];
  }

  function sparkGeometry() {
    const [W, H] = sparkSize();
    const max = Math.max(4, ...history);
    const offset = SLOTS - history.length;
    return history.map((v, i) => [((offset + i) / (SLOTS - 1)) * W, H - 2 - (v / max) * (H - 12)]);
  }

  function renderSpark(hoverIndex) {
    const [W, H] = sparkSize();
    const svg = $("spark-svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const pts = sparkGeometry();
    const max = Math.max(4, ...history);
    let html = `<line x1="0" y1="${H - 2}" x2="${W}" y2="${H - 2}" stroke="rgba(11,31,74,.25)" stroke-width="1"/>
      <text x="0" y="8" font-size="9" fill="#2c4278" font-family="monospace">${max}</text>`;
    if (pts.length > 1) {
      const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
      html += `<path d="${d}L${pts[pts.length - 1][0]},${H - 2}L${pts[0][0]},${H - 2}Z" fill="rgba(40,110,255,.16)"/>
        <path d="${d}" fill="none" stroke="#0b1f4a" stroke-width="2" stroke-linejoin="round"/>`;
      const last = pts[pts.length - 1];
      html += `<circle cx="${last[0]}" cy="${last[1]}" r="4" fill="#286eff" stroke="#e8f0ff" stroke-width="2"/>`;
    }
    if (hoverIndex != null && pts[hoverIndex]) {
      const [x, y] = pts[hoverIndex];
      html += `<line x1="${x}" y1="0" x2="${x}" y2="${H - 2}" stroke="#2c4278" stroke-width="1"/>
        <circle cx="${x}" cy="${y}" r="4" fill="#0b1f4a" stroke="#e8f0ff" stroke-width="2"/>`;
    }
    svg.innerHTML = html;
  }

  $("spark").addEventListener("pointermove", (e) => {
    if (!history.length) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const slot = Math.round(((e.clientX - rect.left) / rect.width) * (SLOTS - 1));
    const i = clamp(slot - (SLOTS - history.length), 0, history.length - 1);
    const secondsAgo = history.length - 1 - i;
    const tip = $("spark-tip");
    tip.hidden = false;
    tip.style.left = `${(sparkGeometry()[i][0] / rect.width) * 100}%`;
    tip.textContent = `${history[i]} ${label} · ${secondsAgo ? `${secondsAgo}s ago` : "now"}`;
    renderSpark(i);
  });
  $("spark").addEventListener("pointerleave", () => { $("spark-tip").hidden = true; renderSpark(); });

  // ---- graph + mask panels: minimize / restore and drag to resize ---------------
  const panels = {
    // The graph sits top-right, so its handle is bottom-left: drag left = wider, down = taller.
    history: { el: $("history-panel"), minW: 180, minH: 28, maxH: 360, dirX: -1, resizeHeight: true },
    // The mask sits bottom-left, so its handle is top-right: drag right = wider (height follows 16:9).
    mask: { el: $("mask-panel"), minW: 180, dirX: 1, resizeHeight: false },
  };

  function applyPanel(name) {
    const p = panels[name], st = prefs[name] || {};
    if (st.w) p.el.style.width = `${st.w}px`;
    if (p.resizeHeight && st.h) p.el.style.setProperty("--spark-h", `${st.h}px`);
    setCollapsed(name, !!st.collapsed, false);
  }

  function setCollapsed(name, collapsed, save = true) {
    const p = panels[name];
    p.el.classList.toggle("collapsed", collapsed);
    const btn = p.el.querySelector("[data-collapse]");
    btn.setAttribute("aria-expanded", String(!collapsed));
    btn.title = `${collapsed ? "Restore" : "Minimize"} ${name === "mask" ? "mask" : "graph"}`;
    if (name === "mask") syncMask();
    if (name === "history" && !collapsed) requestAnimationFrame(() => renderSpark());
    if (save) {
      prefs[name] = { ...(prefs[name] || {}), collapsed };
      savePrefs();
    }
  }

  Object.entries(panels).forEach(([name, p]) => {
    applyPanel(name);
    p.el.querySelector("[data-collapse]").addEventListener("click", () => {
      setCollapsed(name, !p.el.classList.contains("collapsed"));
    });

    const handle = p.el.querySelector("[data-resize]");
    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      const startX = e.clientX, startY = e.clientY;
      const startW = p.el.getBoundingClientRect().width;
      const startH = p.resizeHeight ? $("spark").clientHeight : 0;
      const maxW = window.innerWidth * 0.6;
      body.classList.add("resizing", "nesw");

      const move = (ev) => {
        const w = clamp(startW + (ev.clientX - startX) * p.dirX, p.minW, maxW);
        p.el.style.width = `${w}px`;
        const st = prefs[name] = { ...(prefs[name] || {}), w: Math.round(w) };
        if (p.resizeHeight) {
          const h = clamp(startH + (ev.clientY - startY), p.minH, p.maxH);
          p.el.style.setProperty("--spark-h", `${h}px`);
          st.h = Math.round(h);
          renderSpark();
        }
      };
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
        body.classList.remove("resizing", "nesw");
        savePrefs();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
  });

  // Only pull the mask's MJPEG stream while it is expanded, and never before
  // we know which server to pull it from.
  function syncMask() {
    if (!planLoaded) return;
    const img = $("mask");
    const visible = !panels.mask.el.classList.contains("collapsed");
    if (visible && !img.getAttribute("src")) img.src = videoUrl("/api/mask.mjpg");
    if (!visible && img.getAttribute("src")) img.removeAttribute("src");
  }

  // ---- bottom-right dock: eye slides the tools out and back ----------------------
  const dock = document.querySelector(".dock");
  const tools = document.querySelectorAll("#dock-tools [data-panel]");

  function setToolsOpen(open) {
    dock.classList.toggle("open", open);
    const eye = $("toggle-tools");
    eye.setAttribute("aria-expanded", String(open));
    eye.title = open ? "Hide tools" : "Show tools";
    tools.forEach((btn) => { btn.tabIndex = open ? 0 : -1; });
    if (!open) openPanel(null);
  }

  function openPanel(name) {
    tools.forEach((btn) => btn.setAttribute("aria-expanded", String(btn.dataset.panel === name)));
    document.querySelectorAll("[data-panel-body]").forEach((el) => {
      el.hidden = el.dataset.panelBody !== name;
    });
  }

  $("toggle-tools").addEventListener("click", () => setToolsOpen(!dock.classList.contains("open")));
  tools.forEach((btn) => {
    btn.addEventListener("click", () => {
      openPanel(btn.getAttribute("aria-expanded") === "true" ? null : btn.dataset.panel);
    });
  });

  // ---- controls ------------------------------------------------------------
  async function setLayer(name, on) {
    try {
      await fetch("/api/layers", { method: "POST", headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify({ [name]: on }) });
    } catch { /* next poll shows the truth */ }
  }

  document.querySelectorAll("[data-layer]").forEach((el) => {
    el.addEventListener("change", () => setLayer(el.dataset.layer, el.checked));
  });

  function toggleLayer(name) {
    const el = document.querySelector(`[data-layer="${name}"]`);
    el.checked = !el.checked;
    setLayer(name, el.checked);
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => {});
  }

  $("fullscreen").addEventListener("click", toggleFullscreen);
  $("stage").addEventListener("dblclick", toggleFullscreen);
  $("relearn").addEventListener("click", () => fetch("/api/relearn", { method: "POST" }));

  const keyLayers = { b: "boxes", l: "labels", t: "trails", v: "vectors", a: "tentative",
                      d: "detections", x: "rejected", m: "mask" };
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "f") toggleFullscreen();
    else if (k === "escape") setToolsOpen(false);
    else if (keyLayers[k]) toggleLayer(keyLayers[k]);
  });

  window.addEventListener("resize", () => renderSpark());

  // Keep the video alive if the MJPEG connection drops. A ticket expires, so
  // take a fresh one before reopening.
  const video = $("video");
  video.addEventListener("error", () => {
    setTimeout(async () => {
      await loadStreamPlan("/api/stream-ticket");
      video.src = videoUrl(`/api/video.mjpg?r=${Date.now()}`);
    }, 1500);
  });

  // Hide the cursor when idle so the stream stays clean.
  let idleTimer;
  document.addEventListener("pointermove", () => {
    body.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => body.classList.add("idle"), 2500);
  });

  (async () => {
    await loadStreamPlan();
    planLoaded = true;
    video.src = videoUrl("/api/video.mjpg");
    syncMask();
    poll();
  })();
})();
