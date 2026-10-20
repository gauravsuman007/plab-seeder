"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* keep statusText */ }
    throw new Error(msg);
  }
  return r.json();
}

function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n >= 100 ? 0 : 1)) + " " + u[i];
}
const fmtNum = (n) => Number(n || 0).toLocaleString();
function fmtAgo(t) {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 129600) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
const fmtWhen = (t) => new Date(t * 1000).toLocaleString();

// ------------------------------------------------------------------- tabs
const loaders = { dashboard: loadDashboard, clients: loadClients, activity: loadActivity, settings: loadSettings };
for (const btn of document.querySelectorAll(".tab")) {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + btn.dataset.tab));
    loaders[btn.dataset.tab]();
  });
}
const activeTab = () => $(".tab.active").dataset.tab;

// -------------------------------------------------------------- dashboard
let overview = null;
let trend = null;
const SERIES_VARS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
// Colour follows the client (by creation order), never its rank in the current view.
const colorOf = (clientId) => `var(${SERIES_VARS[Math.max(0, clientIds().indexOf(clientId)) % SERIES_VARS.length]})`;
const clientIds = () => (overview ? overview.clients.map((c) => c.id).sort((a, b) => a - b) : []);

async function loadDashboard() {
  overview = await api("GET", "/api/overview");
  renderTiles();
  renderClientsTable();
  await loadTrend();
}

function renderTiles() {
  const t = overview.totals;
  const tiles = [
    [`${t.active_24h} / ${t.clients}`, "clients active (24h)"],
    [fmtNum(t.announces_24h), "announces (24h)"],
    [fmtNum(t.errors_24h), "errors (24h)"],
    [fmtBytes(t.fake_up_24h), "upload added (24h)"],
    [fmtBytes(t.hidden_down_24h), "downloads hidden (24h)"],
    [fmtNum(t.tokens), "announce tokens"],
  ];
  $("#tiles").innerHTML = tiles.map(([v, l]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join("");
}

function renderClientsTable() {
  const rows = overview.clients;
  if (!rows.length) {
    $("#clientsTable").innerHTML = `<tr><td class="empty">No clients yet. Add one on the Clients tab.</td></tr>`;
    return;
  }
  let html = `<thead><tr><th>Client</th><th>Last seen</th><th class="num">Announces 24h</th><th class="num">Errors</th>
    <th class="num">Real upload 24h</th><th class="num">Added 24h</th><th class="num">Downloads hidden 24h</th><th>Sources</th></tr></thead><tbody>`;
  for (const c of rows) {
    html += `<tr><td><span class="dot" style="background:${colorOf(c.id)}"></span><b>${esc(c.name)}</b>
      <span class="badge">${esc(c.kind)}</span>${c.enabled ? "" : ' <span class="badge off">disabled</span>'}</td>
      <td>${fmtAgo(c.last_seen)}</td><td class="num">${fmtNum(c.announces_24h)}</td><td class="num">${fmtNum(c.errors_24h)}</td>
      <td class="num">${fmtBytes(c.real_up_24h)}</td><td class="num">${fmtBytes(c.fake_up_24h)}</td>
      <td class="num">${fmtBytes(c.hidden_down_24h)}</td><td>${c.source_ips.map(esc).join(", ") || "—"}</td></tr>`;
    for (const i of c.identities) {
      html += `<tr class="sub"><td>&nbsp;&nbsp;&nbsp;↳ ${esc(i.label)}${i.ip ? ` <span class="muted">(${esc(i.ip)})</span>` : ""}</td>
        <td>${fmtAgo(i.last_seen)}</td><td class="num">${fmtNum(i.a24)}</td><td></td><td></td><td class="num">${fmtBytes(i.fu24)}</td><td></td>
        <td>${i.tokens} torrent${i.tokens === 1 ? "" : "s"} · fake upload ${i.fake_upload ? `on (${fmtNum(i.fake_mb_per_hour)} MB/h)` : "off"}</td></tr>`;
    }
  }
  $("#clientsTable").innerHTML = html + "</tbody>";
}

async function loadTrend() {
  trend = await api("GET", "/api/trend?range=" + $("#trendRange").value);
  renderTrend();
}
$("#trendRange").addEventListener("change", () => loadTrend());
$("#trendMetric").addEventListener("change", () => renderTrend());
$("#trendTableBtn").addEventListener("click", () => {
  const t = $("#trendTable");
  t.hidden = !t.hidden;
  $("#trendTableBtn").textContent = t.hidden ? "Table" : "Chart only";
});

const isBytes = (m) => m >= 3 && m !== 1 && m !== 2;      // field indexes: 3 real_up, 4 fake_up, 5 hidden_down
const fmtMetric = (m, v) => (isBytes(m) ? fmtBytes(v) : fmtNum(v));

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
function fmtTick(t, bucket) {
  const d = new Date(t * 1000);
  return bucket >= 86400 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function renderTrend() {
  if (!trend) return;
  const m = Number($("#trendMetric").value);
  const series = trend.series;
  const host = $("#trendChart");
  const n = series.length ? series[0].points.length : 0;
  if (!n || !series.some((s) => s.points.some((p) => p[m] > 0))) {
    host.innerHTML = `<div class="empty">No activity in this range yet.</div>`;
    $("#trendLegend").innerHTML = "";
    $("#trendTable").innerHTML = "";
    return;
  }
  const W = 720, H = 260, L = 56, R = 70, T = 12, B = 28;
  const max = niceMax(Math.max(...series.flatMap((s) => s.points.map((p) => p[m]))));
  const x = (i) => L + (n === 1 ? 0 : (i / (n - 1)) * (W - L - R));
  const y = (v) => T + (1 - v / max) * (H - T - B);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Trend by client">`;
  for (let k = 0; k <= 4; k++) {
    const v = (max * k) / 4, yy = y(v);
    svg += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/><text class="axis" x="${L - 6}" y="${yy + 4}" text-anchor="end">${esc(fmtMetric(m, v))}</text>`;
  }
  const ticks = Math.min(5, n);
  for (let k = 0; k < ticks; k++) {
    const i = Math.round((k * (n - 1)) / Math.max(1, ticks - 1));
    svg += `<text class="axis" x="${x(i)}" y="${H - 8}" text-anchor="${k === 0 ? "start" : k === ticks - 1 ? "end" : "middle"}">${esc(fmtTick(series[0].points[i][0], trend.bucket_s))}</text>`;
  }
  const direct = series.length <= 4;
  for (const s of series) {
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[m]).toFixed(1)}`).join(" ");
    svg += `<path class="line" d="${d}" stroke="${colorOf(s.client_id)}"/>`;
    if (direct) {
      const last = s.points[n - 1];
      svg += `<text class="endlabel" x="${x(n - 1) + 6}" y="${y(last[m]) + 4}">${esc(s.name)}</text>`;
    }
  }
  svg += `<line id="xhair" class="grid" y1="${T}" y2="${H - B}" x1="0" x2="0" style="display:none"/><rect id="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg>`;
  host.innerHTML = svg;

  $("#trendLegend").innerHTML = series.length >= 2
    ? series.map((s) => `<span><span class="dot" style="background:${colorOf(s.client_id)}"></span>${esc(s.name)}</span>`).join("") : "";

  const hit = $("#hit"), xhair = $("#xhair"), tip = $("#tip"), svgEl = host.querySelector("svg");
  hit.addEventListener("mousemove", (ev) => {
    const box = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1))));
    xhair.setAttribute("x1", x(i)); xhair.setAttribute("x2", x(i)); xhair.style.display = "";
    tip.innerHTML = `<b>${esc(fmtTick(series[0].points[i][0], trend.bucket_s))}</b>` + series.map((s) =>
      `<div><span class="dot" style="background:${colorOf(s.client_id)}"></span>${esc(s.name)}: ${esc(fmtMetric(m, s.points[i][m]))}</div>`).join("");
    tip.hidden = false;
    tip.style.left = Math.min(ev.clientX + 14, window.innerWidth - 200) + "px";
    tip.style.top = ev.clientY + 14 + "px";
  });
  hit.addEventListener("mouseleave", () => { tip.hidden = true; xhair.style.display = "none"; });

  let t = `<table class="table"><thead><tr><th>Time</th>${series.map((s) => `<th class="num">${esc(s.name)}</th>`).join("")}</tr></thead><tbody>`;
  for (let i = n - 1; i >= 0; i--) {
    t += `<tr><td>${esc(fmtTick(series[0].points[i][0], trend.bucket_s))}</td>${series.map((s) => `<td class="num">${esc(fmtMetric(m, s.points[i][m]))}</td>`).join("")}</tr>`;
  }
  $("#trendTable").innerHTML = t + "</tbody></table>";
}

// ---------------------------------------------------------------- clients
async function loadClients() {
  overview = await api("GET", "/api/overview");
  const info = await api("GET", "/api/info");
  const list = $("#clientList");
  if (!overview.clients.length) { list.innerHTML = `<div class="card empty">No clients yet.</div>`; return; }
  list.innerHTML = overview.clients.map((c) => `
    <div class="card client" data-id="${c.id}">
      <div class="head">
        <div><span class="dot" style="background:${colorOf(c.id)}"></span><b>${esc(c.name)}</b> <span class="badge">${esc(c.kind)}</span>
          ${c.enabled ? "" : '<span class="badge off">disabled</span>'}
          <span class="muted small"> · last seen ${fmtAgo(c.last_seen)} · ${fmtNum(c.announces_total)} announces · ${fmtBytes(c.fake_up_total)} added in total</span></div>
        <div class="controls">
          <button class="btn small" data-act="toggle">${c.enabled ? "Disable" : "Enable"}</button>
          <button class="btn small" data-act="rotate">New key</button>
          <button class="btn small danger" data-act="delete">Delete</button>
        </div>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="small muted">Announce URL given to this client
          <input data-field="announce_base" value="${esc(c.announce_base)}" placeholder="blank = the address the client used to reach this proxy" size="46"></label>
        <button class="btn small" data-act="saveBase">Save</button>
      </div>
      <div class="idents">${c.identities.length ? c.identities.map((i) => `
        <div class="ident" data-iid="${i.id}">
          <b>${esc(i.label)}</b><span class="muted small">${i.tokens} torrent${i.tokens === 1 ? "" : "s"}${i.ip ? " · " + esc(i.ip) : ""}${i.first_download_at ? "" : " · no download history yet"}</span>
          <label class="check"><input type="checkbox" data-field="fake_upload" ${i.fake_upload ? "checked" : ""}><span>Fake upload credit</span></label>
          <label class="small">MB/hour <input type="number" min="1" max="100000" data-field="fake_mb_per_hour" value="${i.fake_mb_per_hour}"></label>
          <button class="btn small" data-act="saveIdent">Save</button><span class="msg"></span>
        </div>`).join("") : '<span class="muted small">No accounts have registered yet.</span>'}</div>
      <div class="muted small" style="margin-top:8px">Clients on this network reach the announce port at <code>:${info.announce_port}</code>.</div>
    </div>`).join("");
}

$("#clientList").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".client"), id = card.dataset.id;
  const c = overview.clients.find((x) => String(x.id) === id);
  try {
    if (btn.dataset.act === "toggle") await api("PATCH", `/api/clients/${id}`, { enabled: !c.enabled });
    else if (btn.dataset.act === "delete") {
      if (!confirm(`Delete "${c.name}" and all its tokens and history? Torrents using it will stop announcing.`)) return;
      await api("DELETE", `/api/clients/${id}`);
    } else if (btn.dataset.act === "rotate") {
      if (!confirm("The current key stops working immediately. Continue?")) return;
      showKey(c.name, (await api("POST", `/api/clients/${id}/rotate-key`)).key);
    } else if (btn.dataset.act === "saveBase") {
      await api("PATCH", `/api/clients/${id}`, { announce_base: card.querySelector('[data-field="announce_base"]').value });
    } else if (btn.dataset.act === "saveIdent") {
      const row = btn.closest(".ident");
      await api("PATCH", `/api/identities/${row.dataset.iid}`, {
        fake_upload: row.querySelector('[data-field="fake_upload"]').checked,
        fake_mb_per_hour: Number(row.querySelector('[data-field="fake_mb_per_hour"]').value),
      });
      const m = row.querySelector(".msg"); m.textContent = "Saved."; setTimeout(() => (m.textContent = ""), 2000);
      return;
    }
    await loadClients();
  } catch (e) { alert(e.message); }
});

function showKey(name, key) {
  const n = $("#newKey");
  n.hidden = false;
  n.innerHTML = `Key for <b>${esc(name)}</b> — shown once, copy it now:<br><code>${esc(key)}</code>`;
}
$("#clientForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const r = await api("POST", "/api/clients", { name: f.get("name"), kind: f.get("kind") });
    ev.target.reset();
    showKey(r.name, r.key);
    await loadClients();
  } catch (e) { alert(e.message); }
});

// --------------------------------------------------------------- activity
async function loadActivity() {
  const rows = await api("GET", "/api/activity?limit=150");
  $("#activityTable").innerHTML = rows.length
    ? `<thead><tr><th>When</th><th>Client</th><th>Account</th><th>From</th><th>Event</th><th class="num">Client says up</th><th class="num">Reported up</th>
       <th class="num">Client says down</th><th class="num">Reported down</th><th>Tracker</th></tr></thead><tbody>${rows.map((r) => `
       <tr><td>${esc(fmtWhen(r.at))}</td><td>${esc(r.client)}</td><td>${esc(r.identity)}</td><td>${esc(r.remote_ip)}</td><td>${esc(r.event || "—")}</td>
       <td class="num">${fmtBytes(r.real_up)}</td><td class="num">${fmtBytes(r.rep_up)}</td><td class="num">${fmtBytes(r.real_down)}</td>
       <td class="num">${fmtBytes(r.rep_down)}</td><td>${r.status >= 200 && r.status < 300 ? "ok" : esc(r.status || "unreachable")}</td></tr>`).join("")}</tbody>`
    : `<tr><td class="empty">No announces yet.</td></tr>`;
}

// --------------------------------------------------------------- settings
async function loadSettings() {
  const [s, info] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/info")]);
  const f = $("#settingsForm");
  for (const el of f.elements) {
    if (!el.name || !(el.name in s)) continue;
    if (el.type === "checkbox") el.checked = !!s[el.name]; else el.value = s[el.name];
  }
  $("#portsInfo").innerHTML = `Announce listener: <b>${info.announce_port}</b> (point clients and your reverse proxy here — it serves only <code>/ann</code> and the key-gated <code>/v1/*</code> API).
    Management UI: <b>${info.ui_port}</b> (this page; keep it private). Set with <code>ANNOUNCE_PORT</code> / <code>UI_PORT</code> in docker-compose.`;
}
$("#settingsForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {};
  for (const el of ev.target.elements) if (el.name) body[el.name] = el.type === "checkbox" ? el.checked : el.value;
  try {
    await api("POST", "/api/settings", body);
    $("#settingsMsg").textContent = "Saved.";
    setTimeout(() => ($("#settingsMsg").textContent = ""), 2000);
  } catch (e) { alert(e.message); }
});

loadDashboard();
setInterval(() => { if (activeTab() === "dashboard") loadDashboard().catch(() => {}); }, 60000);
