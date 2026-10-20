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
let stateSeq = 0;   // guards refreshState() against a stale response landing after a newer one

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
      const s = await api("GET", "/api/settings").catch(() => ({}));
      if (s.register_url) window.open(s.register_url, "_blank", "noopener");
      else toast("Set plab-register URL in Settings first.", "err");
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
  if (b.refused_until) {
    budgetRows.push(["Site refused until", fmtWhen(b.refused_until)]);
    if (b.refused_reason) budgetRows.push(["Refusal reason", b.refused_reason]);
  }
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
const SECRET_FIELDS = new Set(["qbit_password", "pornolab_password"]);

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
  await showPublicAnnounceStatus();
}

async function showPublicAnnounceStatus() {
  const msg = $("#publicAnnounceMsg");
  try {
    const st = await api("GET", "/api/public-announce");
    if (st.error) { msg.textContent = st.error; msg.className = "form-msg err"; }
    else if (st.enabled) {
      msg.textContent = st.listening ? `Listening on port ${st.port}.` : "Starting…";
      msg.className = "form-msg " + (st.listening ? "ok" : "");
    } else { msg.textContent = "Disabled."; msg.className = "form-msg"; }
  } catch (e) { /* status is informational */ }
}

function initSettings() {
  $("#publicAnnounceTestBtn").addEventListener("click", async () => {
    const msg = $("#publicAnnounceMsg");
    msg.textContent = "Testing…"; msg.className = "form-msg";
    try {
      const r = await api("POST", "/api/public-announce/test");
      msg.textContent = r.detail;
      msg.className = "form-msg " + (r.ok ? "ok" : "err");
    } catch (e) { msg.textContent = e.message; msg.className = "form-msg err"; }
  });

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

// ----------------------------------------------------------------- tailscale
function renderTailscale(st) {
  const notConfigured = $("#tailscaleNotConfigured");
  const body = $("#tailscaleBody");
  const loggedOutEl = $("#tailscaleLoggedOut");
  const loggedInEl = $("#tailscaleLoggedIn");
  const select = $("#tailscaleExitNodeSelect");
  const badge = $("#egressBadge");

  if (!st.configured) {
    notConfigured.hidden = false;
    body.hidden = true;
    badge.textContent = "Direct";
    badge.title = "Network egress: direct (tailscale sidecar not deployed)";
    badge.classList.remove("good");
    return;
  }
  notConfigured.hidden = true;
  body.hidden = false;
  loggedOutEl.hidden = st.logged_in;
  loggedInEl.hidden = !st.logged_in;

  if (st.logged_in) {
    const nodes = st.exit_nodes || [];
    const key = nodes.map(n => n.id).join(",");
    if (select.dataset.nodesKey !== key) {
      select.innerHTML = `<option value="">Direct (no exit node)</option>` +
        nodes.map(n => `<option value="${escapeHtml(n.id)}">${escapeHtml(n.name || n.hostname)}${n.online ? "" : " (offline)"}</option>`).join("");
      select.dataset.nodesKey = key;
    }
    select.value = st.exit_node_id || "";
  }

  if (st.exit_node_id) {
    badge.textContent = `Exit: ${st.exit_node_name || st.exit_node_id}`;
    badge.title = `Network egress: through tailscale exit node ${st.exit_node_name || st.exit_node_id}`;
    badge.classList.add("good");
  } else {
    badge.textContent = "Direct";
    badge.title = st.logged_in
      ? "Network egress: direct (tailscale logged in, no exit node selected)"
      : "Network egress: direct (tailscale not logged in)";
    badge.classList.remove("good");
  }
}

async function refreshTailscale() {
  try {
    renderTailscale(await api("GET", "/api/tailscale/status"));
  } catch (e) { /* leave badge as-is; don't spam toasts for a background poll */ }
}

function initTailscale() {
  $("#tailscaleLoginBtn").addEventListener("click", async () => {
    const btn = $("#tailscaleLoginBtn");
    const urlEl = $("#tailscaleLoginUrl");
    const statusEl = $("#tailscaleStatus");
    btn.disabled = true;
    statusEl.hidden = false;
    statusEl.textContent = "Starting login…";
    try {
      const res = await api("POST", "/api/tailscale/login", {});
      if (res.already_logged_in) {
        statusEl.textContent = "Already logged in.";
        await refreshTailscale();
      } else {
        urlEl.hidden = false;
        urlEl.innerHTML = `Open this link to approve: <a href="${escapeHtml(res.url)}" target="_blank" rel="noopener">${escapeHtml(res.url)}</a>`;
        statusEl.textContent = "Waiting for you to approve in the browser — this updates automatically.";
        let tries = 0;
        const iv = setInterval(async () => {
          tries++;
          const st = await api("GET", "/api/tailscale/status").catch(() => null);
          if (st) renderTailscale(st);
          if ((st && st.logged_in) || tries > 40) {
            clearInterval(iv);
            if (st && st.logged_in) {
              statusEl.textContent = "✓ Logged in.";
              urlEl.hidden = true;
            }
          }
        }, 3000);
      }
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  $("#tailscaleLogoutBtn").addEventListener("click", async () => {
    if (!confirm("Log out of tailscale? Any active exit node will stop being used.")) return;
    try {
      await api("POST", "/api/tailscale/logout", {});
      await refreshTailscale();
    } catch (e) {
      toast(e.message, "err");
    }
  });

  $("#tailscaleExitNodeSelect").addEventListener("change", async (ev) => {
    const statusEl = $("#tailscaleStatus");
    statusEl.hidden = false;
    statusEl.textContent = "Switching exit node…";
    try {
      const st = await api("POST", "/api/tailscale/exit-node", { node_id: ev.target.value });
      renderTailscale(st);
      statusEl.textContent = st.exit_node_id ? `✓ Routing through ${st.exit_node_name}` : "✓ Back to direct.";
    } catch (e) {
      statusEl.textContent = `Error: ${e.message}`;
    }
  });

  refreshTailscale();
  setInterval(refreshTailscale, 20000);
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
async function loadAccounts() {
  const accounts = await api("GET", "/api/accounts");
  accountsCache = accounts;
  const tbody = $("#accountsTable tbody");
  if (!accounts.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No accounts yet — create one from plab-register.</td></tr>`;
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
}

function initAccounts() {
  $("#refreshAccountsBtn").addEventListener("click", loadAccounts);
  api("GET", "/api/settings").then(s => {
    if (s.register_url) {
      const link = $("#createAccountLink");
      link.href = s.register_url;
      link.hidden = false;
    }
  }).catch(() => {});
}
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
  initTailscale();
  initEvents();
  initAccounts();
  initHistory();
  await loadAccountSwitcher();
  initPoll();
});
