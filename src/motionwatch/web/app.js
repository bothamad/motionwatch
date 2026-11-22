/* MotionWatch web interface - no build step, no dependencies. */
"use strict";

const SECRET = "********";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const DAY_ALIASES = { weekdays: DAYS.slice(0, 5), weekends: DAYS.slice(5), daily: DAYS, all: DAYS };
const DEFAULTS = { threshold: 25, min_area: 1500, min_motion_frames: 3, cooldown_seconds: 60, frame_interval: 0.25 };
const TUNING = [
  ["threshold", "Sensitivity threshold", "Brightness change (0-255) that counts as movement. Higher = less sensitive.", 1],
  ["min_area", "Minimum size", "Smallest moving area in pixels. Raise to ignore pets, leaves and rain.", 100],
  ["min_motion_frames", "Frames in a row", "How many checks in a row must see movement before an alert.", 1],
  ["cooldown_seconds", "Cooldown (seconds)", "Minimum time between two alerts from this camera.", 5],
  ["frame_interval", "Check every (seconds)", "Lower reacts faster, higher uses less CPU.", 0.05],
];

const S = { session: null, config: null, saved: "", status: null, view: "dashboard", editCam: null,
  timezones: null, probe: {}, pollTimer: null };

/* ---------- helpers ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled") el[k] = v;
    else if (k === "style") el.style.cssText = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (sel) => document.querySelector(sel);
/* replaceChildren that accepts nested arrays and skips null/false, like h() */
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));

async function api(method, path, body, opts = {}) {
  const init = { method, headers: { "X-MotionWatch": "1" }, credentials: "same-origin" };
  if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch(path, init);
  if (r.status === 401 && !path.startsWith("/api/login") && S.session && S.session.authenticated) {
    S.session.authenticated = false; render();
  }
  if (opts.raw) {
    if (!r.ok) { let m; try { m = (await r.json()).error; } catch (e) { /* ignore */ } throw new Error(m || r.statusText); }
    return r;
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

let toastTimer;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg; t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ""; }, bad ? 6000 : 2800);
}

async function busy(btn, fn) {
  const old = btn.textContent; btn.disabled = true; btn.textContent = "Working…";
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = old; }
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return s + " s ago";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return new Date(ts * 1000).toLocaleString();
}
const randomId = () => "cam-" + Array.from(crypto.getRandomValues(new Uint8Array(4)), (b) => b.toString(16).padStart(2, "0")).join("");
const toMin = (t) => { const [a, b] = String(t || "0:0").split(":").map(Number); return a * 60 + b; };

/* ---------- config normalisation & dirty tracking ---------- */
function normalise(cfg) {
  cfg.telegram = cfg.telegram || {};
  cfg.telegram.chat_ids = cfg.telegram.chat_ids || [];
  cfg.defaults = cfg.defaults || {};
  cfg.schedules = cfg.schedules || {};
  cfg.cameras = cfg.cameras || [];
  for (const wins of Object.values(cfg.schedules)) {
    for (const w of wins) {
      const days = new Set();
      for (const d of w.days || ["daily"]) {
        const k = String(d).toLowerCase();
        (DAY_ALIASES[k] || [k.slice(0, 3)]).forEach((x) => days.add(x));
      }
      w.days = DAYS.filter((d) => days.has(d));
      if (w.end === "24:00") w.end = "00:00";
      if (w.start === "24:00") w.start = "00:00";
    }
  }
  for (const c of cfg.cameras) { if (!c.id) c.id = randomId(); if (c.enabled === undefined) c.enabled = true; }
  return cfg;
}
function setConfig(cfg) { S.config = normalise(cfg); S.saved = JSON.stringify(S.config); updateDirty(); }
const isDirty = () => S.config && JSON.stringify(S.config) !== S.saved;
function updateDirty() {
  const bar = $("#savebar");
  if (bar) bar.style.display = isDirty() ? "" : "none";
}
window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });

async function save(btn) {
  await busy(btn, async () => {
    const res = await api("PUT", "/api/config", S.config);
    setConfig(res.config);
    toast("Saved - changes are live");
    setTimeout(refreshStatus, 1500);
    if (S.view === "dashboard") renderView();
  });
}

/* input binding: writes into obj[key] without re-rendering, so focus is kept */
function bindText(obj, key, extra = {}) {
  return { value: obj[key] == null ? "" : obj[key], oninput: (e) => { obj[key] = e.target.value; updateDirty(); }, ...extra };
}
function bindNum(obj, key, extra = {}) {
  return { type: "number", value: obj[key] == null ? "" : obj[key], oninput: (e) => {
    const v = e.target.value.trim();
    if (v === "") delete obj[key]; else obj[key] = Number(v);
    updateDirty();
  }, ...extra };
}
function bindCheck(obj, key, onchange) {
  return { type: "checkbox", checked: !!obj[key], onchange: (e) => { obj[key] = e.target.checked; updateDirty(); if (onchange) onchange(); } };
}
const field = (label, input, help) => h("label", { class: "field" }, h("span", {}, label), input, help && h("small", {}, help));

/* ---------- top level ---------- */
async function boot() {
  try { S.session = await api("GET", "/api/session"); } catch (e) { S.session = { authenticated: false, setup_required: false }; }
  render();
}

async function render() {
  clearInterval(S.pollTimer);
  const app = $("#app");
  fill(app, );
  if (S.session.setup_required) return app.append(renderSetup());
  if (!S.session.authenticated) return app.append(renderLogin());
  app.append(h("div", { class: "boot" }, "Loading…"));
  try {
    const [cfg, status] = await Promise.all([api("GET", "/api/config"), api("GET", "/api/status")]);
    setConfig(cfg); S.status = status;
  } catch (e) { fill(app, h("div", { class: "boot" }, e.message)); return; }
  fill(app, renderShell());
  renderView();
  S.pollTimer = setInterval(() => { if (S.view === "dashboard" && !document.hidden) refreshStatus(); }, 3000);
}

function renderSetup() {
  const err = h("div", { class: "err" });
  const code = h("input", { type: "text", autocomplete: "off", placeholder: "XXXX-XXXX-XXXX", autofocus: true });
  const pw = h("input", { type: "password", autocomplete: "new-password" });
  const pw2 = h("input", { type: "password", autocomplete: "new-password" });
  const btn = h("button", { class: "primary", type: "submit" }, "Create password");
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    if (pw.value !== pw2.value) { err.textContent = "Passwords don't match"; return; }
    btn.disabled = true;
    try { await api("POST", "/api/setup", { code: code.value, password: pw.value });
      S.session = { authenticated: true }; S.view = "dashboard"; render();
    } catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
  } },
    field("Setup code", code, h("span", {}, "On the server run ", h("code", {}, "sudo cat /var/lib/motionwatch/setup-code"),
      " (Docker: ", h("code", {}, "docker logs motionwatch"), ")")),
    field("New admin password", pw, "At least 8 characters."),
    field("Repeat password", pw2), err, btn);
  return h("div", { class: "auth" }, h("div", { class: "card" },
    h("div", { class: "brand" }, h("img", { src: "/static/icon.svg", alt: "" }), "MotionWatch"),
    h("p", { class: "muted", style: "text-align:center" }, "Welcome! Create the admin password to get started."), form));
}

function renderLogin() {
  const err = h("div", { class: "err" });
  const pw = h("input", { type: "password", autocomplete: "current-password", autofocus: true });
  const btn = h("button", { class: "primary", type: "submit" }, "Log in");
  return h("div", { class: "auth" }, h("div", { class: "card" },
    h("div", { class: "brand" }, h("img", { src: "/static/icon.svg", alt: "" }), "MotionWatch"),
    h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault(); err.textContent = ""; btn.disabled = true;
      try { await api("POST", "/api/login", { password: pw.value }); S.session.authenticated = true; render(); }
      catch (ex) { err.textContent = ex.message; pw.select(); } finally { btn.disabled = false; }
    } }, field("Password", pw), err, btn)));
}

const VIEWS = [["dashboard", "Dashboard", "◉"], ["cameras", "Cameras", "▣"], ["schedules", "Schedules", "◷"],
  ["telegram", "Telegram", "✈"], ["settings", "Settings", "⚙"]];

function go(view) { S.view = view; if (view !== "cameras") S.editCam = null; renderNav(); renderView(); window.scrollTo(0, 0); }

function renderShell() {
  return h("div", { class: "shell" },
    h("nav", { class: "side", id: "nav" }),
    h("main", { class: "main" }, h("div", { id: "view" }),
      h("div", { class: "savebar", id: "savebar", style: isDirty() ? "" : "display:none" },
        h("span", {}, h("b", {}, "Unsaved changes"), h("span", { class: "muted" }, " - nothing changes until you save.")),
        h("div", { class: "row" },
          h("button", { onclick: () => { setConfig(JSON.parse(S.saved)); renderView(); } }, "Discard"),
          h("button", { class: "primary", onclick: (e) => save(e.currentTarget) }, "Save & apply")))));
}

function renderNav() {
  const nav = $("#nav"); if (!nav) return;
  fill(nav, 
    h("div", { class: "brand" }, h("img", { src: "/static/icon.svg", alt: "" }), h("span", {}, "MotionWatch")),
    VIEWS.map(([id, label, icon]) => h("button", { class: "nav" + (S.view === id ? " active" : ""), onclick: () => go(id),
      "aria-current": S.view === id ? "page" : null }, h("span", { "aria-hidden": "true" }, icon), label)),
    h("div", { class: "spacer" }),
    h("button", { class: "nav", onclick: async () => {
      if (isDirty() && !confirm("You have unsaved changes. Log out anyway?")) return;
      await api("POST", "/api/logout").catch(() => {}); S.session.authenticated = false; S.saved = JSON.stringify(S.config); render();
    } }, "⎋ Log out"),
    h("div", { class: "muted small ver", style: "padding:8px 12px" }, "v" + (S.status ? S.status.version : "")));
}

function renderView(withNav = true) {
  if (withNav) renderNav();
  const v = $("#view"); if (!v) return;
  const fn = { dashboard: viewDashboard, cameras: viewCameras, schedules: viewSchedules, telegram: viewTelegram, settings: viewSettings }[S.view];
  fill(v, fn());
  updateDirty();
}

async function refreshStatus() {
  try { S.status = await api("GET", "/api/status"); } catch (e) { return; }
  if (S.view === "dashboard") renderView(false);
}

/* ---------- dashboard ---------- */
function statePill(cam) {
  const map = { online: ["ok", "Watching"], connecting: ["warn", "Connecting…"], starting: ["idle", "Starting"],
    offline: ["bad", "Offline"], disarmed: ["idle", "Off schedule"], disabled: ["idle", "Disabled"], stopped: ["idle", "Stopped"] };
  const [cls, label] = map[cam.state] || ["idle", cam.state];
  return h("span", { class: "pill " + cls }, label);
}

function viewDashboard() {
  const st = S.status || { cameras: [] };
  const hasSchedules = Object.keys(S.config.schedules).length > 0;
  const camsUsingSchedule = S.config.cameras.some((c) => c.schedule && c.schedule !== "always");
  const out = [h("div", { class: "head" }, h("div", {}, h("h1", {}, "Dashboard"),
    h("div", { class: "muted" }, `${st.now || ""} · server time`)))];

  if (st.error) out.push(h("div", { class: "banner bad" }, h("span", {}, h("b", {}, "Configuration problem: "), st.error)));
  if (!st.telegram_ready) out.push(h("div", { class: "banner warn" },
    h("span", {}, h("b", {}, "Telegram isn't set up yet"), " - movement is detected but no alerts are sent."),
    h("button", { onclick: () => go("telegram") }, "Set up Telegram")));

  if (!S.config.cameras.length || !st.telegram_ready) {
    out.push(h("div", { class: "card" }, h("h2", {}, "Getting started"), h("ol", { class: "steps" },
      h("li", { class: st.telegram_ready ? "done" : "" }, h("span", {}, h("b", {}, "Connect Telegram"), " so alerts reach your phone. ",
        h("button", { class: "link", onclick: () => go("telegram") }, "Telegram →"))),
      h("li", { class: S.config.cameras.length ? "done" : "" }, h("span", {}, h("b", {}, "Add your cameras"), " using their RTSP or HTTP stream. ",
        h("button", { class: "link", onclick: () => go("cameras") }, "Cameras →"))),
      h("li", { class: camsUsingSchedule ? "done" : "" }, h("span", {}, h("b", {}, "Choose when to watch"), hasSchedules ? " - pick a schedule for each camera, or leave it on Always. " : ". ",
        h("button", { class: "link", onclick: () => go("schedules") }, "Schedules →"))))));
  }

  if (st.cameras.length) {
    out.push(h("div", { class: "grid" }, st.cameras.map((cam) => {
      const thumb = h("div", { class: "thumb" });
      if (cam.last_frame_at) thumb.append(h("img", { alt: `Live view of ${cam.name}`, src: `/api/cameras/${cam.id}/frame.jpg?t=${cam.last_frame_at}` }));
      else thumb.append(cam.state === "disarmed" ? "Camera is off until its schedule starts" : cam.state === "disabled" ? "Disabled" : cam.error || "Waiting for picture…");
      return h("div", { class: "card cam" }, thumb,
        h("div", { class: "between", style: "margin-bottom:8px" }, h("h3", { style: "margin:0" }, cam.name), statePill(cam)),
        cam.error && cam.state === "offline" ? h("p", { class: "small", style: "color:var(--bad)" }, cam.error) : null,
        h("dl", { class: "kv" },
          h("dt", {}, "Schedule"), h("dd", {}, cam.schedule === "always" ? "Always" : cam.schedule, cam.enabled ? (cam.armed ? " · armed now" : " · not armed now") : ""),
          h("dt", {}, "Last motion"), h("dd", {}, ago(cam.last_motion)),
          h("dt", {}, "Alerts sent"), h("dd", {}, cam.alerts || 0)),
        h("div", { class: "row", style: "margin-top:12px" },
          h("button", { onclick: () => { S.view = "cameras"; S.editCam = cam.id; renderView(); } }, "Edit"),
          cam.has_alert_image ? h("a", { class: "btn", href: `/api/cameras/${cam.id}/alert.jpg`, target: "_blank", rel: "noopener" }, "Last alert picture") : null));
    })));
  }
  return h("div", {}, out);
}

/* ---------- cameras ---------- */
function scheduleOptions(selected) {
  return [h("option", { value: "always", selected: selected === "always" || !selected }, "Always (24/7)"),
    ...Object.keys(S.config.schedules).map((n) => h("option", { value: n, selected: selected === n }, n))];
}

function viewCameras() {
  if (S.editCam) {
    const cam = S.config.cameras.find((c) => c.id === S.editCam);
    if (cam) return cameraEditor(cam);
    S.editCam = null;
  }
  const cams = S.config.cameras;
  return h("div", {},
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Cameras"), h("div", { class: "muted" }, "Each camera is watched separately, with its own schedule and sensitivity.")),
      h("button", { class: "primary", onclick: () => {
        const cam = { id: randomId(), name: `Camera ${cams.length + 1}`, url: "", username: "", password: "", schedule: "always", enabled: true };
        cams.push(cam); S.editCam = cam.id; renderView();
      } }, "+ Add camera")),
    h("div", { class: "card" }, cams.length ? cams.map((c) => h("div", { class: "list-item" },
      h("div", {}, h("div", {}, h("b", {}, c.name || "(no name)"), c.enabled ? "" : h("span", { class: "pill idle", style: "margin-left:8px" }, "Disabled")),
        h("div", { class: "muted small mono" }, c.url || "no URL yet")),
      h("div", { class: "row" }, h("span", { class: "pill" }, c.schedule === "always" || !c.schedule ? "Always" : c.schedule),
        h("button", { onclick: () => { S.editCam = c.id; renderView(); } }, "Edit"))))
      : h("p", { class: "muted", style: "margin:0" }, "No cameras yet. Press ", h("b", {}, "Add camera"), " to connect your first one.")));
}

function cameraEditor(cam) {
  const d = { ...DEFAULTS, ...S.config.defaults };
  const pw = h("input", { type: "password", autocomplete: "new-password", ...bindText(cam, "password"),
    placeholder: cam.password === SECRET ? "" : "optional" });
  const zoneBox = h("div", {});
  const showZones = (src) => { fill(zoneBox, zoneEditor(cam, src)); };
  const probeBtn = h("button", { class: "primary", onclick: (e) => busy(e.currentTarget, async () => {
    const r = await api("POST", "/api/cameras/probe", { id: cam.id, url: cam.url, username: cam.username, password: cam.password }, { raw: true });
    const blob = await r.blob();
    if (S.probe[cam.id]) URL.revokeObjectURL(S.probe[cam.id]);
    S.probe[cam.id] = URL.createObjectURL(blob);
    showZones(S.probe[cam.id]);
    toast("Connected - got a picture");
  }) }, "Test connection");

  const live = (S.status.cameras || []).find((c) => c.id === cam.id && c.last_frame_at);
  if (S.probe[cam.id]) showZones(S.probe[cam.id]);
  else if (live) showZones(`/api/cameras/${cam.id}/frame.jpg?t=${live.last_frame_at}`);
  else zoneBox.append(h("p", { class: "muted", style: "margin:0" }, "Press ", h("b", {}, "Test connection"), " to check the camera and see a picture."));

  return h("div", {},
    h("div", { class: "head" }, h("div", {}, h("button", { class: "link", onclick: () => { S.editCam = null; renderView(); } }, "← All cameras"),
      h("h1", {}, cam.name || "New camera")),
    h("button", { class: "danger", onclick: () => {
      if (!confirm(`Remove camera "${cam.name}"? (Takes effect when you save.)`)) return;
      S.config.cameras.splice(S.config.cameras.indexOf(cam), 1); S.editCam = null; renderView();
    } }, "Remove camera")),
    h("div", { class: "card stack" }, h("h2", {}, "Connection"),
      h("div", { class: "fields" },
        field("Name", h("input", { type: "text", ...bindText(cam, "name"), maxlength: 60 })),
        field("Schedule", h("select", { onchange: (e) => { cam.schedule = e.target.value; updateDirty(); } }, scheduleOptions(cam.schedule)),
          h("span", {}, "When this camera is armed. ", h("button", { class: "link", type: "button", onclick: () => go("schedules") }, "Edit schedules")))),
      field("Stream URL", h("input", { type: "text", class: "mono", ...bindText(cam, "url"), placeholder: "rtsp://192.168.1.10:554/stream2", spellcheck: "false" }),
        h("span", {}, "Examples - Hikvision: ", h("code", {}, "rtsp://IP:554/Streaming/Channels/102"), " · Dahua/Amcrest: ",
          h("code", {}, "rtsp://IP:554/cam/realmonitor?channel=1&subtype=1"), " · Reolink: ", h("code", {}, "rtsp://IP:554/h264Preview_01_sub"),
          " · USB webcam: ", h("code", {}, "0"), ". Use the low-resolution sub-stream - it works just as well and uses far less CPU.")),
      h("div", { class: "fields" },
        field("Username", h("input", { type: "text", autocomplete: "off", ...bindText(cam, "username"), placeholder: "optional" })),
        field("Password", pw, cam.password === SECRET ? "Saved. Leave as is to keep it." : null)),
      h("label", { class: "check" }, h("input", bindCheck(cam, "enabled")), "Camera enabled")),
    h("div", { class: "card stack" }, h("div", { class: "between" }, h("h2", { style: "margin:0" }, "Picture & ignore zones"), probeBtn),
      h("p", { class: "muted small", style: "margin:0" }, "Drag on the picture to mark areas to ignore - a road, a tree, a neighbour's window. Movement there won't trigger alerts."),
      zoneBox),
    h("div", { class: "card" }, h("details", {}, h("summary", {}, "Sensitivity (advanced)"),
      h("p", { class: "muted small" }, "Leave blank to use the defaults from Settings."),
      h("div", { class: "fields" }, TUNING.map(([key, label, help, step]) =>
        field(label, h("input", { ...bindNum(cam, key), step, min: 0, placeholder: String(d[key]) }), help))))));
}

function zoneEditor(cam, src) {
  cam.ignore_regions = cam.ignore_regions || [];
  const img = h("img", { src, alt: `Picture from ${cam.name}`, draggable: "false" });
  const box = h("div", { class: "zone-editor" }, img);
  const layer = () => {
    box.querySelectorAll(".zone").forEach((z) => z.remove());
    cam.ignore_regions.forEach((r, i) => {
      box.append(h("div", { class: "zone", style: `left:${r[0] * 100}%;top:${r[1] * 100}%;width:${r[2] * 100}%;height:${r[3] * 100}%` },
        h("button", { title: "Remove zone", "aria-label": "Remove zone", onpointerdown: (e) => e.stopPropagation(),
          onclick: () => { cam.ignore_regions.splice(i, 1); layer(); updateDirty(); } }, "×")));
    });
  };
  let start = null, draft = null;
  const pos = (e) => { const r = img.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]; };
  box.addEventListener("pointerdown", (e) => { start = pos(e); box.setPointerCapture(e.pointerId);
    draft = h("div", { class: "zone draft" }); box.append(draft); });
  box.addEventListener("pointermove", (e) => { if (!start) return; const p = pos(e);
    draft.style.cssText = `left:${Math.min(start[0], p[0]) * 100}%;top:${Math.min(start[1], p[1]) * 100}%;width:${Math.abs(p[0] - start[0]) * 100}%;height:${Math.abs(p[1] - start[1]) * 100}%`; });
  box.addEventListener("pointerup", (e) => { if (!start) return; const p = pos(e);
    const r = [Math.min(start[0], p[0]), Math.min(start[1], p[1]), Math.abs(p[0] - start[0]), Math.abs(p[1] - start[1])].map((v) => Math.round(v * 1000) / 1000);
    start = null; draft.remove();
    if (r[2] > 0.02 && r[3] > 0.02) { cam.ignore_regions.push(r); updateDirty(); }
    layer(); });
  img.addEventListener("load", layer);
  img.addEventListener("error", () => box.replaceWith(h("p", { class: "muted" }, "No picture available yet - press Test connection.")));
  return h("div", {}, box, h("div", { class: "row", style: "margin-top:8px" },
    h("span", { class: "muted small" }, `${cam.ignore_regions.length || "No"} ignore zone${cam.ignore_regions.length === 1 ? "" : "s"}`),
    cam.ignore_regions.length ? h("button", { class: "link", onclick: () => { cam.ignore_regions = []; layer(); updateDirty(); renderView(); } }, "Clear all") : null));
}

/* ---------- schedules ---------- */
function segments(wins) {
  const segs = DAYS.map(() => []);
  for (const w of wins) {
    const s = toMin(w.start), e = toMin(w.end);
    w.days.forEach((d) => {
      const i = DAYS.indexOf(d);
      if (s === e) segs[i].push([0, 1440]);
      else if (s < e) segs[i].push([s, e]);
      else { segs[i].push([s, 1440]); segs[(i + 1) % 7].push([0, e]); }
    });
  }
  return segs;
}

function weekPreview(wins) {
  const segs = segments(wins);
  return h("div", { class: "week", "aria-label": "Weekly preview" },
    DAYS.map((d, i) => [h("span", { class: "muted" }, DAY_LABEL[d]),
      h("div", { class: "bar" }, segs[i].map(([a, b]) => h("i", { style: `left:${a / 14.4}%;width:${(b - a) / 14.4}%` })))]),
    h("span", {}), h("div", { class: "hours" }, ["00", "06", "12", "18", "24"].map((x) => h("span", {}, x))));
}

function windowRow(name, wins, w, rerender) {
  const note = h("span", { class: "muted small" });
  const setNote = () => {
    const s = toMin(w.start), e = toMin(w.end);
    note.textContent = s === e ? "all day" : e < s ? "→ ends next morning" : "";
  };
  setNote();
  const time = (key) => h("input", { type: "time", value: w[key], required: true,
    onchange: (e) => { w[key] = e.target.value || "00:00"; setNote(); updateDirty(); rerender(); } });
  return h("div", { class: "window" },
    h("div", { class: "days" }, DAYS.map((d) => h("button", { type: "button", class: "day" + (w.days.includes(d) ? " on" : ""),
      "aria-pressed": w.days.includes(d) ? "true" : "false",
      onclick: () => { w.days = w.days.includes(d) ? w.days.filter((x) => x !== d) : DAYS.filter((x) => x === d || w.days.includes(x));
        updateDirty(); rerender(); } }, DAY_LABEL[d]))),
    h("span", { class: "row" }, "from", time("start"), "to", time("end"), note),
    h("button", { class: "link danger", onclick: () => { wins.splice(wins.indexOf(w), 1); updateDirty(); rerender(); } }, "Remove"));
}

function scheduleCard(name) {
  const card = h("div", { class: "card" });
  const draw = () => {
    const wins = S.config.schedules[name];
    if (!wins) { card.remove(); return; }
    const users = S.config.cameras.filter((c) => c.schedule === name).map((c) => c.name);
    const empty = wins.some((w) => !w.days.length);
    fill(card, 
      h("div", { class: "between" }, h("h2", { style: "margin:0" }, name),
        h("button", { class: "danger", onclick: () => {
          if (users.length) { toast(`Used by ${users.join(", ")} - change those cameras first`, true); return; }
          if (!confirm(`Delete schedule "${name}"?`)) return;
          delete S.config.schedules[name]; updateDirty(); renderView();
        } }, "Delete")),
      h("p", { class: "muted small" }, users.length ? "Used by: " + users.join(", ") : "Not used by any camera yet."),
      wins.map((w) => windowRow(name, wins, w, draw)),
      empty ? h("p", { class: "err" }, "Pick at least one day for every time window.") : null,
      h("button", { onclick: () => { wins.push({ days: DAYS.slice(0, 5), start: "22:00", end: "06:00" }); updateDirty(); draw(); } }, "+ Add time window"),
      weekPreview(wins));
  };
  draw();
  return card;
}

function viewSchedules() {
  return h("div", {},
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Schedules"),
      h("div", { class: "muted" }, "A schedule says when cameras are armed. Outside it, a camera is disconnected and sends nothing. Cameras set to ", h("b", {}, "Always"), " are armed 24/7.")),
      h("button", { class: "primary", onclick: () => {
        const name = (prompt("Name for the new schedule (e.g. night, away, office closed):") || "").trim();
        if (!name) return;
        if (!/^[A-Za-z0-9 _.-]{1,40}$/.test(name) || name === "always") { toast("Use letters, numbers, spaces, - and _ only", true); return; }
        if (S.config.schedules[name]) { toast("A schedule with that name already exists", true); return; }
        S.config.schedules[name] = [{ days: DAYS.slice(0, 5), start: "18:00", end: "07:00" }];
        updateDirty(); renderView();
      } }, "+ New schedule")),
    Object.keys(S.config.schedules).length ? Object.keys(S.config.schedules).map(scheduleCard)
      : h("div", { class: "card muted" }, "No schedules yet - all cameras are watched 24/7."),
    h("p", { class: "muted small" }, "Tip: a window whose end is earlier than its start runs past midnight - Mon 18:00 to 07:00 covers Monday evening and Tuesday morning. Same start and end (e.g. 00:00 to 00:00) means the whole day."));
}

/* ---------- telegram ---------- */
function viewTelegram() {
  const tg = S.config.telegram;
  const chatsBox = h("div", {});
  const found = h("div", {});
  const drawChats = () => fill(chatsBox, h("div", { class: "row" },
    tg.chat_ids.length ? tg.chat_ids.map((id) => h("span", { class: "chip mono" }, id,
      h("button", { "aria-label": `Remove ${id}`, onclick: () => { tg.chat_ids = tg.chat_ids.filter((x) => x !== id); updateDirty(); drawChats(); } }, "×")))
      : h("span", { class: "muted small" }, "No chats yet.")));
  drawChats();
  const addChat = (id) => { id = String(id).trim(); if (id && !tg.chat_ids.includes(id)) { tg.chat_ids.push(id); updateDirty(); drawChats(); } };
  const manual = h("input", { type: "text", class: "mono", placeholder: "e.g. 123456789 or -1001234567890", style: "max-width:280px" });

  return h("div", {},
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Telegram"), h("div", { class: "muted" }, "Where motion alerts are sent."))),
    h("div", { class: "card stack" },
      h("h2", {}, "1. Create a bot"),
      h("p", { class: "muted", style: "margin:0" }, "In Telegram, open ", h("a", { href: "https://t.me/BotFather", target: "_blank", rel: "noopener" }, "@BotFather"),
        ", send ", h("code", {}, "/newbot"), " and follow the steps. Paste the token it gives you here."),
      field("Bot token", h("input", { type: "password", class: "mono", autocomplete: "off", ...bindText(tg, "bot_token"), placeholder: "123456789:AA…" }),
        tg.bot_token === SECRET ? "Saved. Paste a new token only if you want to change it." : null)),
    h("div", { class: "card stack" },
      h("h2", {}, "2. Choose who gets alerts"),
      h("p", { class: "muted", style: "margin:0" }, "Send any message to your new bot (or add it to a group and say hello), then press ", h("b", {}, "Find chats"), "."),
      h("div", { class: "row" }, h("button", { onclick: (e) => busy(e.currentTarget, async () => {
        const r = await api("POST", "/api/telegram/chats", { bot_token: tg.bot_token });
        fill(found, r.chats.length
          ? h("div", { class: "card", style: "margin:0;background:var(--bg)" }, h("div", { class: "small muted" }, `Chats that messaged @${r.bot}:`),
            r.chats.map((c) => h("div", { class: "list-item" }, h("span", {}, h("b", {}, c.title), " ", h("span", { class: "muted small" }, `${c.type} · ${c.id}`)),
              tg.chat_ids.includes(c.id) ? h("span", { class: "pill ok" }, "Added") : h("button", { onclick: () => { addChat(c.id); fill(found, ); } }, "Add"))))
          : h("p", { class: "err" }, `No messages found. Open @${r.bot} in Telegram, press Start or send "hi", then try again.`));
      }) }, "Find chats")),
      found,
      h("div", {}, h("div", { class: "small", style: "font-weight:600;margin-bottom:6px" }, "Alerts go to"), chatsBox),
      h("details", {}, h("summary", {}, "Add a chat id by hand"),
        h("div", { class: "row" }, manual, h("button", { onclick: () => { addChat(manual.value); manual.value = ""; } }, "Add")))),
    h("div", { class: "card stack" },
      h("h2", {}, "3. Test it"),
      h("label", { class: "check" }, h("input", bindCheck(tg, "send_startup_message")), "Send a message when MotionWatch starts"),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: (e) => busy(e.currentTarget, async () => {
        await api("POST", "/api/telegram/test", { bot_token: tg.bot_token, chat_ids: tg.chat_ids });
        toast("Test message sent - check Telegram");
      }) }, "Send test message"), h("span", { class: "muted small" }, "Uses the values above, even before you save."))));
}

/* ---------- settings ---------- */
function viewSettings() {
  const cfg = S.config;
  const tzSelect = h("select", { onchange: (e) => { cfg.timezone = e.target.value || null; updateDirty(); } },
    h("option", { value: "" }, "Server's timezone"), cfg.timezone ? h("option", { value: cfg.timezone, selected: true }, cfg.timezone) : null);
  const fillTz = (zones) => {
    fill(tzSelect, h("option", { value: "" }, "Server's timezone"),
      zones.map((z) => h("option", { value: z, selected: z === cfg.timezone }, z)));
  };
  if (S.timezones) fillTz(S.timezones);
  else api("GET", "/api/timezones").then((r) => { S.timezones = r.timezones; if (r.timezones.length) fillTz(r.timezones); }).catch(() => {});

  const snapDir = (S.status.data_dir || "/var/lib/motionwatch") + "/snapshots";
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password" });
  const nw2 = h("input", { type: "password", autocomplete: "new-password" });
  const d = cfg.defaults;

  return h("div", {},
    h("div", { class: "head" }, h("div", {}, h("h1", {}, "Settings"))),
    h("div", { class: "card stack" }, h("h2", {}, "General"),
      h("div", { class: "fields" },
        field("Timezone for schedules", tzSelect, `Server time is now ${S.status.now}.`),
        field("Log detail", h("select", { onchange: (e) => { cfg.log_level = e.target.value; updateDirty(); } },
          ["INFO", "DEBUG", "WARNING"].map((l) => h("option", { value: l, selected: (cfg.log_level || "INFO") === l }, l))))),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!cfg.snapshot_dir,
        onchange: (e) => { cfg.snapshot_dir = e.target.checked ? snapDir : null; updateDirty(); } }),
        "Also keep a copy of every alert picture on the server"),
      h("small", { class: "muted" }, "Saved in ", h("code", {}, cfg.snapshot_dir || snapDir), ". Remember to clean it up now and then.")),
    h("div", { class: "card" }, h("h2", {}, "Default sensitivity"),
      h("p", { class: "muted small" }, "Used by every camera unless the camera sets its own value."),
      h("div", { class: "fields" }, TUNING.map(([key, label, help, step]) =>
        field(label, h("input", { ...bindNum(d, key), step, min: 0, placeholder: String(DEFAULTS[key]) }), help)))),
    h("div", { class: "card stack" }, h("h2", {}, "Admin password"),
      h("div", { class: "fields" }, field("Current password", cur), field("New password", nw, "At least 8 characters."), field("Repeat new password", nw2)),
      h("div", {}, h("button", { onclick: (e) => busy(e.currentTarget, async () => {
        if (nw.value !== nw2.value) throw new Error("New passwords don't match");
        await api("POST", "/api/password", { current: cur.value, new: nw.value });
        cur.value = nw.value = nw2.value = ""; toast("Password changed - other sessions were logged out");
      }) }, "Change password"))),
    h("p", { class: "muted small" }, `MotionWatch ${S.status.version} · `,
      h("a", { href: "https://github.com/bothamad/motionwatch", target: "_blank", rel: "noopener" }, "GitHub"), " · ",
      h("a", { href: "https://github.com/bothamad/motionwatch/releases", target: "_blank", rel: "noopener" }, "Check for updates")));
}

boot();
