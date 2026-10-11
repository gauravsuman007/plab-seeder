/* pornolab-seeder web UI: a thin, dependency-free client over /api/*.
 *
 * One poll loop keeps /api/state fresh; everything else (settings, events,
 * history, candidates) is fetched on demand -- when its tab is opened, or
 * after an action that would change it.
 */

const POLL_MS = 5000;
let lastState = null;
let loginCreds = null;     // {username, password, remember} kept only to resubmit after a captcha
let currentAccountId = parseInt(localStorage.getItem("accountId") || "1", 10);
let accountsCache = [];

// -------------------------------------------------------------------- utils
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  n = Number(n);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 2)} ${units[i]}`;
}

function fmtNum(n) {
  return n === null || n === undefined ? "—" : Number(n).toLocaleString();
}

function fmtWhen(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmtDuration(sec) {
  sec = Number(sec) || 0;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, kind === "err" ? 12000 : 4000);
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* no body */ }
  if (!r.ok) {
    const msg = (data && data.detail) || `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

// -------------------------------------------------------------------- tabs
function initTabs() {
  $$(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach(b => b.classList.remove("active"));
      $$(".panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#panel-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "settings") loadSettings();
      if (btn.dataset.tab === "events") loadEvents();
      if (btn.dataset.tab === "accounts") loadAccounts();
      if (btn.dataset.tab === "dashboard" && lastState?.logged_in) loadHistory();
    });
  });
}

// ----------------------------------------------------------- account switcher
async function loadAccountSwitcher() {
  accountsCache = await api("GET", "/api/accounts");
  const sel = $("#accountSwitcher");
  if (!accountsCache.some(a => a.id === currentAccountId)) {
    currentAccountId = accountsCache[0] ? accountsCache[0].id : 1;
  }
  sel.innerHTML = accountsCache.map(a =>
    `<option value="${a.id}">${escapeHtml(a.label)}${a.logged_in ? "" : " (logged out)"}</option>`
  ).join("") + `<option value="__new__">+ New account…</option>`;
  sel.value = String(currentAccountId);
}

function initAccountSwitcher() {
  $("#accountSwitcher").addEventListener("change", async (ev) => {
    if (ev.target.value === "__new__") {
      ev.target.value = String(currentAccountId);   // revert the select
      $$(".tab").forEach(b => b.classList.remove("active"));
      $$(".panel").forEach(p => p.classList.remove("active"));
      $('.tab[data-tab="register"]').classList.add("active");
      $("#panel-register").classList.add("active");
      return;
    }
    currentAccountId = parseInt(ev.target.value, 10);
    localStorage.setItem("accountId", String(currentAccountId));
    await refreshState();
    if ($(".tab.active")?.dataset.tab === "settings") loadSettings();
    if ($(".tab.active")?.dataset.tab === "events") loadEvents();
    if ($(".tab.active")?.dataset.tab === "dashboard") loadHistory();
  });
}

// ------------------------------------------------------------------ status
function renderPills(s) {
  const pills = [];
  pills.push(pill("PornoLab", s.logged_in, s.logged_in ? `as ${s.username}` : "logged out"));
  pills.push(pill("qBittorrent", s.qbit.ok, s.qbit.ok ? `v${s.qbit.version}` : (s.qbit.error || "down")));
  pills.push(pill("Gateway", s.gateway.ok,
    s.gateway.ok ? `${s.gateway.active ?? "?"}/${s.gateway.desired ?? "?"} tunnels` : (s.gateway.error || "down")));
  $("#statusPills").innerHTML = pills.join("");
  $("#autoToggle").checked = !!s.auto;
  $("#guardOverrideToggle").checked = !!s.guard_override;
  $("#fakeUploadToggle").checked = !!s.fake_upload_credit;
  if (s.fake_upload_mb_per_hour != null) $("#fakeUploadRate").value = s.fake_upload_mb_per_hour;
}

function pill(label, ok, detail) {
  return `<span class="pill ${ok ? "ok" : "bad"}"><i class="dot"></i>${label}: ${detail}</span>`;
}

// ----------------------------------------------------------------- profile
function renderProfile(s) {
  const loggedIn = s.logged_in && s.profile;
  $("#loginCard").hidden = loggedIn;
  $("#profileArea").hidden = !loggedIn;
  if (!loggedIn) return;

  const p = s.profile;
  $("#profileAt").textContent = `updated ${fmtAgo(p.at)}`;

  const rows = [
    ["Username", p.username ?? "—"],
    ["Rating", p.rating === null ? "—" : p.rating.toFixed(3)],
    ["Newbie tier", p.newbie ? "yes" : "no"],
    ["Downloaded (live)", fmtBytes(p.downloaded)],
    ["Credited upload (live)", fmtBytes(p.credited_upload)],
    ["— uploaded", fmtBytes(p.uploaded)],
    ["— on own releases", fmtBytes(p.uploaded_own)],
    ["— bonus", fmtBytes(p.uploaded_bonus)],
    ["Effective ratio", p.effective_ratio === null ? "—" : p.effective_ratio.toFixed(3)],
    ["Credited since seeder started", fmtBytes(p.credited_since_start)],
  ];
  $("#profileKv").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  // guard
  const g = s.guard, t = s.tier;
  if (g) {
    const guardRows = [
      ["Tier", t ? `${t.name}${t.daily === null ? " (unlimited)" : ` — ${t.daily}/day`}` : "—"],
      ["Mode", g.mode],
      ["Ratio", g.ratio.toFixed(3)],
      ["Committed", fmtBytes(g.committed)],
      ["Ceiling", fmtBytes(g.ceiling)],
      ["Headroom", fmtBytes(g.headroom)],
      ["Pending (in-flight)", fmtBytes(g.pending)],
      ["Overhead allowance", `${(g.overhead * 100).toFixed(0)}%`],
    ];
    $("#guardKv").innerHTML = guardRows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    const pct = g.ceiling ? Math.min(100, (g.committed / g.ceiling) * 100) : 0;
    const fill = $("#guardBarFill");
    fill.style.width = `${pct}%`;
    fill.className = "bar-fill" + (pct >= 95 ? " bad" : pct >= 75 ? " warn" : "");
    $("#guardReason").textContent = g.reason;
    if (t && t.note) $("#guardReason").textContent += ` — ${t.note}`;
  }

  // budget
  const b = s.budget;
  const budgetRows = [
    ["Budget", b.budget === null ? "unlimited" : `${b.budget}/day`],
    ["Used today", fmtNum(b.used)],
    ["Left today", b.left === null ? "—" : fmtNum(b.left)],
    ["Next slot", b.next_slot ? fmtWhen(b.next_slot) : "—"],
  ];
  if (b.refused_until) budgetRows.push(["Site refused until", fmtWhen(b.refused_until)]);
  $("#budgetKv").innerHTML = budgetRows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  const bpct = b.budget ? Math.min(100, (b.used / b.budget) * 100) : 0;
  const bfill = $("#budgetBarFill");
  bfill.style.width = `${bpct}%`;
  bfill.className = "bar-fill" + (bpct >= 100 ? "bad" : bpct >= 75 ? " warn" : "");

  // today/yesterday
  const statRows = [["", "Down", "Up", "Own", "Bonus"],
    ["Today", fmtBytes(p.today.down), fmtBytes(p.today.up), fmtBytes(p.today.own), fmtBytes(p.today.bonus)],
    ["Yesterday", fmtBytes(p.yesterday.down), fmtBytes(p.yesterday.up), fmtBytes(p.yesterday.own), fmtBytes(p.yesterday.bonus)]];
  $("#statsTable").innerHTML = statRows.map((r, i) =>
    `<tr>${r.map(c => `<t${i === 0 ? "h" : "d"} class="${i === 0 ? "" : "num"}">${c}</t${i === 0 ? "h" : "d"}>`).join("")}</tr>`
  ).join("");
}

// ------------------------------------------------------------------- login
function showCaptcha(image) {
  $("#captchaBlock").hidden = false;
  $("#captchaImg").src = image;
}

async function submitLogin(creds) {
  const msg = $("#loginMsg");
  msg.textContent = "Logging in…";
  msg.className = "form-msg";
  try {
    const res = await api("POST", "/api/pornolab/login", { ...creds, account_id: currentAccountId });
    if (res.ok) {
      msg.textContent = "Logged in.";
      msg.className = "form-msg ok";
      loginCreds = null;
      $("#captchaBlock").hidden = true;
      $("#loginForm").reset();
      await refreshState();
      loadHistory();
    } else if (res.captcha) {
      loginCreds = creds;
      showCaptcha(res.captcha);
      msg.textContent = "Enter the captcha code and submit again.";
      msg.className = "form-msg";
    } else {
      msg.textContent = res.error || "Login failed.";
      msg.className = "form-msg err";
    }
  } catch (e) {
    msg.textContent = e.message;
    msg.className = "form-msg err";
  }
}

function initLogin() {
  $("#loginForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const creds = loginCreds
      ? { ...loginCreds, captcha: f.get("captcha") || "" }
      : { username: f.get("username"), password: f.get("password"), remember: !!f.get("remember") };
    await submitLogin(creds);
  });

  $("#logoutBtn").addEventListener("click", async () => {
    if (!confirm("Log out of PornoLab? The stored password will be cleared.")) return;
    await api("POST", "/api/pornolab/logout", { account_id: currentAccountId });
    await refreshState();
  });

  $("#refreshProfileBtn").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    try {
      await api("POST", "/api/profile/refresh", { account_id: currentAccountId });
      await refreshState();
      loadHistory();
      toast("Profile refreshed", "ok");
    } catch (e) {
      toast(e.message, "err");
    } finally {
      ev.target.disabled = false;
    }
  });

  $("#autoToggle").addEventListener("change", async (ev) => {
    try {
      await api("POST", "/api/auto", { enabled: ev.target.checked, account_id: currentAccountId });
      toast(`Auto-seed ${ev.target.checked ? "enabled" : "disabled"}`, "ok");
      await refreshState();
    } catch (e) {
      ev.target.checked = !ev.target.checked;
      toast(e.message, "err");
    }
  });

  $("#fakeUploadToggle").addEventListener("change", async (ev) => {
    if (ev.target.checked && !confirm(
      "This makes the tracker believe you are uploading even when qBittorrent " +
      "isn't seeding. PornoLab may detect this and ban the account. It only " +
      "takes effect once this account has a real download on record (added " +
      "automatically if auto-seed is also on). Enable anyway?"
    )) {
      ev.target.checked = false;
      return;
    }
    try {
      await api("POST", `/api/accounts/${currentAccountId}/settings`, { fake_upload_credit: ev.target.checked });
      const msg = $("#fakeUploadMsg");
      msg.textContent = ev.target.checked ? "Enabled." : "Disabled.";
      msg.className = "form-msg " + (ev.target.checked ? "warn" : "ok");
      await refreshState();
    } catch (e) {
      ev.target.checked = !ev.target.checked;
      const msg = $("#fakeUploadMsg");
      msg.textContent = e.message;
      msg.className = "form-msg err";
    }
  });

  $("#fakeUploadSaveBtn").addEventListener("click", async () => {
    const rate = parseInt($("#fakeUploadRate").value, 10);
    const msg = $("#fakeUploadMsg");
    if (!rate || rate < 1) { msg.textContent = "Enter a valid rate."; msg.className = "form-msg err"; return; }
    try {
      await api("POST", `/api/accounts/${currentAccountId}/settings`, { fake_upload_mb_per_hour: rate });
      msg.textContent = "Rate saved.";
      msg.className = "form-msg ok";
      await refreshState();
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "form-msg err";
    }
  });

  $("#guardOverrideToggle").addEventListener("change", async (ev) => {
    if (ev.target.checked && !confirm(
      "This ignores the download guard entirely: the app will keep fetching " +
      "new .torrent files even though your ratio is already low. PornoLab " +
      "itself can block downloads or ban the account once rating drops too " +
      "far. Turn this on anyway?"
    )) {
      ev.target.checked = false;
      return;
    }
    try {
      await api("POST", "/api/guard-override", { enabled: ev.target.checked, account_id: currentAccountId });
      toast(`Download guard override ${ev.target.checked ? "enabled" : "disabled"}`, ev.target.checked ? "warn" : "ok");
      await refreshState();
    } catch (e) {
      ev.target.checked = !ev.target.checked;
      toast(e.message, "err");
    }
  });
}

// --------------------------------------------------------------- candidates
function statusBadge(c) {
  if (c.fits && c.auto_fits) return `<span class="badge good">ready</span>`;
  if (c.fits) return `<span class="badge warn" title="${c.auto_reason}">manual only</span>`;
  if (c.blocked) return `<span class="badge ${c.blocked.includes("guard") ? "warn" : ""}" title="${c.blocked}">${c.blocked}</span>`;
  return `<span class="badge">—</span>`;
}

function renderCandidates(s) {
  $("#scanAt").textContent = s.scan.at ? `scanned ${fmtAgo(s.scan.at)}` : "never scanned";
  $("#scanStats").textContent = s.scan.at
    ? `${s.scan.releases ?? 0} releases scanned · ${s.scan.candidates ?? 0} eligible · ${s.scan.blocked_by_guard ?? 0} blocked by the guard`
    : "Click \"Scan now\" to pull the tracker listing and rank candidates.";

  const tbody = $("#candidatesTable tbody");
  const list = s.candidates || [];
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty">No candidates yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = list.map(c => `
    <tr>
      <td>${escapeHtml(c.title)}</td>
      <td>${escapeHtml(c.forum || "—")}</td>
      <td class="num">${fmtBytes(c.size)}</td>
      <td class="num">${fmtNum(c.seeders)} / ${fmtNum(c.leechers)}</td>
      <td class="num">${fmtNum(c.grabs)}</td>
      <td class="num">${c.score.toFixed(3)}</td>
      <td class="num">${fmtBytes(c.cost)}</td>
      <td>${statusBadge(c)}</td>
      <td><button class="btn small" data-add="${c.topic_id}" ${c.fits ? "" : "disabled"}>Add</button></td>
    </tr>
  `).join("");

  $$("#candidatesTable [data-add]").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api("POST", `/api/candidates/${btn.dataset.add}/add`, { account_id: currentAccountId });
        toast("Added to qBittorrent", "ok");
        await refreshState();
      } catch (e) {
        toast(e.message, "err");
        btn.disabled = false;
      }
    });
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function initCandidates() {
  $("#scanBtn").addEventListener("click", async (ev) => {
    ev.target.disabled = true;
    ev.target.textContent = "Scanning…";
    try {
      await api("POST", "/api/scan", { account_id: currentAccountId });
      await refreshState();
      toast("Scan complete", "ok");
    } catch (e) {
      toast(e.message, "err");
    } finally {
      ev.target.disabled = false;
      ev.target.textContent = "Scan now";
    }
  });
}

// ----------------------------------------------------------------- torrents
function stateBadgeClass(state) {
  state = (state || "").toLowerCase();
  if (state.includes("error") || state.includes("missing")) return "bad";
  if (state.includes("stop") || state.includes("pause")) return "warn";
  if (state.includes("up") || state.includes("seed")) return "good";
  return "";
}

function renderTorrents(s) {
  $("#torrentsTotals").textContent =
    `${s.torrents.length} torrents · ${fmtBytes(s.seeding_bytes)} on disk · ${fmtBytes(s.qbit_uploaded_total)} uploaded`;

  const tbody = $("#torrentsTable tbody");
  if (!s.torrents.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty">Nothing in qBittorrent under this category.</td></tr>`;
    return;
  }
  tbody.innerHTML = s.torrents.map(t => {
    const pct = Math.round((t.progress || 0) * 100);
    const stopped = (t.state || "").toLowerCase().includes("stop") || (t.state || "").toLowerCase().includes("pause");
    return `
    <tr>
      <td title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</td>
      <td>
        <div class="progress-mini"><i style="width:${pct}%"></i></div>
        <span class="small muted">${pct}%</span>
      </td>
      <td><span class="badge ${stateBadgeClass(t.state)}">${escapeHtml(t.state || "—")}</span></td>
      <td class="num">${fmtBytes(t.dlspeed)}/s ↓ · ${fmtBytes(t.upspeed)}/s ↑</td>
      <td class="num">${fmtBytes(t.downloaded)} / ${fmtBytes(t.uploaded)}</td>
      <td class="num">${(t.ratio ?? 0).toFixed(2)}</td>
      <td class="num">${fmtNum(t.num_seeds)} / ${fmtNum(t.num_leechs)}</td>
      <td class="num">${fmtDuration(t.seeding_time)}</td>
      <td class="row-actions">
        <button class="btn small" data-act="${stopped ? "start" : "stop"}" data-hash="${t.hash}">${stopped ? "Start" : "Stop"}</button>
        <button class="btn small danger" data-act="remove" data-hash="${t.hash}">Remove</button>
      </td>
    </tr>`;
  }).join("");

  $$("#torrentsTable [data-act]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const { act, hash } = btn.dataset;
      let body = { account_id: currentAccountId };
      if (act === "remove") {
        if (!confirm("Remove this torrent? Downloaded files will be deleted too.")) return;
        body.delete_files = true;
      }
      btn.disabled = true;
      try {
        await api("POST", `/api/torrents/${hash}/${act}`, body);
        await refreshState();
      } catch (e) {
        toast(e.message, "err");
      } finally {
        btn.disabled = false;
      }
    });
  });
}

// ----------------------------------------------------------------- settings
// Two physically separate forms now (global infra vs. this account's own
// rules), matching app/store.py's GlobalSettings/AccountSettings split —
// no more guessing which endpoint a field belongs to.
const LIST_FIELDS = new Set(["exclude_forums"]);
const SECRET_FIELDS = new Set(["qbit_password", "pornolab_password", "sms_activate_api_key"]);

function fillForm(form, s) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in s)) continue;
    if (SECRET_FIELDS.has(el.name)) { el.value = ""; continue; }  // never prefill secrets
    if (el.type === "checkbox") el.checked = !!s[el.name];
    else if (LIST_FIELDS.has(el.name)) el.value = (s[el.name] || []).join(" ");
    else el.value = s[el.name];
  }
}

function formPayload(form) {
  const payload = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") payload[el.name] = el.checked;
    else if (SECRET_FIELDS.has(el.name) && el.value === "") continue;  // blank = unchanged
    else payload[el.name] = el.value;
  }
  return payload;
}

async function loadSettings() {
  const label = accountsCache.find(a => a.id === currentAccountId)?.label;
  $("#settingsAccountLabel").textContent = label ? `— ${label}` : "";
  const [g, a] = await Promise.all([
    api("GET", "/api/settings"),
    api("GET", `/api/accounts/${currentAccountId}/settings`),
  ]);
  fillForm($("#globalSettingsForm"), g);
  fillForm($("#accountSettingsForm"), a);
}

function initSettings() {
  $("#globalSettingsForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("#globalSettingsMsg");
    try {
      await api("POST", "/api/settings", formPayload(ev.target));
      msg.textContent = "Saved.";
      msg.className = "form-msg ok";
      await loadSettings();
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "form-msg err";
    }
  });

  $("#accountSettingsForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("#accountSettingsMsg");
    try {
      await api("POST", `/api/accounts/${currentAccountId}/settings`, formPayload(ev.target));
      msg.textContent = "Saved.";
      msg.className = "form-msg ok";
      await loadSettings();
      await refreshState();
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "form-msg err";
    }
  });
}

// ------------------------------------------------------------------- events
async function loadEvents() {
  const events = await api("GET", `/api/events?limit=300&account_id=${currentAccountId}`);
  const list = $("#eventsList");
  if (!events.length) {
    list.innerHTML = `<li class="empty">No events yet.</li>`;
    return;
  }
  list.innerHTML = events.map(e => `
    <li class="${e.level}">
      <span class="lvl"></span>
      <time>${fmtWhen(e.at)}</time>
      <span>${escapeHtml(e.message)}</span>
    </li>
  `).join("");
}

function initEvents() {
  $("#refreshEventsBtn").addEventListener("click", loadEvents);
}

// ------------------------------------------------------------------- history
async function loadHistory() {
  const days = Number($("#historyRange").value);
  const points = await api("GET", `/api/history?days=${days}&account_id=${currentAccountId}`);
  drawHistory(points);
}

function drawHistory(points) {
  const svg = $("#historyChart");
  const W = 640, H = 220, PAD = 28;
  if (!points.length) {
    svg.innerHTML = `<text x="${W/2}" y="${H/2}" fill="var(--muted)" font-size="12" text-anchor="middle">No history yet</text>`;
    return;
  }
  const xs = points.map(p => p.at);
  const minX = Math.min(...xs), maxX = Math.max(...xs) || minX + 1;
  const series = {
    rating: points.map(p => p.rating ?? 0),
    downloaded: points.map(p => p.downloaded ?? 0),
    credited_upload: points.map(p => p.credited_upload ?? 0),
  };
  const colors = { rating: "var(--accent)", downloaded: "var(--info)", credited_upload: "var(--good)" };

  function path(key, scaleMax) {
    const ys = series[key];
    const max = scaleMax || Math.max(...ys, 1);
    return ys.map((y, i) => {
      const x = PAD + (W - 2 * PAD) * ((xs[i] - minX) / Math.max(1, maxX - minX));
      const yy = H - PAD - (H - 2 * PAD) * (y / max);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${yy.toFixed(1)}`;
    }).join(" ");
  }

  const byteMax = Math.max(...series.downloaded, ...series.credited_upload, 1);
  const ratingMax = Math.max(...series.rating, 1);

  svg.innerHTML = `
    <line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="var(--border)" />
    <line x1="${PAD}" y1="${PAD}" x2="${PAD}" y2="${H - PAD}" stroke="var(--border)" />
    <path d="${path("downloaded", byteMax)}" fill="none" stroke="${colors.downloaded}" stroke-width="2" />
    <path d="${path("credited_upload", byteMax)}" fill="none" stroke="${colors.credited_upload}" stroke-width="2" />
    <path d="${path("rating", ratingMax)}" fill="none" stroke="${colors.rating}" stroke-width="2" />
  `;
}

function initHistory() {
  $("#historyRange").addEventListener("change", loadHistory);
}

// --------------------------------------------------------------- registration
const REG_NAMES = [
  "alexey","ivan","sergey","dmitry","nikolay","pavel","mikhail","andrey","maxim",
  "roman","artem","kirill","vitaly","evgeny","igor","oleg","denis","vladislav",
  "anna","natasha","elena","olga","katya","marina","svetlana","irina","alina",
  "daria","oksana","tatyana","vitalik","sanya","kostya","dima","nikita","ruslan",
];
const REG_SUFFIXES = ["_pro","_ru","_online","_net","_top","_vip","_ok","_kz","_ua"];

function regRandomUsername() {
  const name = REG_NAMES[Math.floor(Math.random() * REG_NAMES.length)];
  const r = Math.random();
  if (r < 0.4) {
    // name + birth year 1978-2003
    return name + (1978 + Math.floor(Math.random() * 26));
  } else if (r < 0.65) {
    // name + 2-digit number
    return name + String(Math.floor(Math.random() * 90) + 10);
  } else if (r < 0.8) {
    // name + suffix
    return name + REG_SUFFIXES[Math.floor(Math.random() * REG_SUFFIXES.length)];
  } else {
    // name + _ + another name fragment
    const name2 = REG_NAMES[Math.floor(Math.random() * REG_NAMES.length)];
    return name + "_" + name2.slice(0, 3 + Math.floor(Math.random() * 3));
  }
}

function regRandomPassword() {
  // 16-char password: letters + digits + symbols, max 20 chars (site limit)
  const upper = "ABCDEFGHJKLMNPQRSTUVWXYZ";
  const lower = "abcdefghjkmnpqrstuvwxyz";
  const digits = "23456789";
  const syms = "!@#$%&*";
  const pool = upper + lower + digits + syms;
  const arr = new Uint8Array(16);
  crypto.getRandomValues(arr);
  // guarantee at least one of each class
  let pw = [
    upper[arr[0] % upper.length],
    lower[arr[1] % lower.length],
    digits[arr[2] % digits.length],
    syms[arr[3] % syms.length],
    ...Array.from(arr.slice(4), b => pool[b % pool.length]),
  ];
  // Fisher-Yates shuffle
  for (let i = pw.length - 1; i > 0; i--) {
    const j = arr[i % arr.length] % (i + 1);
    [pw[i], pw[j]] = [pw[j], pw[i]];
  }
  return pw.join("");
}

function initRegister() {
  const getCaptchaBtn = $("#regGetCaptchaBtn");
  const captchaBlock = $("#regCaptchaBlock");
  const submitRow = $("#regSubmitRow");
  const captchaImg = $("#regCaptchaImg");
  const msg = $("#regMsg");
  const turnstileNote = $("#regTurnstileNote");

  $("#regRandomBtn").addEventListener("click", () => {
    const form = $("#registerForm");
    form.elements["username"].value = regRandomUsername();
    form.elements["password"].value = regRandomPassword();
  });

  getCaptchaBtn.addEventListener("click", async () => {
    getCaptchaBtn.disabled = true;
    getCaptchaBtn.textContent = "Loading…";
    msg.textContent = "";
    try {
      const res = await api("GET", "/api/register/form");

      const countrySelect = $("#regCountry");
      countrySelect.innerHTML = res.countries.map(
        ([v, t]) => `<option value="${escapeHtml(v)}">${escapeHtml(t)}</option>`
      ).join("");
      const ru = countrySelect.querySelector('option[value="181"]');
      if (ru) ru.selected = true;

      const tzSelect = $("#regTimezone");
      tzSelect.innerHTML = res.timezones.map(
        ([v, t]) => `<option value="${escapeHtml(v)}">${escapeHtml(t)}</option>`
      ).join("");
      const msk = tzSelect.querySelector('option[value="6"]');
      if (msk) msk.selected = true;

      if (res.turnstile_solved) {
        turnstileNote.textContent = "✓ Cloudflare bot-check solved.";
        turnstileNote.className = "muted small ok";
      } else {
        turnstileNote.innerHTML =
          'Cloudflare bot-check not solved. If registration fails, ' +
          '<a href="https://pornolab.net/forum/profile.php?mode=register" target="_blank">register directly on the site</a>.';
        turnstileNote.className = "muted small";
      }

      if (res.captcha) {
        captchaImg.src = res.captcha;
        captchaBlock.hidden = false;
        submitRow.hidden = false;
        const captchaInput = captchaBlock.querySelector('input[name="captcha"]');
        if (res.captcha_text) {
          captchaInput.value = res.captcha_text;
          captchaInput.style.color = "var(--muted)";
          captchaInput.title = "Auto-solved by OCR — edit if incorrect";
        } else {
          captchaInput.value = "";
          captchaInput.style.color = "";
          captchaInput.focus();
        }
      } else {
        msg.textContent = "Captcha image could not be loaded. Try again.";
        msg.className = "form-msg err";
      }
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "form-msg err";
    } finally {
      getCaptchaBtn.disabled = false;
      getCaptchaBtn.textContent = "Refresh captcha";
    }
  });

  $("#registerForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const payload = {
      username: f.get("username"),
      password: f.get("password"),
      email: f.get("email"),
      captcha: f.get("captcha"),
      country: f.get("country"),
      timezone: f.get("timezone"),
    };
    msg.textContent = "Submitting…";
    msg.className = "form-msg";
    try {
      const res = await api("POST", "/api/register", payload);
      if (res.ok) {
        msg.textContent = "Registration submitted! Check your email for an activation link.";
        msg.className = "form-msg ok";
        captchaBlock.hidden = true;
        submitRow.hidden = false;
        ev.target.reset();
      } else {
        msg.textContent = res.error || "Registration failed.";
        msg.className = "form-msg err";
        getCaptchaBtn.click();   // auto-refresh captcha on failure
      }
    } catch (e) {
      msg.textContent = e.message;
      msg.className = "form-msg err";
    }
  });
}


// ------------------------------------------------------------- full automation
function initFullAuto() {
  const btn = $("#fullAutoBtn");
  const statusEl = $("#fullAutoStatus");

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    statusEl.hidden = false;
    statusEl.textContent = "Creating email, registering PornoLab account, solving captcha " +
      "(retrying up to 10×)… this can take a couple of minutes.";
    try {
      const res = await api("POST", "/api/register/auto", {});
      statusEl.textContent = res.activated
        ? `✓ Registered and activated: ${res.username} (${res.email})`
        : `✓ Registered: ${res.username} (${res.email}) — activation pending, ` +
          `check the Accounts tab to view the inbox or finish by hand.`;
      currentAccountId = res.account_id;
      localStorage.setItem("accountId", String(currentAccountId));
      await loadAccountSwitcher();
      await refreshState();
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

// ---------------------------------------------- atomicmail.io (manual captcha)
function initAtomicmail() {
  const startBtn = $("#atomicmailStartBtn");
  const verifyBtn = $("#atomicmailVerifyBtn");
  const cancelBtn = $("#atomicmailCancelBtn");
  const promptEl = $("#atomicmailPrompt");
  const imageWrap = $("#atomicmailImageWrap");
  const img = $("#atomicmailImage");
  const statusEl = $("#atomicmailStatus");

  let sessionId = null;
  const clicks = [];

  function reset() {
    sessionId = null;
    clicks.length = 0;
    startBtn.hidden = false;
    verifyBtn.hidden = true;
    cancelBtn.hidden = true;
    promptEl.hidden = true;
    imageWrap.hidden = true;
  }

  function showChallenge(data) {
    sessionId = data.session_id || sessionId;
    clicks.length = 0;
    img.src = "data:image/png;base64," + data.image;
    promptEl.textContent = data.prompt || "Click the matching tile(s), then Verify.";
    promptEl.hidden = false;
    imageWrap.hidden = false;
    startBtn.hidden = true;
    verifyBtn.hidden = false;
    cancelBtn.hidden = false;
  }

  // Use pointerup rather than click -- on some mobile browsers (seen on
  // Firefox/Android) a native image long-press/drag gesture can swallow the
  // synthetic click event entirely, so no tap on the challenge ever lands.
  img.addEventListener("contextmenu", (ev) => ev.preventDefault());
  img.addEventListener("pointerup", async (ev) => {
    if (!sessionId) return;
    ev.preventDefault();
    const rect = img.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width;
    const y = (ev.clientY - rect.top) / rect.height;
    clicks.push([x, y]);
    statusEl.hidden = false;
    statusEl.textContent = `${clicks.length} click(s) queued — click more tiles or hit Verify.`;
    try {
      await api("POST", "/api/atomicmail/signup/click", { session_id: sessionId, x, y });
    } catch (e) {
      statusEl.textContent = `Error registering click: ${e.message}`;
    }
  });

  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    statusEl.hidden = false;
    statusEl.textContent = "Starting signup, filling the form…";
    try {
      const res = await api("POST", "/api/atomicmail/signup/start", {});
      if (res.solved_without_challenge) {
        statusEl.textContent = res.ok
          ? `✓ No captcha needed this time — account created: ${res.email}`
          : `Signup ended unexpectedly: ${res.error || "unknown state"}`;
        reset();
      } else {
        showChallenge(res);
        statusEl.textContent = `Account in progress: ${res.email} — solve the captcha above.`;
      }
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
      startBtn.hidden = false;
    } finally {
      startBtn.disabled = false;
    }
  });

  verifyBtn.addEventListener("click", async () => {
    if (!sessionId) return;
    verifyBtn.disabled = true;
    statusEl.hidden = false;
    statusEl.textContent = "Submitting…";
    try {
      const res = await api("POST", "/api/atomicmail/signup/verify", { session_id: sessionId });
      if (res.solved || (res.ok && res.account_id)) {
        statusEl.textContent = res.activated
          ? `✓ Registered and activated: ${res.username} (${res.email})`
          : `✓ Registered: ${res.username} (${res.email}) — activation pending.`;
        currentAccountId = res.account_id;
        localStorage.setItem("accountId", String(currentAccountId));
        await loadAccountSwitcher();
        await refreshState();
        reset();
      } else if (res.image) {
        showChallenge(res);
        statusEl.textContent = "Another round — solve the new image and Verify again.";
      } else if (res.done) {
        statusEl.textContent = `Failed: ${res.error || "giving up"}.`;
        reset();
      } else {
        statusEl.textContent = res.error || "Not solved yet — try again.";
      }
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    } finally {
      verifyBtn.disabled = false;
    }
  });

  cancelBtn.addEventListener("click", async () => {
    if (sessionId) {
      try { await api("POST", "/api/atomicmail/signup/cancel", { session_id: sessionId }); } catch (e) { /* ignore */ }
    }
    statusEl.hidden = false;
    statusEl.textContent = "Cancelled.";
    reset();
  });
}

// ----------------------------------------------------------------- accounts
async function loadAccounts() {
  const accounts = await api("GET", "/api/accounts");
  accountsCache = accounts;
  const tbody = $("#accountsTable tbody");
  if (!accounts.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No accounts yet — use Register → Full automation.</td></tr>`;
    return;
  }
  tbody.innerHTML = accounts.map(a => `
    <tr data-account-row="${a.id}">
      <td>${escapeHtml(a.label)}</td>
      <td>${escapeHtml(a.pornolab_username || "—")}</td>
      <td class="pw-cell" data-field="pornolab_password">${a.pornolab_username ? "••••••••" : "—"}</td>
      <td>${escapeHtml(a.email_address || "—")}</td>
      <td class="pw-cell" data-field="email_password">${a.email_address ? "••••••••" : "—"}</td>
      <td>${a.logged_in ? `<span class="badge good">logged in</span>` : `<span class="badge">logged out</span>`}
          ${a.first_download_at ? "" : `<span class="badge warn" title="No real download yet — fake-upload-credit stays off until one happens">no download yet</span>`}</td>
      <td class="row-actions">
        <button class="btn small" data-reveal="${a.id}">Show</button>
        ${a.email_provider === "tuta" ? `<button class="btn small" data-inbox="${a.id}">Check inbox</button>` : ""}
      </td>
    </tr>
  `).join("");

  $$("#accountsTable [data-reveal]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.reveal;
      const row = $(`tr[data-account-row="${id}"]`);
      try {
        const creds = await api("GET", `/api/accounts/${id}/credentials`);
        row.querySelector('[data-field="pornolab_password"]').textContent = creds.pornolab_password || "—";
        row.querySelector('[data-field="email_password"]').textContent = creds.email_password || "—";
        btn.textContent = "Shown";
        btn.disabled = true;
      } catch (e) {
        toast(e.message, "err");
      }
    });
  });

  $$("#accountsTable [data-inbox]").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Loading…";
      try {
        const res = await api("GET", `/api/email/inbox?account_id=${btn.dataset.inbox}`);
        const list = (res.messages || []).map(m =>
          `• ${escapeHtml(m.subject || "(no subject)")} — ${escapeHtml(m.from || "")}`
        ).join("\n") || "(inbox empty)";
        alert(list);
      } catch (e) {
        toast(e.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "Check inbox";
      }
    });
  });
}

function initAccounts() {
  $("#refreshAccountsBtn").addEventListener("click", loadAccounts);
}

// ----------------------------------------------------------- mail.ru factory
function initEmailFactory() {
  const btn      = $("#regEmailBtn");
  const statusEl = $("#regEmailStatus");
  const emailInput = $("#registerForm").elements["email"];
  const passInput  = $("#registerForm").elements["password"];

  if (!btn) return;

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    statusEl.hidden = false;
    statusEl.textContent = "Creating email account (browser automation, may take ~30–90s)…";
    try {
      const res = await api("POST", "/api/email/create", {});
      emailInput.value = res.email;
      passInput.value  = res.password;
      emailInput.dispatchEvent(new Event("input"));
      statusEl.textContent = `✓ Created ${res.email}`;
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

// --------------------------------------------------------------------- poll
let stateSeq = 0;
async function refreshState() {
  const seq = ++stateSeq;
  try {
    const s = await api("GET", `/api/state?account_id=${currentAccountId}`);
    if (seq !== stateSeq) return;   // a newer request already landed -- drop this stale one
    lastState = s;
    renderPills(s);
    renderProfile(s);
    renderCandidates(s);
    renderTorrents(s);
  } catch (e) {
    if (seq !== stateSeq) return;
    toast(`state: ${e.message}`, "err");
  }
}

function initPoll() {
  refreshState();
  setInterval(refreshState, POLL_MS);
}

// --------------------------------------------------------------------- init
document.addEventListener("DOMContentLoaded", async () => {
  initTabs();
  initAccountSwitcher();
  initLogin();
  initCandidates();
  initSettings();
  initEvents();
  initAccounts();
  initHistory();
  initRegister();
  initEmailFactory();
  initFullAuto();
  initAtomicmail();
  await loadAccountSwitcher();
  initPoll();
});
