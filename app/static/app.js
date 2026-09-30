"use strict";

/* =====================================================================
 * DOM helpers — all user-provided text goes through text nodes.
 * ===================================================================== */

const SVG_NS = "http://www.w3.org/2000/svg";
const DOM_PROPS = new Set(["value", "checked", "disabled", "selected", "readOnly", "open", "htmlFor"]);

function appendChildren(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (DOM_PROPS.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  appendChildren(el, children);
  return el;
}

function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value !== null && value !== undefined && value !== false) el.setAttribute(key, value);
  }
  appendChildren(el, children);
  return el;
}

const $ = (selector, root = document) => root.querySelector(selector);

/** Replace an element's children; null/false entries are skipped (unlike Element.replaceChildren). */
function fill(el, ...children) {
  el.replaceChildren();
  appendChildren(el, children);
}

function storageGet(key, fallback) {
  try {
    const value = sessionStorage.getItem(key);
    return value === null ? fallback : JSON.parse(value);
  } catch (e) {
    return fallback;
  }
}

function storageSet(key, value) {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch (e) {
    /* storage unavailable: settings just don't persist */
  }
}

/* =====================================================================
 * Formatting
 * ===================================================================== */

const nf0 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 });

function fmtNum(value) {
  if (value === null || value === undefined) return "–";
  const abs = Math.abs(value);
  if (abs >= 1e6) return nf1.format(value / 1e6) + " Mio.";
  if (abs >= 1e4) return nf0.format(value);
  if (abs >= 100) return nf1.format(value);
  return nf2.format(value);
}

function fmtMs(value) {
  if (value === null || value === undefined) return "–";
  return value >= 1000 ? nf2.format(value / 1000) + " s" : nf0.format(value) + " ms";
}

function fmtPct(value) {
  return value === null || value === undefined ? "–" : nf2.format(value) + " %";
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  seconds = Math.max(0, Math.round(seconds));
  const d = Math.floor(seconds / 86400);
  const hrs = Math.floor((seconds % 86400) / 3600);
  const min = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d} ${d === 1 ? "Tag" : "Tage"} ${hrs} h`;
  if (hrs > 0) return `${hrs} h ${min} min`;
  if (min > 0) return `${min} min`;
  return `${seconds} s`;
}

function fmtInterval(seconds) {
  if (seconds % 86400 === 0) return seconds === 86400 ? "1 Tag" : `${seconds / 86400} Tage`;
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

function fmtAgo(ts) {
  if (!ts) return "nie";
  const diff = Date.now() / 1000 - ts;
  if (diff < 5) return "gerade eben";
  if (diff < 60) return `vor ${Math.round(diff)} s`;
  if (diff < 3600) return `vor ${Math.round(diff / 60)} min`;
  if (diff < 86400) return `vor ${Math.round(diff / 3600)} h`;
  const days = Math.round(diff / 86400);
  return days === 1 ? "vor 1 Tag" : `vor ${days} Tagen`;
}

function fmtDateTime(ts) {
  return new Date(ts * 1000).toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function fmtTimeShort(ts, withDate) {
  const d = new Date(ts * 1000);
  return withDate
    ? d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }) + "."
    : d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

/* =====================================================================
 * Domain vocabulary
 * ===================================================================== */

const STATUS = {
  up: "OK",
  down: "Störung",
  pending: "Ausstehend",
  paused: "Pausiert",
};

const TYPES = {
  http: {
    label: "Website / API",
    badge: "HTTP",
    desc: "Ruft eine URL ab und prüft Statuscode, Inhalt und TLS-Zertifikat.",
    targetLabel: "URL",
    placeholder: "https://example.com/health",
    help: "Vollständige URL inkl. http:// oder https://",
  },
  tcp: {
    label: "TCP-Port",
    badge: "TCP",
    desc: "Prüft, ob ein Port erreichbar ist – Datenbank, SSH, Mailserver, …",
    targetLabel: "Host:Port",
    placeholder: "db.example.com:5432",
    help: "Hostname oder IP und Port, z. B. 192.168.1.10:22 oder [::1]:443",
  },
  ping: {
    label: "Ping",
    badge: "PING",
    desc: "ICMP-Erreichbarkeit eines Hosts – Router, Server, Netzwerkgeräte.",
    targetLabel: "Host / IP",
    placeholder: "192.168.1.1",
    help: "Hostname oder IP-Adresse",
  },
  dns: {
    label: "DNS",
    badge: "DNS",
    desc: "Prüft, ob ein Hostname aufgelöst wird – optional auf eine erwartete IP.",
    targetLabel: "Hostname",
    placeholder: "example.com",
    help: "Der Name, der aufgelöst werden soll",
  },
  push: {
    label: "Push / Agent / Heartbeat",
    badge: "PUSH",
    desc: "Das System meldet sich selbst per HTTP – Server-Agents, Cronjobs, Backups, IoT, Skripte.",
  },
};

const METRICS = {
  cpu: { label: "CPU-Auslastung", unit: "%", max: 100 },
  mem: { label: "Arbeitsspeicher", unit: "%", max: 100 },
  swap: { label: "Swap", unit: "%", max: 100 },
  disk: { label: "Festplatte /", unit: "%", max: 100 },
  load1: { label: "Load (1 min)" },
  load5: { label: "Load (5 min)" },
  procs: { label: "Prozesse" },
  cores: { label: "CPU-Kerne", static: true },
  uptime: { label: "Uptime", static: true, format: fmtDuration },
  cert_days: { label: "Zertifikat gültig", unit: "Tage", static: true },
};

function metricInfo(key) {
  if (METRICS[key]) return METRICS[key];
  if (key.startsWith("disk_")) return { label: "Festplatte " + key.slice(5), unit: "%", max: 100 };
  return { label: key };
}

function fmtMetric(key, value) {
  const info = metricInfo(key);
  if (value === null || value === undefined) return "–";
  if (info.format) return info.format(value);
  return fmtNum(value) + (info.unit ? (info.unit === "%" ? " %" : " " + info.unit) : "");
}

function statusEl(status) {
  return h("span", { class: `status status-${status}` }, h("span", { class: "dot", "aria-hidden": "true" }), STATUS[status] || status);
}

function baseUrl() {
  return (state.auth && state.auth.public_url) || location.origin;
}

function pushUrl(monitor) {
  return baseUrl() + monitor.push_path;
}

/* =====================================================================
 * API
 * ===================================================================== */

class AuthLost extends Error {}

function errorText(data, status) {
  if (data && typeof data.detail === "string") return data.detail;
  if (data && Array.isArray(data.detail)) {
    return data.detail
      .map((d) => {
        const field = (d.loc || []).filter((p) => p !== "body").join(".");
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return `Fehler ${status}`;
}

async function api(method, path, body) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    state.auth.user = null;
    route();
    throw new AuthLost("Sitzung abgelaufen");
  }
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}

function toast(message, isError) {
  const el = h("div", { class: "toast" + (isError ? " error" : ""), role: isError ? "alert" : "status" }, message);
  const host = $("#toasts");
  host.append(el);
  while (host.children.length > 3) host.firstElementChild.remove();
  setTimeout(() => el.remove(), isError ? 7000 : 3000);
}

function reportError(error) {
  if (!(error instanceof AuthLost)) toast(error.message || String(error), true);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // Fallback for plain-http deployments where the Clipboard API is unavailable.
    const area = h("textarea", { style: "position:fixed;opacity:0" }, text);
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast("In die Zwischenablage kopiert");
}

function copyButton(getText, label = "Kopieren") {
  return h("button", { type: "button", class: "btn btn-sm copy", onclick: () => copyText(getText()) }, label);
}

function codeBlock(text) {
  return h("div", { class: "code-block" }, h("pre", null, h("code", null, text)), copyButton(() => text));
}

function inlineCode(text) {
  return h("div", { class: "inline-code" }, h("code", null, text), copyButton(() => text));
}

/* =====================================================================
 * App state & routing
 * ===================================================================== */

const state = { auth: null };
let routeSeq = 0;
let cleanups = [];

function onCleanup(fn) {
  cleanups.push(fn);
}

function every(ms, fn) {
  const seq = routeSeq;
  const id = setInterval(() => {
    if (seq !== routeSeq || document.hidden) return;
    fn();
  }, ms);
  onCleanup(() => clearInterval(id));
}

function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  routeSeq += 1;
  cleanups.forEach((fn) => fn());
  cleanups = [];

  if (!state.auth.user) {
    if (state.auth.setup_required) renderSetup();
    else renderLogin();
    return;
  }

  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  let view;
  let section = "dashboard";
  if (parts.length === 0) view = viewDashboard;
  else if (parts[0] === "monitor" && parts[1] === "new") view = (main) => viewMonitorForm(main, null);
  else if (parts[0] === "monitor" && /^\d+$/.test(parts[1] || "") && parts[2] === "edit") view = (main) => viewMonitorForm(main, Number(parts[1]));
  else if (parts[0] === "monitor" && /^\d+$/.test(parts[1] || "")) view = (main) => viewMonitorDetail(main, Number(parts[1]));
  else if (parts[0] === "channels") { view = viewChannels; section = "channels"; }
  else if (parts[0] === "settings") { view = viewSettings; section = "settings"; }
  else {
    location.hash = "#/";
    return;
  }

  const main = renderShell(section);
  window.scrollTo(0, 0);
  try {
    await view(main);
  } catch (error) {
    if (error instanceof AuthLost) return;
    fill(main, 
      h("div", { class: "card card-pad" }, h("h2", null, "Fehler"), h("p", { class: "muted" }, error.message || String(error)),
        h("a", { class: "btn", href: "#/" }, "Zur Übersicht")),
    );
  }
}

function brandMark() {
  return h("span", { class: "brand-mark", "aria-hidden": "true" },
    s("svg", { width: 16, height: 16, viewBox: "0 0 32 32" },
      s("path", { d: "M3 17h7l3-8 5 15 3-7h8", fill: "none", stroke: "#fff", "stroke-width": 3.2, "stroke-linecap": "round", "stroke-linejoin": "round" })));
}

function themeButton() {
  const order = ["auto", "dark", "light"];
  const labels = { auto: "Design: automatisch", dark: "Design: dunkel", light: "Design: hell" };
  const icons = { auto: "◐", dark: "☾", light: "☀" };
  let current = document.documentElement.dataset.theme || "auto";
  const button = h("button", { type: "button", class: "btn btn-sm btn-ghost btn-icon", title: labels[current], "aria-label": labels[current] }, icons[current]);
  button.addEventListener("click", () => {
    current = order[(order.indexOf(current) + 1) % order.length];
    if (current === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = current;
    try {
      if (current === "auto") localStorage.removeItem("theme");
      else localStorage.setItem("theme", current);
    } catch (e) { /* ignore */ }
    button.textContent = icons[current];
    button.title = labels[current];
    button.setAttribute("aria-label", labels[current]);
  });
  return button;
}

function renderShell(section) {
  const link = (href, label, key) => h("a", { href, class: section === key ? "active" : null, "aria-current": section === key ? "page" : null }, label);
  const main = h("main", { id: "main" });
  fill($("#app"), 
    h("header", { class: "topbar" },
      h("div", { class: "topbar-inner" },
        h("a", { class: "brand", href: "#/", "aria-label": "Monitoring – Übersicht" }, brandMark(), h("span", { class: "brand-text" }, "Monitoring")),
        h("nav", { class: "nav", "aria-label": "Hauptnavigation" },
          link("#/", "Übersicht", "dashboard"),
          link("#/channels", "Benachrichtigungen", "channels"),
          link("#/settings", "Anbindung & Einstellungen", "settings")),
        h("div", { class: "topbar-right" },
          themeButton(),
          h("button", { type: "button", class: "btn btn-sm btn-ghost", onclick: logout }, "Abmelden")))),
    main,
  );
  return main;
}

async function logout() {
  try {
    await api("POST", "/api/auth/logout");
  } finally {
    state.auth.user = null;
    route();
  }
}

/* =====================================================================
 * Login / first-run setup
 * ===================================================================== */

function authScreen(title, subtitle, fields, submitLabel, onSubmit) {
  const error = h("div", { class: "notice warn", hidden: true, role: "alert" });
  const button = h("button", { type: "submit", class: "btn btn-primary", style: "width:100%" }, submitLabel);
  const form = h("form", { class: "card auth-card fields" },
    h("div", null, h("div", { class: "brand" }, brandMark(), "Monitoring"), h("h1", null, title), h("p", { class: "muted", style: "margin:4px 0 0" }, subtitle)),
    fields, error, button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    try {
      await onSubmit(new FormData(form));
      state.auth = await api("GET", "/api/auth/status");
      route();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
  fill($("#app"), h("div", { class: "auth-wrap" }, form));
  $("input", form).focus();
}

function textField(label, props, help) {
  return h("label", { class: "field" }, h("span", null, label), h("input", props), help ? h("div", { class: "help" }, help) : null);
}

function renderLogin() {
  authScreen("Anmelden", "Bitte melde dich an, um fortzufahren.", [
    textField("Benutzername", { name: "username", type: "text", autocomplete: "username", required: true }),
    textField("Passwort", { name: "password", type: "password", autocomplete: "current-password", required: true }),
  ], "Anmelden", (data) => api("POST", "/api/auth/login", { username: data.get("username"), password: data.get("password") }));
}

function renderSetup() {
  authScreen("Ersteinrichtung", "Lege das Administrator-Konto für dein Monitoring an.", [
    textField("Benutzername", { name: "username", type: "text", autocomplete: "username", required: true, value: "admin" }),
    textField("Passwort", { name: "password", type: "password", autocomplete: "new-password", required: true, minlength: 8 }, "Mindestens 8, höchstens 72 Zeichen"),
    textField("Passwort wiederholen", { name: "password2", type: "password", autocomplete: "new-password", required: true }),
  ], "Konto anlegen", (data) => {
    if (data.get("password") !== data.get("password2")) throw new Error("Die Passwörter stimmen nicht überein");
    return api("POST", "/api/auth/setup", { username: data.get("username"), password: data.get("password") });
  });
}

/* =====================================================================
 * Dashboard
 * ===================================================================== */

function beatsEl(beats, slots = 40) {
  const list = beats.slice(-slots);
  const fails = list.filter((b) => !b.ok).length;
  const label = list.length ? `Letzte ${list.length} Ergebnisse: ${list.length - fails} OK, ${fails} Fehler` : "Noch keine Ergebnisse";
  const el = h("div", { class: "beats", role: "img", "aria-label": label, title: label });
  for (let i = list.length; i < slots; i++) el.append(h("i", { class: "empty" }));
  for (const beat of list) {
    el.append(h("i", { class: beat.ok ? null : "fail", title: `${fmtDateTime(beat.ts)} – ${beat.ok ? "OK" : "Fehler"}${beat.message ? ": " + beat.message : ""}` }));
  }
  return el;
}

function targetText(monitor) {
  if (monitor.type === "push") return `meldet sich alle ${fmtInterval(monitor.interval)}`;
  return monitor.target;
}

function monitorRow(monitor) {
  const metrics = monitor.last_metrics || {};
  let secondLabel = "Antwort";
  let secondValue = fmtMs(monitor.last_latency);
  if (monitor.type === "push" && metrics.cpu !== undefined) {
    secondLabel = "CPU";
    secondValue = fmtMetric("cpu", metrics.cpu);
  }
  return h("a", { class: "mon-row", href: `#/monitor/${monitor.id}` },
    statusEl(monitor.status),
    h("div", { style: "min-width:0" },
      h("div", { class: "mon-name" }, monitor.name),
      h("div", { class: "mon-target" }, h("span", { class: "badge" }, TYPES[monitor.type].badge), " ", targetText(monitor))),
    beatsEl(monitor.beats || []),
    h("div", { class: "mon-num hide-sm" }, h("span", { class: "k" }, "Uptime 24 h"), fmtPct(monitor.uptime_24h)),
    h("div", { class: "mon-num hide-sm" }, h("span", { class: "k" }, secondLabel), secondValue),
    h("div", { class: "mon-num" }, h("span", { class: "k" }, "Zuletzt"), fmtAgo(monitor.last_check_at)));
}

function tile(label, value, extra) {
  return h("div", { class: "card tile" }, h("div", { class: "label" }, extra || null, label), h("div", { class: "value" }, value));
}

function statusTile(status, count) {
  return tile(STATUS[status], String(count), h("span", { class: `status status-${status}` }, h("span", { class: "dot", "aria-hidden": "true" })));
}

function emptyDashboard() {
  const how = (title, text) => h("div", { class: "card" }, h("strong", null, title), h("p", { class: "muted small" }, text));
  return h("div", { class: "card empty-state" },
    h("h2", null, "Noch nichts überwacht"),
    h("p", null, "Lege deinen ersten Monitor an. Es gibt zwei Wege, Systeme anzubinden:"),
    h("div", { class: "how-grid" },
      how("Pull – der Server prüft", "Websites & APIs (HTTP), Ports (TCP), Ping und DNS. Auf dem Zielsystem muss nichts installiert werden."),
      how("Push – das System meldet sich", "Jedes System, das eine URL aufrufen kann: Server-Agent (CPU, RAM, Disk), Cronjobs, Backups, Home Assistant, IoT, Skripte. Funktioniert auch hinter NAT/Firewall.")),
    h("a", { class: "btn btn-primary", href: "#/monitor/new" }, "+ Ersten Monitor anlegen"));
}

async function viewDashboard(main) {
  const filters = storageGet("filters", { q: "", group: "", status: "" });
  let monitors = [];

  const tilesEl = h("div", { class: "tiles" });
  const listEl = h("div");
  const eventsEl = h("div", { class: "card" });
  const search = h("input", { type: "search", placeholder: "Suchen …", "aria-label": "Monitore durchsuchen", value: filters.q });
  const groupSelect = h("select", { "aria-label": "Gruppe filtern" });
  const statusSelect = h("select", { "aria-label": "Status filtern" },
    h("option", { value: "" }, "Alle Status"),
    Object.entries(STATUS).map(([value, label]) => h("option", { value, selected: filters.status === value }, label)));

  const update = () => {
    filters.q = search.value;
    filters.group = groupSelect.value;
    filters.status = statusSelect.value;
    storageSet("filters", filters);
    renderList();
  };
  search.addEventListener("input", update);
  groupSelect.addEventListener("change", update);
  statusSelect.addEventListener("change", update);

  const toolbar = h("div", { class: "toolbar" }, search, groupSelect, statusSelect);
  const content = h("div", { class: "grid-2" }, h("div", null, toolbar, listEl), eventsEl);

  main.append(
    h("div", { class: "page-head" },
      h("div", null, h("h1", null, "Übersicht"), h("div", { class: "sub" }, "Alle überwachten Systeme auf einen Blick")),
      h("a", { class: "btn btn-primary", href: "#/monitor/new" }, "+ Neuer Monitor")),
    tilesEl, content);

  function renderTiles() {
    const count = (status) => monitors.filter((m) => m.status === status).length;
    fill(tilesEl, 
      tile("Monitore", String(monitors.length)),
      statusTile("up", count("up")),
      statusTile("down", count("down")),
      statusTile("pending", count("pending")),
      statusTile("paused", count("paused")));
  }

  function renderGroupOptions() {
    const groups = [...new Set(monitors.map((m) => m.group_name))].sort((a, b) => a.localeCompare(b, "de"));
    if (filters.group && !groups.includes(filters.group)) filters.group = "";
    fill(groupSelect, 
      h("option", { value: "" }, "Alle Gruppen"),
      groups.map((g) => h("option", { value: g, selected: g === filters.group }, g || "Ohne Gruppe")));
    groupSelect.value = filters.group;
  }

  function renderList() {
    const q = filters.q.trim().toLowerCase();
    const visible = monitors.filter((m) =>
      (!q || m.name.toLowerCase().includes(q) || m.target.toLowerCase().includes(q) || m.group_name.toLowerCase().includes(q)) &&
      (!filters.group || m.group_name === filters.group) &&
      (!filters.status || m.status === filters.status));
    if (!visible.length) {
      fill(listEl, h("div", { class: "card card-pad muted" }, "Keine Monitore für diesen Filter."));
      return;
    }
    const groups = new Map();
    for (const monitor of visible) {
      if (!groups.has(monitor.group_name)) groups.set(monitor.group_name, []);
      groups.get(monitor.group_name).push(monitor);
    }
    const hasGroups = groups.size > 1 || !groups.has("");
    const card = h("div", { class: "card" });
    for (const [group, items] of groups) {
      if (hasGroups) {
        const down = items.filter((m) => m.status === "down").length;
        card.append(h("div", { class: "group-head" }, group || "Ohne Gruppe",
          h("span", { class: "count" }, `${items.length}`),
          down ? h("span", { class: "status status-down" }, h("span", { class: "dot", "aria-hidden": "true" }), `${down} Störung`) : null));
      }
      items.forEach((m) => card.append(monitorRow(m)));
    }
    fill(listEl, card);
  }

  function renderEvents(events) {
    fill(eventsEl, 
      h("div", { class: "card-head" }, h("h2", null, "Letzte Ereignisse")),
      events.length
        ? h("ul", { class: "events" }, events.map((e) => h("li", null,
          h("div", { class: "ev-top" }, statusEl(e.status), h("span", { class: "muted small", title: fmtDateTime(e.ts) }, fmtAgo(e.ts))),
          h("a", { href: `#/monitor/${e.monitor_id}` }, e.monitor_name),
          e.message ? h("div", { class: "ev-msg" }, e.message) : null)))
        : h("div", { class: "card-pad muted" }, "Noch keine Statuswechsel."));
  }

  async function load() {
    const [monitorList, events] = await Promise.all([api("GET", "/api/monitors"), api("GET", "/api/events?limit=15")]);
    monitors = monitorList;
    if (!monitors.length) {
      fill(tilesEl);
      fill(content, emptyDashboard());
      return;
    }
    if (!content.contains(toolbar)) fill(content, h("div", null, toolbar, listEl), eventsEl);
    renderTiles();
    renderGroupOptions();
    renderList();
    renderEvents(events);
  }

  await load();
  every(10000, () => load().catch(reportError));
}

/* =====================================================================
 * Charts
 * ===================================================================== */

function niceTicks(lo, hi, count = 4) {
  if (!(hi > lo)) hi = lo + 1;
  const raw = (hi - lo) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / magnitude;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * magnitude;
  const ticks = [];
  for (let v = Math.floor(lo / step) * step; v <= Math.ceil(hi / step) * step + step / 2; v += step) {
    ticks.push(Number(v.toFixed(10)));
  }
  return ticks;
}

function timeTicks(start, end, width) {
  const steps = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  const wanted = Math.max(2, Math.floor(width / 110));
  const step = steps.find((st) => (end - start) / st <= wanted) || steps[steps.length - 1];
  const offset = -new Date().getTimezoneOffset() * 60;
  const ticks = [];
  for (let t = Math.ceil((start + offset) / step) * step - offset; t <= end; t += step) ticks.push(t);
  return { ticks, withDate: step >= 86400 };
}

/** Line chart for one series over time; failed buckets are shaded. */
function drawLineChart(container, { points, get, start, end, format, fixedMax, label, height = 170 }) {
  fill(container);
  const width = Math.max(container.clientWidth, 260);
  const m = { top: 10, right: 14, bottom: 24, left: 52 };
  const iw = width - m.left - m.right;
  const ih = height - m.top - m.bottom;
  const data = points.map((p) => ({ p, v: get(p) }));
  const values = data.filter((d) => d.v !== null && d.v !== undefined).map((d) => d.v);

  const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label, tabindex: 0 });
  const x = (t) => m.left + ((t - start) / (end - start)) * iw;

  const ticks = niceTicks(Math.min(0, ...values), fixedMax !== undefined ? fixedMax : Math.max(...values, 0) * 1.05 || 1);
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const y = (v) => m.top + ih - ((v - lo) / (hi - lo)) * ih;

  for (const t of ticks) {
    svg.append(s("line", { class: t === lo ? "base-line" : "grid-line", x1: m.left, x2: width - m.right, y1: y(t), y2: y(t) }));
    svg.append(s("text", { class: "axis-label", x: m.left - 8, y: y(t) + 4, "text-anchor": "end" }, format(t)));
  }
  const { ticks: xTicks, withDate } = timeTicks(start, end, iw);
  for (const t of xTicks) {
    svg.append(s("text", { class: "axis-label", x: x(t), y: height - 6, "text-anchor": "middle" }, fmtTimeShort(t, withDate)));
  }

  // Typical spacing between points, used for band width and to break the line at gaps.
  const gaps = [];
  for (let i = 1; i < data.length; i++) gaps.push(data[i].p.ts - data[i - 1].p.ts);
  gaps.sort((a, b) => a - b);
  const spacing = gaps.length ? gaps[Math.floor(gaps.length / 2)] : (end - start) / 60;
  const bandWidth = Math.max(2, (spacing / (end - start)) * iw);

  for (const d of data) {
    if (d.p.fails > 0) {
      svg.append(s("rect", { class: "fail-band", x: x(d.p.ts) - bandWidth / 2, y: m.top, width: bandWidth, height: ih }));
      svg.append(s("rect", { class: "fail-tick", x: x(d.p.ts) - bandWidth / 2, y: m.top + ih - 3, width: bandWidth, height: 3 }));
    }
  }

  if (!values.length) {
    svg.append(s("text", { class: "empty-label", x: m.left + iw / 2, y: m.top + ih / 2, "text-anchor": "middle" }, "Keine Messwerte im Zeitraum"));
    container.append(svg);
    return;
  }

  const segments = [];
  let current = [];
  let prevTs = null;
  for (const d of data) {
    const broken = d.v === null || d.v === undefined || (prevTs !== null && d.p.ts - prevTs > spacing * 3.5);
    if (broken && current.length) {
      segments.push(current);
      current = [];
    }
    if (d.v !== null && d.v !== undefined) current.push(d);
    prevTs = d.p.ts;
  }
  if (current.length) segments.push(current);

  for (const seg of segments) {
    const line = seg.map((d, i) => `${i ? "L" : "M"}${x(d.p.ts).toFixed(1)},${y(d.v).toFixed(1)}`).join("");
    if (seg.length > 1) {
      const area = `${line}L${x(seg[seg.length - 1].p.ts).toFixed(1)},${y(lo)}L${x(seg[0].p.ts).toFixed(1)},${y(lo)}Z`;
      svg.append(s("path", { class: "series-area", d: area }));
      svg.append(s("path", { class: "series-line", d: line }));
    } else {
      svg.append(s("circle", { class: "end-dot", cx: x(seg[0].p.ts), cy: y(seg[0].v), r: 3 }));
    }
  }
  const last = data.filter((d) => d.v !== null && d.v !== undefined).pop();
  svg.append(s("circle", { class: "end-dot", cx: x(last.p.ts), cy: y(last.v), r: 4 }));

  // Hover / keyboard layer: crosshair snaps to the nearest point.
  const crosshair = s("line", { class: "crosshair", y1: m.top, y2: m.top + ih, visibility: "hidden" });
  const hoverDot = s("circle", { class: "hover-dot", r: 4.5, visibility: "hidden" });
  const hit = s("rect", { x: m.left, y: 0, width: iw, height: height, fill: "transparent" });
  svg.append(crosshair, hoverDot, hit);
  const tip = h("div", { class: "tooltip", hidden: true });
  container.append(svg, tip);

  let index = -1;
  function show(i) {
    index = Math.max(0, Math.min(data.length - 1, i));
    const d = data[index];
    const cx = x(d.p.ts);
    crosshair.setAttribute("x1", cx);
    crosshair.setAttribute("x2", cx);
    crosshair.setAttribute("visibility", "visible");
    if (d.v !== null && d.v !== undefined) {
      hoverDot.setAttribute("cx", cx);
      hoverDot.setAttribute("cy", y(d.v));
      hoverDot.setAttribute("visibility", "visible");
    } else {
      hoverDot.setAttribute("visibility", "hidden");
    }
    fill(tip, 
      h("div", { class: "t-time" }, fmtDateTime(d.p.ts), d.p.count > 1 ? ` · Ø aus ${d.p.count}` : ""),
      h("div", { class: "t-row" }, h("span", { class: "t-key" }), h("span", { class: "t-val" }, d.v === null || d.v === undefined ? "–" : format(d.v)), h("span", { class: "muted" }, label)),
      d.p.fails ? h("div", { class: "t-fail" }, `✕ ${d.p.fails} Fehler${d.p.message ? ": " + d.p.message : ""}`) : null);
    tip.hidden = false;
    const tipWidth = tip.offsetWidth;
    const left = cx + 14 + tipWidth > width ? cx - 14 - tipWidth : cx + 14;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `${m.top}px`;
  }
  function hide() {
    crosshair.setAttribute("visibility", "hidden");
    hoverDot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  }
  function nearest(clientX) {
    const rect = svg.getBoundingClientRect();
    const t = start + ((clientX - rect.left - m.left) / iw) * (end - start);
    let best = 0;
    for (let i = 1; i < data.length; i++) {
      if (Math.abs(data[i].p.ts - t) < Math.abs(data[best].p.ts - t)) best = i;
    }
    return best;
  }
  svg.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("focus", () => show(data.length - 1));
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { show(index - 1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { show(index + 1); e.preventDefault(); }
    else if (e.key === "Home") { show(0); e.preventDefault(); }
    else if (e.key === "End") { show(data.length - 1); e.preventDefault(); }
  });
}

/** Availability strip: the range split into equal slots, red if any check in the slot failed. */
function availabilityStrip(points, start, end, slots = 90) {
  const buckets = Array.from({ length: slots }, () => ({ ok: 0, fail: 0, message: "" }));
  for (const p of points) {
    const i = Math.min(slots - 1, Math.max(0, Math.floor(((p.ts - start) / (end - start)) * slots)));
    buckets[i].ok += p.count - p.fails;
    buckets[i].fail += p.fails;
    if (p.fails) buckets[i].message = p.message;
  }
  const size = (end - start) / slots;
  const total = points.reduce((sum, p) => sum + p.count, 0);
  const failed = points.reduce((sum, p) => sum + p.fails, 0);
  const pct = total ? ((total - failed) / total) * 100 : null;
  const strip = h("div", { class: "strip", role: "img", "aria-label": `Verfügbarkeit im Zeitraum: ${fmtPct(pct)}` },
    buckets.map((b, i) => {
      const from = start + i * size;
      const cls = b.fail ? "fail" : b.ok ? null : "none";
      const text = b.fail
        ? `${fmtDateTime(from)}: ${b.fail} Fehler${b.message ? " – " + b.message : ""}`
        : b.ok ? `${fmtDateTime(from)}: OK` : `${fmtDateTime(from)}: keine Daten`;
      return h("i", { class: cls, title: text });
    }));
  const withDate = end - start > 86400;
  return { pct, el: h("div", null, strip, h("div", { class: "strip-axis" }, h("span", null, fmtTimeShort(start, withDate)), h("span", null, "jetzt"))) };
}

/* =====================================================================
 * Monitor detail
 * ===================================================================== */

function integrationCard(monitor, onRegenerate) {
  const url = pushUrl(monitor);
  const base = baseUrl();
  const intervalMin = Math.max(1, Math.round(monitor.interval / 60));
  const tabs = [
    ["Heartbeat", [
      h("p", { class: "muted" }, "Für Cronjobs, Backups und Skripte: Die URL nach einem erfolgreichen Lauf aufrufen. Bleibt der Aufruf aus, schlägt der Monitor Alarm."),
      codeBlock(`curl -fsS -m 10 "${url}"`),
      h("p", { class: "muted" }, "Beispiel Cronjob – meldet Erfolg oder Fehler mit Text:"),
      codeBlock(`0 3 * * * /usr/local/bin/backup.sh && curl -fsS -m 10 "${url}?msg=Backup+ok" || curl -fsS -m 10 "${url}?status=down&msg=Backup+fehlgeschlagen"`),
    ]],
    ["Mit Metriken", [
      h("p", { class: "muted" }, "Beliebige Zahlenwerte mitsenden – sie werden als Diagramme angezeigt und können Grenzwerte auslösen."),
      codeBlock(`curl -fsS -m 10 -X POST "${url}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"status": "up", "message": "alles gut", "metrics": {"cpu": 42.5, "queue_length": 17}}'`),
      h("p", { class: "muted" }, "Oder einfach per GET mit Query-Parametern:"),
      codeBlock(`curl -fsS "${url}?status=up&msg=OK&temperatur=21.5&ping=12"`),
    ]],
    ["Linux-Server", [
      h("p", { class: "muted" }, `Installiert den Agent und meldet jede Minute CPU, RAM, Swap, Festplatte, Load und Uptime. Stelle das Intervall des Monitors auf 60 s (aktuell: ${fmtInterval(monitor.interval)}).`),
      codeBlock(`sudo curl -fsSL ${base}/agent/linux-agent.sh -o /usr/local/bin/monitor-agent\nsudo chmod +x /usr/local/bin/monitor-agent\necho '* * * * * root /usr/local/bin/monitor-agent ${url} >/dev/null 2>&1' | sudo tee /etc/cron.d/monitor-agent`),
      h("p", { class: "muted" }, "Weitere Mountpoints mitmelden: MONITOR_DISK_PATHS=\"/ /data\" vor den Befehl in der Cron-Zeile setzen."),
    ]],
    ["Windows-Server", [
      h("p", { class: "muted" }, "In einer PowerShell als Administrator ausführen – legt eine geplante Aufgabe an, die jede Minute meldet:"),
      codeBlock(`New-Item -ItemType Directory -Force C:\\monitor | Out-Null\nInvoke-WebRequest ${base}/agent/windows-agent.ps1 -OutFile C:\\monitor\\windows-agent.ps1\n$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\\monitor\\windows-agent.ps1 -Url ${url}"\n$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)\nRegister-ScheduledTask -TaskName "MonitorAgent" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest`),
    ]],
    ["Home Assistant", [
      h("p", { class: "muted" }, "rest_command in der configuration.yaml anlegen und per Automation regelmäßig aufrufen:"),
      codeBlock(`rest_command:\n  monitoring_push:\n    url: "${url}"\n    method: POST\n    content_type: "application/json"\n    payload: >-\n      {"status": "up", "metrics": {"temperatur": {{ states('sensor.wohnzimmer_temperatur') | float(0) }}}}\n\nautomation:\n  - alias: "Monitoring Heartbeat"\n    triggers:\n      - trigger: time_pattern\n        minutes: "/${intervalMin}"\n    actions:\n      - action: rest_command.monitoring_push`),
    ]],
    ["Code", [
      h("p", { class: "muted" }, "Jede Sprache mit HTTP-Client funktioniert. Beispiel Python:"),
      codeBlock(`import requests\n\nrequests.post(\n    "${url}",\n    json={"status": "up", "message": "Import fertig", "metrics": {"rows": 1532, "dauer_s": 42}},\n    timeout=10,\n)`),
      h("p", { class: "muted" }, "Status-Werte: up / ok (alles gut) oder down / fail / error (Alarm). Ohne Status gilt „up“."),
    ]],
  ];

  let selected = storageGet("integrationTab", 0);
  if (selected >= tabs.length) selected = 0;
  const panel = h("div", { class: "card-pad fields" });
  const tabButtons = tabs.map(([title], i) => h("button", { type: "button", role: "tab", "aria-selected": String(i === selected), onclick: () => select(i) }, title));
  function select(i) {
    selected = i;
    storageSet("integrationTab", i);
    tabButtons.forEach((b, j) => b.setAttribute("aria-selected", String(i === j)));
    fill(panel, ...tabs[i][1]);
  }
  select(selected);

  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", null, h("h2", null, "Anbindung"), h("div", { class: "hint" }, "Das überwachte System ruft diese URL auf. Das Token in der URL ist das Passwort – behandle es vertraulich.")),
      h("button", { type: "button", class: "btn btn-sm", onclick: onRegenerate }, "Neues Token")),
    h("div", { class: "card-pad", style: "padding-bottom:0" }, inlineCode(url)),
    h("div", { class: "tabs", role: "tablist", style: "margin-top:12px" }, tabButtons),
    panel);
}

async function viewMonitorDetail(main, id) {
  let range = storageGet("range", "24h");
  let monitor = await api("GET", `/api/monitors/${id}`);
  let results = null;
  let events = [];

  const headEl = h("div", { class: "page-head" });
  const tilesEl = h("div", { class: "tiles" });
  const rangeButtons = ["1h", "24h", "7d", "30d"].map((r) =>
    h("button", { type: "button", "aria-pressed": String(r === range), onclick: () => setRange(r) }, { "1h": "1 Std.", "24h": "24 Std.", "7d": "7 Tage", "30d": "30 Tage" }[r]));
  const chartsEl = h("div");
  const tableEl = h("details", { class: "card table-view" });
  const integrationEl = h("div");
  const eventsEl = h("div", { class: "card" });

  main.append(
    h("a", { href: "#/", class: "small" }, "← Übersicht"),
    h("div", { style: "height:8px" }),
    headEl, tilesEl,
    h("div", { class: "toolbar" }, h("div", { class: "segmented", role: "group", "aria-label": "Zeitraum" }, rangeButtons)),
    chartsEl, h("div", { style: "height:16px" }), tableEl, h("div", { style: "height:16px" }), integrationEl, h("div", { style: "height:16px" }), eventsEl);

  async function act(fn, success) {
    try {
      await fn();
      if (success) toast(success);
      await refresh();
    } catch (e) {
      reportError(e);
    }
  }

  function renderHead() {
    const isPull = monitor.type !== "push";
    const checkButton = h("button", { type: "button", class: "btn", hidden: !isPull }, "Jetzt prüfen");
    checkButton.addEventListener("click", async () => {
      checkButton.disabled = true;
      checkButton.textContent = "Prüfe …";
      try {
        const r = await api("POST", `/api/monitors/${id}/check`);
        toast(`${r.ok ? "OK" : "Fehler"}: ${r.message}`, !r.ok);
        await refresh();
      } catch (e) {
        reportError(e);
      } finally {
        checkButton.disabled = false;
        checkButton.textContent = "Jetzt prüfen";
      }
    });
    fill(headEl, 
      h("div", { style: "min-width:0" },
        h("div", { class: "row" }, h("h1", null, monitor.name), h("span", { class: "pill" }, statusEl(monitor.status))),
        h("div", { class: "sub" },
          h("span", { class: "badge" }, TYPES[monitor.type].badge), " ", targetText(monitor),
          monitor.group_name ? ` · Gruppe: ${monitor.group_name}` : "",
          ` · Intervall ${fmtInterval(monitor.interval)}`),
        monitor.last_message ? h("div", { class: "sub" }, "Letzte Meldung: ", monitor.last_message) : null),
      h("div", { class: "row" },
        checkButton,
        monitor.enabled
          ? h("button", { type: "button", class: "btn", onclick: () => act(() => api("POST", `/api/monitors/${id}/pause`), "Monitor pausiert") }, "Pausieren")
          : h("button", { type: "button", class: "btn", onclick: () => act(() => api("POST", `/api/monitors/${id}/resume`), "Monitor fortgesetzt") }, "Fortsetzen"),
        h("a", { class: "btn", href: `#/monitor/${id}/edit` }, "Bearbeiten"),
        h("button", {
          type: "button", class: "btn btn-danger", onclick: async () => {
            if (!confirm(`Monitor „${monitor.name}“ mit allen Messwerten löschen?`)) return;
            try {
              await api("DELETE", `/api/monitors/${id}`);
              toast("Monitor gelöscht");
              navigate("#/");
            } catch (e) {
              reportError(e);
            }
          },
        }, "Löschen")));
  }

  function renderTiles() {
    const st = monitor.stats;
    const tiles = [
      tile("Uptime 24 h", fmtPct(st.uptime_24h)),
      tile("Uptime 7 Tage", fmtPct(st.uptime_7d)),
      tile("Uptime 30 Tage", fmtPct(st.uptime_30d)),
    ];
    if (monitor.type === "push") {
      tiles.push(tile("Letztes Signal", fmtAgo(monitor.last_push_at)));
    } else {
      tiles.push(tile("Ø Antwortzeit 24 h", fmtMs(st.avg_latency_24h)));
      tiles.push(tile("Letzte Prüfung", fmtAgo(monitor.last_check_at)));
    }
    for (const [key, value] of Object.entries(monitor.last_metrics || {})) {
      if (metricInfo(key).static) tiles.push(tile(metricInfo(key).label, fmtMetric(key, value)));
    }
    fill(tilesEl, ...tiles);
  }

  function chartCard(title, current, draw) {
    const chart = h("div", { class: "chart" });
    const card = h("div", { class: "card chart-card" },
      h("div", { class: "chart-title" }, h("h3", null, title), current ? h("div", { class: "now" }, "aktuell ", h("strong", null, current)) : null),
      chart);
    return { card, draw: () => draw(chart) };
  }

  function renderCharts() {
    if (!results) return;
    const { points, start, end, metric_keys: metricKeys } = results;
    const drawers = [];
    const strip = availabilityStrip(points, start, end);
    const children = [
      h("div", { class: "card chart-card" },
        h("div", { class: "chart-title" }, h("h3", null, "Verfügbarkeit"), h("div", { class: "now" }, "im Zeitraum ", h("strong", null, fmtPct(strip.pct)))),
        strip.el),
    ];

    if (points.some((p) => p.latency !== null && p.latency !== undefined)) {
      const c = chartCard("Antwortzeit", fmtMs(monitor.last_latency), (el) => drawLineChart(el, {
        points, start, end, get: (p) => p.latency, format: fmtMs, label: "Antwortzeit",
      }));
      children.push(h("div", { style: "height:16px" }), c.card);
      drawers.push(c.draw);
    }

    const charted = metricKeys.filter((k) => !metricInfo(k).static);
    if (charted.length) {
      const grid = h("div", { class: "small-multiples" });
      for (const key of charted) {
        const info = metricInfo(key);
        const c = chartCard(info.label, fmtMetric(key, (monitor.last_metrics || {})[key]), (el) => drawLineChart(el, {
          points, start, end, get: (p) => p.metrics[key], fixedMax: info.max, height: 150,
          format: (v) => fmtMetric(key, v), label: info.label,
        }));
        grid.append(c.card);
        drawers.push(c.draw);
      }
      children.push(h("div", { style: "height:16px" }), grid);
    }
    chartsEl.classList.remove("refreshing");
    fill(chartsEl, ...children);
    drawers.forEach((draw) => draw());
    renderTable(points, metricKeys);
  }

  function renderTable(points, metricKeys) {
    const wasOpen = tableEl.open;
    const rows = points.slice(-100).reverse();
    fill(tableEl, 
      h("summary", null, `Messwerte als Tabelle (${rows.length} neueste Einträge)`),
      h("div", { class: "table-scroll" },
        h("table", { class: "data" },
          h("thead", null, h("tr", null,
            h("th", null, "Zeit"), h("th", null, "Status"), h("th", { class: "num" }, "Antwortzeit"),
            metricKeys.map((k) => h("th", { class: "num" }, metricInfo(k).label)),
            h("th", null, "Meldung"))),
          h("tbody", null, rows.map((p) => h("tr", null,
            h("td", null, fmtDateTime(p.ts)),
            h("td", null, statusEl(p.ok ? "up" : "down"), p.count > 1 ? h("span", { class: "muted small" }, ` (${p.count - p.fails}/${p.count})`) : null),
            h("td", { class: "num" }, fmtMs(p.latency)),
            metricKeys.map((k) => h("td", { class: "num" }, fmtMetric(k, p.metrics[k]))),
            h("td", { class: "muted" }, p.message)))))));
    tableEl.open = wasOpen;
  }

  function renderEvents() {
    fill(eventsEl, 
      h("div", { class: "card-head" }, h("h2", null, "Statuswechsel")),
      events.length
        ? h("div", { class: "table-scroll" }, h("table", { class: "data" },
          h("thead", null, h("tr", null, h("th", null, "Zeit"), h("th", null, "Status"), h("th", null, "Meldung"))),
          h("tbody", null, events.map((e) => h("tr", null,
            h("td", { style: "white-space:nowrap" }, fmtDateTime(e.ts)),
            h("td", null, statusEl(e.status)),
            h("td", { class: "muted" }, e.message))))))
        : h("div", { class: "card-pad muted" }, "Noch keine Statuswechsel."));
  }

  function renderIntegration() {
    if (monitor.type !== "push") {
      fill(integrationEl);
      return;
    }
    fill(integrationEl, integrationCard(monitor, async () => {
      if (!confirm("Neues Token erzeugen? Die bisherige URL funktioniert danach nicht mehr – alle angebundenen Systeme müssen angepasst werden.")) return;
      try {
        monitor = { ...monitor, ...(await api("POST", `/api/monitors/${id}/token`)) };
        renderIntegration();
        toast("Neues Token erzeugt");
      } catch (e) {
        reportError(e);
      }
    }));
  }

  async function loadResults() {
    results = await api("GET", `/api/monitors/${id}/results?range=${range}`);
  }

  async function refresh() {
    const [m, , ev] = await Promise.all([
      api("GET", `/api/monitors/${id}`),
      loadResults(),
      api("GET", `/api/events?monitor_id=${id}&limit=30`),
    ]);
    const tokenChanged = m.push_path !== monitor.push_path || m.type !== monitor.type;
    monitor = m;
    events = ev;
    renderHead();
    renderTiles();
    renderCharts();
    renderEvents();
    if (tokenChanged) renderIntegration();
  }

  async function setRange(r) {
    range = r;
    storageSet("range", r);
    rangeButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(["1h", "24h", "7d", "30d"][i] === r)));
    chartsEl.classList.add("refreshing");
    try {
      await loadResults();
      renderCharts();
    } catch (e) {
      chartsEl.classList.remove("refreshing");
      reportError(e);
    }
  }

  renderHead();
  renderTiles();
  renderIntegration();
  await refresh();

  let resizeTimer = null;
  const onResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderCharts, 150);
  };
  window.addEventListener("resize", onResize);
  onCleanup(() => window.removeEventListener("resize", onResize));
  every(15000, () => refresh().catch(reportError));
}

/* =====================================================================
 * Monitor form
 * ===================================================================== */

const SERVER_THRESHOLDS = [
  { metric: "cpu", op: ">", value: 90 },
  { metric: "mem", op: ">", value: 90 },
  { metric: "disk", op: ">", value: 90 },
];

function defaultConfig(type) {
  switch (type) {
    case "http":
      return { method: "GET", expected_status: "200-399", keyword: "", keyword_invert: false, verify_tls: true, follow_redirects: true, cert_expiry_days: 14, headers: {}, body: "" };
    case "push":
      return { grace: 60, thresholds: [] };
    case "dns":
      return { expected: "" };
    default:
      return {};
  }
}

async function viewMonitorForm(main, id) {
  const [existing, channels, monitors] = await Promise.all([
    id ? api("GET", `/api/monitors/${id}`) : Promise.resolve(null),
    api("GET", "/api/channels"),
    api("GET", "/api/monitors"),
  ]);
  const groups = [...new Set(monitors.map((m) => m.group_name).filter(Boolean))].sort((a, b) => a.localeCompare(b, "de"));

  const draft = existing
    ? {
      name: existing.name, type: existing.type, target: existing.target, group_name: existing.group_name,
      interval: existing.interval, timeout: existing.timeout, retries: existing.retries,
      config: { ...defaultConfig(existing.type), ...existing.config }, channel_ids: [...existing.channel_ids], enabled: existing.enabled,
    }
    : {
      name: "", type: "http", target: "", group_name: "", interval: 60, timeout: 10, retries: 1,
      config: defaultConfig("http"), channel_ids: channels.filter((c) => c.enabled).map((c) => c.id), enabled: true,
    };
  // Keep per-type settings when switching back and forth between types.
  const configs = { [draft.type]: draft.config };
  let headersText = draft.type === "http" && Object.keys(draft.config.headers || {}).length ? JSON.stringify(draft.config.headers, null, 2) : "";

  const title = existing ? `„${existing.name}“ bearbeiten` : "Neuer Monitor";
  const formEl = h("form", { class: "fields", novalidate: true });
  main.append(
    h("a", { href: existing ? `#/monitor/${id}` : "#/", class: "small" }, "← Zurück"),
    h("div", { style: "height:8px" }),
    h("div", { class: "page-head" }, h("h1", null, title)),
    formEl);

  const bind = (obj, key, type = "text") => ({
    value: obj[key] === undefined || obj[key] === null ? "" : String(obj[key]),
    onInput: (e) => {
      obj[key] = type === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value;
    },
  });
  const bindCheck = (obj, key) => ({ checked: !!obj[key], onChange: (e) => { obj[key] = e.target.checked; } });
  const field = (label, input, help) => h("label", { class: "field" }, h("span", null, label), input, help ? h("div", { class: "help" }, help) : null);
  const section = (heading, hint, ...content) => h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", null, h("h2", null, heading), hint ? h("div", { class: "hint" }, hint) : null)),
    h("div", { class: "card-pad fields" }, content));

  function typeSection() {
    return section("Art der Überwachung", null,
      h("div", { class: "type-grid", role: "radiogroup", "aria-label": "Art der Überwachung" },
        Object.entries(TYPES).map(([key, info]) => h("label", { class: "type-option" + (draft.type === key ? " selected" : "") },
          h("input", {
            type: "radio", name: "type", value: key, checked: draft.type === key, onChange: () => {
              configs[draft.type] = draft.config;
              draft.type = key;
              draft.config = configs[key] || defaultConfig(key);
              render();
              $(`input[name=type][value=${key}]`, formEl).focus();
            },
          }),
          h("strong", null, info.label), h("span", null, info.desc)))));
  }

  function generalSection() {
    const info = TYPES[draft.type];
    return section("Allgemein", null,
      h("div", { class: "fields-2" },
        field("Name", h("input", { type: "text", required: true, maxlength: 100, placeholder: "z. B. Webshop, NAS, Backup-Job", ...bind(draft, "name") })),
        field("Gruppe", h("input", { type: "text", list: "group-list", maxlength: 100, placeholder: "optional, z. B. Kunde A, Heimnetz", ...bind(draft, "group_name") }),
          "Zum Gruppieren in der Übersicht")),
      h("datalist", { id: "group-list" }, groups.map((g) => h("option", { value: g }))),
      draft.type === "push" ? null : field(info.targetLabel, h("input", { type: "text", required: true, placeholder: info.placeholder, ...bind(draft, "target") }), info.help));
  }

  function httpSection() {
    const c = draft.config;
    return section("HTTP-Prüfung", "Wann gilt die Seite als erreichbar?",
      h("div", { class: "fields-3" },
        field("Methode", h("select", { onChange: (e) => { c.method = e.target.value; } },
          ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].map((m) => h("option", { value: m, selected: c.method === m }, m)))),
        field("Erwartete Statuscodes", h("input", { type: "text", ...bind(c, "expected_status") }), "z. B. 200-399, 200,204 oder 2xx"),
        field("Zertifikat-Warnung (Tage)", h("input", { type: "number", min: 0, max: 365, ...bind(c, "cert_expiry_days", "number") }), "Alarm, wenn das TLS-Zertifikat früher abläuft. 0 = aus")),
      h("div", { class: "fields-2" },
        field("Muss enthalten (Text)", h("input", { type: "text", placeholder: "optional", ...bind(c, "keyword") }), "Die Antwort muss diesen Text enthalten"),
        h("div", { class: "fields", style: "gap:8px;align-content:center" },
          h("label", { class: "check" }, h("input", { type: "checkbox", ...bindCheck(c, "keyword_invert") }), h("span", null, "Umkehren: Alarm, wenn der Text vorkommt")),
          h("label", { class: "check" }, h("input", { type: "checkbox", ...bindCheck(c, "verify_tls") }), h("span", null, "TLS-Zertifikat prüfen")),
          h("label", { class: "check" }, h("input", { type: "checkbox", ...bindCheck(c, "follow_redirects") }), h("span", null, "Weiterleitungen folgen")))),
      h("details", null,
        h("summary", { class: "muted", style: "cursor:pointer" }, "Header & Body (z. B. für APIs mit Token)"),
        h("div", { class: "fields-2", style: "margin-top:12px" },
          field("Header (JSON)", h("textarea", { placeholder: '{"Authorization": "Bearer …"}', value: headersText, onInput: (e) => { headersText = e.target.value; } })),
          field("Body", h("textarea", { placeholder: "optional", ...bind(c, "body") })))));
  }

  function dnsSection() {
    return section("DNS-Prüfung", null,
      field("Erwartete IP-Adresse", h("input", { type: "text", placeholder: "optional, z. B. 93.184.216.34", ...bind(draft.config, "expected") }),
        "Wenn gesetzt, muss diese Adresse in der Antwort enthalten sein"));
  }

  function pushSection() {
    const c = draft.config;
    c.thresholds = c.thresholds || [];
    const rows = h("div");
    const renderRows = () => {
      fill(rows, ...c.thresholds.map((rule, i) => h("div", { class: "threshold-row" },
        h("input", { type: "text", "aria-label": "Metrik", placeholder: "Metrik, z. B. cpu", ...bind(rule, "metric") }),
        h("select", { "aria-label": "Vergleich", onChange: (e) => { rule.op = e.target.value; } },
          [">", ">=", "<", "<=", "==", "!="].map((op) => h("option", { value: op, selected: rule.op === op }, op))),
        h("input", { type: "number", step: "any", "aria-label": "Grenzwert", placeholder: "Wert", ...bind(rule, "value", "number") }),
        h("button", { type: "button", class: "btn btn-icon btn-ghost", "aria-label": "Grenzwert entfernen", onclick: () => { c.thresholds.splice(i, 1); renderRows(); } }, "✕"))));
      if (!c.thresholds.length) rows.append(h("div", { class: "muted small", style: "margin-bottom:8px" }, "Keine Grenzwerte – nur ausbleibende Signale oder gemeldete Fehler lösen Alarm aus."));
    };
    renderRows();
    return section("Push-Einstellungen", "Das System ruft eine URL auf. Die URL erscheint nach dem Speichern.",
      field("Karenzzeit (Sekunden)", h("input", { type: "number", min: 0, max: 86400, ...bind(c, "grace", "number") }),
        "So lange wird nach Ablauf des Intervalls noch gewartet, bevor ein fehlendes Signal Alarm auslöst"),
      h("div", null,
        h("div", { class: "field-label" }, "Grenzwerte für Metriken"),
        h("div", { class: "help muted small", style: "margin-bottom:8px" }, "Alarm, wenn eine gemeldete Metrik die Bedingung erfüllt – z. B. cpu > 90."),
        rows,
        h("div", { class: "row" },
          h("button", { type: "button", class: "btn btn-sm", onclick: () => { c.thresholds.push({ metric: "", op: ">", value: "" }); renderRows(); } }, "+ Grenzwert"),
          h("button", {
            type: "button", class: "btn btn-sm", onclick: () => {
              for (const t of SERVER_THRESHOLDS) if (!c.thresholds.some((r) => r.metric === t.metric)) c.thresholds.push({ ...t });
              renderRows();
            },
          }, "Server-Standard (CPU / RAM / Disk > 90 %)"))));
  }

  function scheduleSection() {
    const isPush = draft.type === "push";
    return section("Zeitplan & Alarmierung", null,
      h("div", { class: "fields-3" },
        field(isPush ? "Erwartetes Intervall (Sekunden)" : "Intervall (Sekunden)",
          h("input", { type: "number", min: 10, max: 86400, required: true, ...bind(draft, "interval", "number") }),
          isPush ? "Wie oft sich das System meldet (Agent: 60)" : "Wie oft geprüft wird (min. 10)"),
        isPush ? null : field("Timeout (Sekunden)", h("input", { type: "number", min: 1, max: 120, ...bind(draft, "timeout", "number") })),
        field("Fehlversuche bis Alarm", h("input", { type: "number", min: 0, max: 20, ...bind(draft, "retries", "number") }),
          "0 = sofort alarmieren. 1 = erst beim zweiten Fehler in Folge (vermeidet Fehlalarme)")));
  }

  function channelSection() {
    return section("Benachrichtigungen", "Wohin sollen Alarme bei Statuswechseln gehen?",
      channels.length
        ? h("div", { class: "fields", style: "gap:8px" }, channels.map((ch) => h("label", { class: "check" },
          h("input", {
            type: "checkbox", checked: draft.channel_ids.includes(ch.id), onChange: (e) => {
              draft.channel_ids = e.target.checked ? [...draft.channel_ids, ch.id] : draft.channel_ids.filter((x) => x !== ch.id);
            },
          }),
          h("span", null, ch.name, " ", h("span", { class: "badge" }, CHANNEL_TYPES[ch.type].short), ch.enabled ? "" : " (deaktiviert)"))))
        : h("div", { class: "muted" }, "Noch keine Kanäle eingerichtet. ", h("a", { href: "#/channels" }, "Kanal anlegen"), " (Discord, Telegram, E-Mail, ntfy, Webhook, …)"),
      h("label", { class: "check", style: "margin-top:8px" }, h("input", { type: "checkbox", ...bindCheck(draft, "enabled") }), h("span", null, "Monitor ist aktiv")));
  }

  const submit = h("button", { type: "submit", class: "btn btn-primary" }, existing ? "Speichern" : "Monitor anlegen");

  function render() {
    fill(formEl, 
      typeSection(),
      generalSection(),
      draft.type === "http" ? httpSection() : null,
      draft.type === "dns" ? dnsSection() : null,
      draft.type === "push" ? pushSection() : null,
      scheduleSection(),
      channelSection(),
      h("div", { class: "row" }, submit, h("a", { class: "btn btn-ghost", href: existing ? `#/monitor/${id}` : "#/" }, "Abbrechen")));
  }

  formEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { ...draft, config: { ...draft.config } };
    if (!body.name.trim()) return toast("Bitte einen Namen angeben", true);
    if (body.type === "http") {
      try {
        body.config.headers = headersText.trim() ? JSON.parse(headersText) : {};
      } catch (e) {
        return toast("Header müssen gültiges JSON sein", true);
      }
    }
    if (body.type === "push") {
      body.config.thresholds = (body.config.thresholds || []).filter((t) => String(t.metric).trim() !== "" || t.value !== "");
    }
    submit.disabled = true;
    try {
      const saved = existing ? await api("PUT", `/api/monitors/${id}`, body) : await api("POST", "/api/monitors", body);
      toast(existing ? "Gespeichert" : "Monitor angelegt");
      navigate(`#/monitor/${saved.id}`);
    } catch (e) {
      reportError(e);
    } finally {
      submit.disabled = false;
    }
  });

  render();
  if (!existing) $("input[name=type]:checked", formEl).focus();
}

/* =====================================================================
 * Notification channels
 * ===================================================================== */

const CHANNEL_TYPES = {
  discord: { label: "Discord", short: "Discord", fields: [["url", "Webhook-URL", "url", true, "", "Servereinstellungen → Integrationen → Webhooks"]] },
  telegram: {
    label: "Telegram",
    short: "Telegram",
    fields: [
      ["bot_token", "Bot-Token", "password", true, "", "Von @BotFather"],
      ["chat_id", "Chat-ID", "text", true, "", "Deine User-ID oder die ID einer Gruppe"],
    ],
  },
  ntfy: {
    label: "ntfy (Push aufs Handy)",
    short: "ntfy",
    fields: [
      ["server", "Server", "url", false, "https://ntfy.sh", "Leer lassen für ntfy.sh oder eigene Instanz eintragen"],
      ["topic", "Topic", "text", true, "", "Frei wählbar – in der ntfy-App abonnieren. Schwer erratbar wählen!"],
      ["token", "Access-Token", "password", false, "", "Nur bei geschützten Topics"],
    ],
  },
  email: {
    label: "E-Mail (SMTP)",
    short: "E-Mail",
    fields: [
      ["host", "SMTP-Server", "text", true, "smtp.example.com"],
      ["port", "Port", "number", false, "587"],
      ["security", "Verschlüsselung", ["starttls", "ssl", "none"]],
      ["username", "Benutzer", "text", false],
      ["password", "Passwort", "password", false],
      ["sender", "Absender", "text", true, "monitoring@example.com"],
      ["recipients", "Empfänger", "text", true, "ich@example.com, team@example.com", "Mehrere mit Komma trennen"],
    ],
  },
  slack: { label: "Slack / Mattermost", short: "Slack", fields: [["url", "Incoming-Webhook-URL", "url", true]] },
  webhook: {
    label: "Webhook (JSON)",
    short: "Webhook",
    fields: [
      ["url", "URL", "url", true, "https://example.com/hook", "Erhält bei jedem Statuswechsel einen POST mit JSON"],
      ["headers", "Zusätzliche Header (JSON)", "json", false, '{"Authorization": "Bearer …"}'],
    ],
  },
};

const WEBHOOK_EXAMPLE = `{
  "event": "status_change",
  "title": "🔴 Webshop: STÖRUNG",
  "text": "HTTP 503 (erwartet 200-399)\\nZiel: https://shop.example.com",
  "status": "down",
  "previous_status": "up",
  "message": "HTTP 503 (erwartet 200-399)",
  "timestamp": 1767225600.0,
  "url": "https://monitor.example.com/#/monitor/3",
  "monitor": {"id": 3, "name": "Webshop", "type": "http", "target": "https://shop.example.com", "group": "Kunde A"}
}`;

async function viewChannels(main) {
  let channels = await api("GET", "/api/channels");
  let editing = null; // null | "new" | channel object

  const formHost = h("div");
  const listHost = h("div");
  main.append(
    h("div", { class: "page-head" },
      h("div", null, h("h1", null, "Benachrichtigungen"), h("div", { class: "sub" }, "Kanäle, über die bei Statuswechseln alarmiert wird. Pro Monitor auswählbar.")),
      h("button", { type: "button", class: "btn btn-primary", onclick: () => { editing = "new"; renderForm(); } }, "+ Kanal hinzufügen")),
    formHost, listHost,
    h("details", { class: "card table-view", style: "margin-top:16px" },
      h("summary", null, "Aufbau der Webhook-Nachricht (JSON)"),
      h("div", { class: "card-pad" }, codeBlock(WEBHOOK_EXAMPLE))));

  async function reload() {
    channels = await api("GET", "/api/channels");
    renderList();
  }

  function renderForm() {
    if (!editing) {
      fill(formHost);
      return;
    }
    const isNew = editing === "new";
    const draft = isNew
      ? { name: "", type: "discord", config: {}, enabled: true }
      : { name: editing.name, type: editing.type, config: { ...editing.config }, enabled: editing.enabled };
    if (!isNew && Array.isArray(draft.config.recipients)) draft.config.recipients = draft.config.recipients.join(", ");
    if (!isNew && draft.config.headers && typeof draft.config.headers === "object") draft.config.headers = JSON.stringify(draft.config.headers);

    const fieldsHost = h("div", { class: "fields" });
    const renderFields = () => {
      fill(fieldsHost, ...CHANNEL_TYPES[draft.type].fields.map(([key, label, kind, required, placeholder, help]) => {
        let input;
        if (Array.isArray(kind)) {
          input = h("select", { onChange: (e) => { draft.config[key] = e.target.value; } },
            kind.map((opt) => h("option", { value: opt, selected: (draft.config[key] || kind[0]) === opt }, opt)));
        } else if (kind === "json") {
          input = h("textarea", { placeholder, value: draft.config[key] || "", onInput: (e) => { draft.config[key] = e.target.value; } });
        } else {
          input = h("input", {
            type: kind === "password" ? "password" : kind === "number" ? "number" : "text",
            placeholder: placeholder || "", required: !!required, autocomplete: "off",
            value: draft.config[key] === undefined ? "" : String(draft.config[key]),
            onInput: (e) => { draft.config[key] = e.target.value; },
          });
        }
        return h("label", { class: "field" }, h("span", null, label, required ? "" : h("span", { class: "muted" }, " (optional)")), input,
          help ? h("div", { class: "help" }, help) : null);
      }));
    };
    renderFields();

    const submit = h("button", { type: "submit", class: "btn btn-primary" }, "Speichern");
    const form = h("form", { class: "card", style: "margin-bottom:16px" },
      h("div", { class: "card-head" }, h("h2", null, isNew ? "Neuer Kanal" : `„${editing.name}“ bearbeiten`)),
      h("div", { class: "card-pad fields" },
        h("div", { class: "fields-2" },
          h("label", { class: "field" }, h("span", null, "Name"), h("input", { type: "text", required: true, placeholder: "z. B. Handy, Team-Chat", value: draft.name, onInput: (e) => { draft.name = e.target.value; } })),
          h("label", { class: "field" }, h("span", null, "Typ"), h("select", {
            onChange: (e) => { draft.type = e.target.value; draft.config = {}; renderFields(); },
          }, Object.entries(CHANNEL_TYPES).map(([key, info]) => h("option", { value: key, selected: draft.type === key }, info.label))))),
        fieldsHost,
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: draft.enabled, onChange: (e) => { draft.enabled = e.target.checked; } }), h("span", null, "Aktiv")),
        h("div", { class: "row" }, submit, h("button", { type: "button", class: "btn btn-ghost", onclick: () => { editing = null; renderForm(); } }, "Abbrechen"))));

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = { ...draft, config: { ...draft.config } };
      if (typeof body.config.headers === "string") {
        try {
          body.config.headers = body.config.headers.trim() ? JSON.parse(body.config.headers) : undefined;
        } catch (e) {
          return toast("Header müssen gültiges JSON sein", true);
        }
      }
      if (body.config.port !== undefined && body.config.port !== "") body.config.port = Number(body.config.port);
      submit.disabled = true;
      try {
        if (isNew) await api("POST", "/api/channels", body);
        else await api("PUT", `/api/channels/${editing.id}`, body);
        toast("Kanal gespeichert – teste ihn gleich mit „Testen“");
        editing = null;
        renderForm();
        await reload();
      } catch (e) {
        reportError(e);
      } finally {
        submit.disabled = false;
      }
    });
    fill(formHost, form);
    $("input", form).focus();
  }

  function renderList() {
    if (!channels.length) {
      fill(listHost, h("div", { class: "card empty-state" },
        h("h2", null, "Noch keine Kanäle"),
        h("p", null, "Richte mindestens einen Kanal ein, damit du bei Störungen benachrichtigt wirst – z. B. ntfy oder Telegram fürs Handy, Discord/Slack fürs Team, E-Mail oder einen eigenen Webhook.")));
      return;
    }
    fill(listHost, h("div", { class: "card" }, channels.map((ch) => {
      const testButton = h("button", { type: "button", class: "btn btn-sm" }, "Testen");
      testButton.addEventListener("click", async () => {
        testButton.disabled = true;
        try {
          await api("POST", `/api/channels/${ch.id}/test`);
          toast(`Testnachricht an „${ch.name}“ gesendet`);
        } catch (e) {
          reportError(e);
        } finally {
          testButton.disabled = false;
          reload().catch(reportError);
        }
      });
      return h("div", { class: "list-item" },
        h("div", { class: "grow" },
          h("div", { class: "row" }, h("strong", null, ch.name), h("span", { class: "badge" }, CHANNEL_TYPES[ch.type].short), ch.enabled ? null : h("span", { class: "badge" }, "deaktiviert")),
          ch.last_error
            ? h("div", { class: "small", style: "color:var(--critical-text)" }, "✕ Letzter Versand fehlgeschlagen: ", ch.last_error)
            : h("div", { class: "small muted" }, ch.last_sent_at ? `Zuletzt gesendet ${fmtAgo(ch.last_sent_at)}` : "Noch nichts gesendet")),
        testButton,
        h("button", { type: "button", class: "btn btn-sm", onclick: () => { editing = ch; renderForm(); window.scrollTo(0, 0); } }, "Bearbeiten"),
        h("button", {
          type: "button", class: "btn btn-sm btn-danger", onclick: async () => {
            if (!confirm(`Kanal „${ch.name}“ löschen?`)) return;
            try {
              await api("DELETE", `/api/channels/${ch.id}`);
              await reload();
            } catch (e) {
              reportError(e);
            }
          },
        }, "Löschen"));
    })));
  }

  renderList();
}

/* =====================================================================
 * Settings & integration
 * ===================================================================== */

async function viewSettings(main) {
  const base = baseUrl();
  const keysHost = h("div");
  const newKeyHost = h("div");

  async function loadKeys() {
    const keys = await api("GET", "/api/keys");
    fill(keysHost, keys.length
      ? h("div", { class: "table-scroll" }, h("table", { class: "data" },
        h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "Schlüssel"), h("th", null, "Erstellt"), h("th", null, "Zuletzt benutzt"), h("th", null, ""))),
        h("tbody", null, keys.map((k) => h("tr", null,
          h("td", null, k.name),
          h("td", { class: "mono" }, k.prefix + "…"),
          h("td", null, fmtDateTime(k.created_at)),
          h("td", null, k.last_used_at ? fmtAgo(k.last_used_at) : "nie"),
          h("td", { class: "num" }, h("button", {
            type: "button", class: "btn btn-sm btn-danger", onclick: async () => {
              if (!confirm(`API-Key „${k.name}“ widerrufen?`)) return;
              try {
                await api("DELETE", `/api/keys/${k.id}`);
                await loadKeys();
              } catch (e) {
                reportError(e);
              }
            },
          }, "Widerrufen")))))))
      : h("div", { class: "muted" }, "Noch keine API-Keys."));
  }

  const keyName = h("input", { type: "text", placeholder: "Name, z. B. Grafana, Ansible", maxlength: 100, "aria-label": "Name des API-Keys" });
  const keyForm = h("form", { class: "row" }, h("div", { style: "flex:1;min-width:200px" }, keyName), h("button", { type: "submit", class: "btn" }, "Key erzeugen"));
  keyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!keyName.value.trim()) return toast("Bitte einen Namen angeben", true);
    try {
      const created = await api("POST", "/api/keys", { name: keyName.value.trim() });
      keyName.value = "";
      fill(newKeyHost, h("div", { class: "notice fields", style: "gap:8px" },
        h("strong", null, "Neuer API-Key – jetzt kopieren, er wird nur einmal angezeigt:"),
        inlineCode(created.key)));
      await loadKeys();
    } catch (e) {
      reportError(e);
    }
  });

  const pwForm = h("form", { class: "fields" },
    h("div", { class: "fields-3" },
      textField("Aktuelles Passwort", { name: "current", type: "password", autocomplete: "current-password", required: true }),
      textField("Neues Passwort", { name: "next", type: "password", autocomplete: "new-password", required: true, minlength: 8 }),
      textField("Wiederholen", { name: "next2", type: "password", autocomplete: "new-password", required: true })),
    h("div", null, h("button", { type: "submit", class: "btn" }, "Passwort ändern")));
  pwForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(pwForm);
    if (data.get("next") !== data.get("next2")) return toast("Die neuen Passwörter stimmen nicht überein", true);
    try {
      await api("POST", "/api/auth/password", { current_password: data.get("current"), new_password: data.get("next") });
      pwForm.reset();
      toast("Passwort geändert");
    } catch (e) {
      reportError(e);
    }
  });

  const card = (title, hint, ...content) => h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", null, h("h2", null, title), hint ? h("div", { class: "hint" }, hint) : null)),
    h("div", { class: "card-pad fields" }, content));

  main.append(
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Anbindung & Einstellungen"), h("div", { class: "sub" }, "Schnittstellen, API-Zugriff und Konto"))),
    card("Wie Systeme angebunden werden", null,
      h("div", { class: "how-grid", style: "margin:0" },
        h("div", { class: "card" }, h("strong", null, "Pull (HTTP, TCP, Ping, DNS)"),
          h("p", { class: "muted small" }, "Dieser Server prüft das Ziel selbst in festem Intervall. Nichts zu installieren – das Ziel muss nur von hier erreichbar sein.")),
        h("div", { class: "card" }, h("strong", null, "Push (Agent, Heartbeat, Metriken)"),
          h("p", { class: "muted small" }, "Das System ruft eine geheime URL auf – per curl, Agent-Skript, Home Assistant, Code. Braucht nur ausgehendes HTTPS, funktioniert also auch hinter NAT/Firewall.")),
        h("div", { class: "card" }, h("strong", null, "Weiterverarbeitung"),
          h("p", { class: "muted small" }, "REST-API zum Automatisieren (Ansible, Terraform, Skripte) und Prometheus-Endpunkt für Grafana.")))),
    card("REST-API", "Alles, was die Oberfläche kann, geht auch per API – authentifiziert mit einem API-Key.",
      h("div", null, h("div", { class: "field-label" }, "Interaktive Dokumentation (OpenAPI)"), h("a", { href: "/docs", target: "_blank", rel: "noopener" }, base + "/docs")),
      h("div", null, h("div", { class: "field-label" }, "Beispiel: alle Monitore abrufen"),
        codeBlock(`curl -H "Authorization: Bearer <API-KEY>" ${base}/api/monitors`)),
      h("div", null, h("div", { class: "field-label" }, "Beispiel: Monitor anlegen"),
        codeBlock(`curl -X POST ${base}/api/monitors \\\n  -H "Authorization: Bearer <API-KEY>" -H "Content-Type: application/json" \\\n  -d '{"name": "Webshop", "type": "http", "target": "https://shop.example.com", "interval": 60}'`))),
    card("Prometheus / Grafana", "Status, Antwortzeiten und alle Push-Metriken im Prometheus-Format.",
      codeBlock(`scrape_configs:\n  - job_name: monitoring\n    scheme: ${location.protocol.replace(":", "")}\n    metrics_path: /metrics\n    authorization:\n      credentials: <API-KEY>\n    static_configs:\n      - targets: ["${location.host}"]`)),
    card("API-Keys", "Für REST-API und Prometheus. Keys haben vollen Zugriff – nur an vertrauenswürdige Systeme geben.",
      keyForm, newKeyHost, keysHost),
    card("Konto", `Angemeldet als ${state.auth.user.name}`, pwForm),
    card("System", null,
      h("table", { class: "data" }, h("tbody", null,
        h("tr", null, h("th", null, "Version"), h("td", null, state.auth.version)),
        h("tr", null, h("th", null, "Benutzer-Datenbank"), h("td", null, state.auth.user_db)),
        h("tr", null, h("th", null, "Datenaufbewahrung"), h("td", null, `${state.auth.retention_days} Tage (MONITOR_RETENTION_DAYS)`)),
        h("tr", null, h("th", null, "Öffentliche URL"), h("td", null, state.auth.public_url || h("span", { class: "muted" }, "nicht gesetzt – MONITOR_PUBLIC_URL setzen, damit Links in Benachrichtigungen funktionieren"))),
      ))));

  await loadKeys();
}

/* =====================================================================
 * Boot
 * ===================================================================== */

window.addEventListener("hashchange", () => {
  if (state.auth) route();
});

(async function boot() {
  try {
    state.auth = await api("GET", "/api/auth/status");
  } catch (e) {
    fill($("#app"), h("div", { class: "auth-wrap" }, h("div", { class: "card auth-card" },
      h("h1", null, "Server nicht erreichbar"), h("p", { class: "muted" }, e.message))));
    return;
  }
  route();
})();
