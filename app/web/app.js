/* Renewal Risk Workbench — no build step, no framework. */
(() => {
  "use strict";

  const state = {
    token: localStorage.getItem("chp_token") || "",
    tenant: localStorage.getItem("chp_tenant") || "",
    me: null,
    tenants: [],
    band: "",
    owner: "",
    search: "",
    horizon: false,
    offset: 0,
    actionsTab: "approval",
    loading: false,
  };
  const PAGE = 25;

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (c) => {
    if (c === null || c === undefined) return "—";
    const n = Number(c) / 100;
    if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
    if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}k`;
    return `$${n.toFixed(0)}`;
  };
  const pct = (p, d = 0) => (p === null || p === undefined ? "—" : `${(Number(p) * 100).toFixed(d)}%`);
  const date = (s) => (s ? new Date(s).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "—");
  const shortDate = (s) => (s ? new Date(s).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "—");
  const dateTime = (s) => (s ? new Date(s).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—");
  const days = (d) => (d === null || d === undefined ? "—" : d < 0 ? "overdue" : `${d}d`);
  const band = (b) => `<span class="band ${esc(b)}"><i></i>${esc(b)}</span>`;
  const statusTag = (s) => `<span class="tag-status ${esc(s)}">${esc(s)}</span>`;
  const conf = (c) => `<span class="tag">${esc(c)} confidence</span>`;

  function toast(msg, ms = 3200) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), ms);
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) {
      logout();
      throw new Error("Session expired — choose a workspace again.");
    }
    let body = null;
    try { body = await res.json(); } catch (e) { /* empty */ }
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : `Request failed (${res.status})`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return body;
  }

  function spinner(label = "Working…") {
    $("#view").innerHTML = `<div class="loading"><span class="spinner" style="border-color:#d0d5dd;border-top-color:#0f766e"></span> ${esc(label)}</div>`;
  }

  function errorView(err) {
    $("#view").innerHTML = `<div class="error"><strong>Something went wrong.</strong><div style="margin-top:6px">${esc(err.message)}</div>
      <div style="margin-top:10px"><button class="btn small" onclick="location.reload()">Retry</button></div></div>`;
  }

  /* ------------------------------------------------------------------ login */
  async function showLogin() {
    $("#app").classList.add("hidden");
    $("#login").classList.remove("hidden");
    try {
      const data = await fetch("/api/demo/tenants").then((r) => r.json());
      state.tenants = data.tenants || [];
      $("#tenant-cards").innerHTML = state.tenants.map((t) => `
        <button class="tenant-card" data-key="${esc(t.api_key)}" data-slug="${esc(t.slug)}" data-name="${esc(t.name)}">
          <h3>${esc(t.name)}</h3>
          <div class="meta">${t.accounts.toLocaleString()} accounts · isolated tenant</div>
          <div class="meta mono" style="margin-top:8px">key …${esc(t.api_key.slice(-4))}</div>
        </button>`).join("");
      $$(".tenant-card").forEach((b) => b.addEventListener("click", () => {
        localStorage.setItem("chp_token", b.dataset.key);
        localStorage.setItem("chp_tenant", b.dataset.name);
        state.token = b.dataset.key;
        location.hash = "#/queue";
        boot();
      }));
    } catch (e) {
      $("#tenant-cards").innerHTML = `<div class="error">Cannot reach the API: ${esc(e.message)}</div>`;
    }
  }

  function logout() {
    localStorage.removeItem("chp_token");
    localStorage.removeItem("chp_tenant");
    state.token = "";
    showLogin();
  }

  async function boot() {
    if (!state.token) return showLogin();
    try {
      state.me = await api("/api/me");
    } catch (e) {
      return showLogin();
    }
    $("#login").classList.add("hidden");
    $("#app").classList.remove("hidden");
    $("#tenant-name").textContent = state.me.name;
    $("#as-of").textContent = state.me.latest_run ? `Scored ${dateTime(state.me.latest_run.as_of)}` : "No run yet";
    $("#kill-btn").classList.toggle("danger", !!state.me.kill_switch);
    $("#kill-btn").textContent = state.me.kill_switch ? "Kill switch ON" : "Kill switch";
    route();
  }

  /* ------------------------------------------------------------------ router */
  function setNav(name) {
    $$("#topnav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  }

  async function route() {
    if (!state.token) return showLogin();
    const hash = location.hash || "#/queue";
    const parts = hash.replace(/^#\//, "").split("/");
    try {
      if (parts[0] === "account" && parts[1]) return await renderAccount(decodeURIComponent(parts[1]));
      if (parts[0] === "actions") return await renderActions();
      if (parts[0] === "model") return await renderModel();
      if (parts[0] === "pipeline") return await renderPipeline();
      if (parts[0] === "runs") return await renderRuns(parts[1]);
      return await renderQueue();
    } catch (e) {
      if (e.message !== "Session expired — choose a workspace again.") errorView(e);
    }
  }

  window.addEventListener("hashchange", () => { state.offset = 0; route(); });

  /* ------------------------------------------------------------------ queue */
  async function renderQueue() {
    setNav("queue");
    spinner("Loading the Monday queue…");
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(state.offset) });
    if (state.band) params.set("band", state.band);
    if (state.owner) params.set("owner", state.owner);
    if (state.search) params.set("search", state.search);
    if (state.horizon) params.set("include_outside_horizon", "true");
    const [digest, queue, owners] = await Promise.all([
      api("/api/digest"),
      api(`/api/queue?${params}`),
      api("/api/owners"),
    ]);
    const c = queue.counts || {};
    const d = digest;
    $("#as-of").textContent = `Scored ${dateTime(queue.as_of)}`;
    $("#nav-actions").textContent = d.awaiting_approval || "";
    $("#nav-actions").classList.toggle("hot", (d.awaiting_approval || 0) > 0);

    const runStats = (d.run && d.run.stats) ? d.run.stats : {};
    const late = (runStats.ingest && runStats.ingest.held_back) || 0;
    const transitions = runStats.transitions || {};

    const rows = queue.rows.map((r) => `
      <tr class="click" data-account="${esc(r.account_id)}">
        <td>
          <div class="acct-name">${esc(r.name)}</div>
          <div class="acct-meta">${esc(r.segment)} · ${esc(r.owner_name)} · ${esc(r.industry || "—")}</div>
        </td>
        <td class="num">${money(r.arr_cents)}</td>
        <td class="num">${days(r.days_to_renewal)}<div class="acct-meta">${shortDate(r.renewal_date)}</div></td>
        <td>
          <div class="prob ${esc(r.band)}">${pct(r.prob, 1)} <span class="muted" style="font-weight:400">risk</span></div>
          <div class="acct-meta">${money(r.expected_arr_at_risk_cents)} expected at risk</div>
        </td>
        <td>${band(r.band)} ${conf(r.confidence)}</td>
        <td><div class="chips">${(r.reasons || []).slice().sort((a, b) => (a.imputed ? 1 : 0) - (b.imputed ? 1 : 0) || Math.abs(b.contribution) - Math.abs(a.contribution)).slice(0, 2).map((x) => `<span class="chip risk" title="${esc(x.imputed ? "no source record in the window; model imputed a value" : x.value_display)}">${esc(x.label)}${x.imputed ? " · no data" : ""}</span>`).join("") || '<span class="tag">—</span>'}</div></td>
        <td>
          <div class="cov"><div class="covbar ${r.coverage < 0.5 ? "low" : ""}"><span style="width:${Math.round(r.coverage * 100)}%"></span></div><span class="tag">${pct(r.coverage)}</span></div>
          ${r.in_horizon ? "" : '<div class="tag" style="color:#b54708">outside horizon</div>'}
        </td>
        <td>${r.action_play ? `${statusTag(r.action_status)}<div class="acct-meta">${esc(r.action_play)}</div>` : '<span class="tag">—</span>'}</td>
      </tr>`).join("");

    $("#view").innerHTML = `
      <div class="between" style="margin-bottom:16px">
        <div>
          <h1>Monday queue</h1>
          <div class="muted">Ranked by expected ARR at risk — probability × contract value. Every row is traceable to its evidence.</div>
        </div>
        <div class="row" style="gap:8px">
          <span class="tag">${esc(d.model ? d.model.id : "")}</span>
        </div>
      </div>

      ${late ? `<div class="banner info"><strong>${late.toLocaleString()} records are held back</strong> because they arrived after the ${dateTime(queue.as_of)} cut-off. They will be picked up by the next run; no score uses them yet.</div>` : ""}
      ${(transitions.band_change || 0) + (transitions.risk_jump || 0) ? `<div class="banner"><strong>${(transitions.band_change||0) + (transitions.risk_jump||0)} risk changes</strong> since the previous run (${transitions.band_change || 0} band changes, ${transitions.risk_jump || 0} material jumps). The agent has queued the next action.</div>` : ""}

      <div class="grid cards" style="grid-template-columns:repeat(5,1fr)">
        <div class="card stat"><div class="k">Expected ARR at risk</div><div class="v">${money(d.arr_at_risk_cents)}</div><div class="s">red + yellow bands</div></div>
        <div class="card stat"><div class="k">Red accounts</div><div class="v" style="color:var(--red)">${c.red ? c.red.accounts : 0}</div><div class="s">${money(c.red ? c.red.expected_arr_at_risk_cents : 0)} expected loss</div></div>
        <div class="card stat"><div class="k">Yellow accounts</div><div class="v" style="color:var(--amber)">${c.yellow ? c.yellow.accounts : 0}</div><div class="s">watch list</div></div>
        <div class="card stat"><div class="k">Awaiting approval</div><div class="v">${d.awaiting_approval || 0}</div><div class="s">drafts the agent wrote</div></div>
        <div class="card stat"><div class="k">In-horizon book</div><div class="v">${pct(d.coverage_pct, 0)}</div><div class="s">${(d.horizon_accounts || 0).toLocaleString()} of ${(d.active_accounts || 0).toLocaleString()} active · covered by data</div></div>
      </div>

      <div class="section-title"><h2>Prioritised accounts</h2><span class="muted">${queue.total.toLocaleString()} accounts in this view</span></div>

      <div class="filters">
        <div class="seg" id="band-tabs">
          <button data-band="" class="${state.band === "" ? "active" : ""}">All</button>
          <button data-band="red" class="${state.band === "red" ? "active" : ""}">Red</button>
          <button data-band="yellow" class="${state.band === "yellow" ? "active" : ""}">Yellow</button>
          <button data-band="green" class="${state.band === "green" ? "active" : ""}">Green</button>
        </div>
        <select id="owner-filter">
          <option value="">All CSMs</option>
          ${owners.rows.map((o) => `<option value="${esc(o.owner_email)}" ${state.owner === o.owner_email ? "selected" : ""}>${esc(o.owner_name)} (${o.n})</option>`).join("")}
        </select>
        <select id="segment-filter">
          <option value="">All segments</option>
          <option>Enterprise</option><option>Mid-Market</option><option>SMB</option>
        </select>
        <input type="search" id="acct-search" placeholder="Search account…" value="${esc(state.search)}"/>
        <label class="tag"><input type="checkbox" id="horizon-toggle" ${state.horizon ? "checked" : ""}/> include beyond 330 days</label>
      </div>

      <div class="card">
        <div class="bd tight">
          <table>
            <thead><tr><th>Account</th><th class="num">ARR</th><th class="num">Renewal</th><th>Risk</th><th>Band</th><th>Why</th><th>Data coverage</th><th>Next action</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="8" class="empty">No accounts match these filters.</td></tr>'}</tbody>
          </table>
        </div>
      </div>
      ${queue.total > PAGE ? `<div class="between" style="margin-top:12px"><span class="tag">Showing ${state.offset + 1}–${Math.min(state.offset + PAGE, queue.total)} of ${queue.total}</span>
        <div class="row"><button class="btn small" id="prev" ${state.offset === 0 ? "disabled" : ""}>Previous</button><button class="btn small" id="next" ${state.offset + PAGE >= queue.total ? "disabled" : ""}>Next</button></div></div>` : ""}
      <div class="footer-note">Ranking = calibrated P(churn at next renewal) × current ARR. Bands are calibrated thresholds (${pct(d.model?.metrics?.thresholds?.yellow || 0.16)} / ${pct(d.model?.metrics?.thresholds?.red || 0.35)}). Confidence reflects source coverage and recency, not the score itself.</div>
    `;

    $$("#band-tabs button").forEach((b) => b.addEventListener("click", () => { state.band = b.dataset.band; state.offset = 0; renderQueue(); }));
    $("#owner-filter").addEventListener("change", (e) => { state.owner = e.target.value; state.offset = 0; renderQueue(); });
    $("#segment-filter").addEventListener("change", (e) => { state.segment = e.target.value; state.offset = 0; renderQueue(); });
    $("#acct-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { state.search = e.target.value.trim(); state.offset = 0; renderQueue(); } });
    $("#horizon-toggle").addEventListener("change", (e) => { state.horizon = e.target.checked; state.offset = 0; renderQueue(); });
    $$("tr[data-account]").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/account/${tr.dataset.account}`; }));
    if ($("#prev")) $("#prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE); renderQueue(); });
    if ($("#next")) $("#next").addEventListener("click", () => { state.offset += PAGE; renderQueue(); });
  }

  /* ------------------------------------------------------------------ account */
  async function renderAccount(id) {
    setNav("queue");
    spinner("Loading account…");
    const d = await api(`/api/accounts/${encodeURIComponent(id)}`);
    const a = d.account, s = d.score;
    if (!s) {
      $("#view").innerHTML = `<div class="empty">This account has not been scored yet.</div>`;
      return;
    }
    const reasons = s.reasons || [];
    const feats = d.features || [];
    const maxAbs = Math.max(...feats.map((f) => Math.abs(f.contribution || 0)), 0.001);
    const top = feats.filter((f) => f.value !== null || f.imputed).slice(0, 16);

    const bar = (f) => {
      const w = Math.min(50, (Math.abs(f.contribution) / maxAbs) * 50);
      const pos = f.contribution >= 0;
      return `<div class="bar-row">
        <div title="${esc(f.label)}">${esc(f.label)}${f.imputed ? ' <span class="tag">(missing)</span>' : ""}</div>
        <div class="bar-track"><div class="zero"></div><div class="bar ${pos ? "pos" : "neg"}" style="${pos ? "left:50%" : `left:${50 - w}%`};width:${w}%"></div></div>
        <div class="bar-val">${pos ? "+" : ""}${(f.contribution || 0).toFixed(2)}</div>
      </div>`;
    };

    const reasonHtml = reasons.map((r) => `
      <div class="reason ${r.contribution >= 0 ? "up" : "down"}">
        <div class="top"><span class="label">${esc(r.label)}</span><span class="val">${esc(r.value_display)}</span></div>
        <div class="why">${esc(r.why || "")} · <strong>${esc(r.impact)}</strong> (${r.contribution > 0 ? "+" : ""}${r.contribution.toFixed(2)} log-odds)</div>
        <div class="evidence">
          ${(r.evidence || []).map((e) => `<div class="ev"><span class="src">${esc(e.source)}</span><span>${esc(e.summary)}</span>
            <span class="when">${shortDate(e.occurred_at)}</span><button data-raw="${esc(e.raw_event_id)}">trace</button></div>`).join("") || '<span class="tag">No source record attached (feature missing or aggregate only).</span>'}
        </div>
      </div>`).join("") || '<div class="empty">No material contributors.</div>';

    const contactRows = (d.contacts || []).map((c) => `<tr>
      <td><strong>${esc(c.name)}</strong><div class="acct-meta">${esc(c.title || "")}</div></td>
      <td>${esc(c.role)}</td><td class="mono">${esc(c.email)}</td>
      <td>${c.consent_email && !c.opted_out_at ? '<span class="chip ok">consented</span>' : '<span class="chip risk">no email consent</span>'}</td>
      <td>${c.active ? "active" : '<span class="tag">inactive</span>'}</td></tr>`).join("");

    const renewalRows = (d.renewals || []).slice(0, 6).map((r) => `<tr>
      <td>${date(r.renewal_date)}</td><td>${money(r.arr_before_cents)}</td><td>${r.arr_after_cents === null ? "—" : money(r.arr_after_cents)}</td>
      <td>${r.outcome === "churned" ? '<span class="band red"><i></i>churned</span>' : r.outcome === "downgraded" ? '<span class="band yellow"><i></i>downgraded</span>' : '<span class="band green"><i></i>renewed</span>'}</td>
      <td>${esc(r.churn_reason || "—")}</td></tr>`).join("");

    const actionHtml = (d.actions || []).map((x) => `<div class="reason" style="border-left:3px solid var(--accent)">
      <div class="top"><span class="label">${esc(x.play_name)}</span>${statusTag(x.status)}</div>
      <div class="why">${esc(x.trigger_reason)} · ${esc(x.channel)} · due ${date(x.due_at)}</div>
      ${x.draft_subject ? `<div class="tag">${esc(x.draft_subject)}</div>` : ""}
    </div>`).join("") || '<span class="tag">No actions yet.</span>';

    $("#view").innerHTML = `
      <div class="crumb"><a href="#/queue">← Monday queue</a> · ${esc(a.account_id)}</div>
      <div class="account-head">
        <div>
          <div class="name">${esc(a.name)}</div>
          <div class="meta">${esc(a.segment)} · ${esc(a.industry || "—")} · ${esc(a.region || "—")} · owned by ${esc(a.owner_name)}</div>
        </div>
        <div class="row" style="gap:8px;align-items:center">
          ${band(s.band)} ${conf(s.confidence)}
          <span class="tag">renewal ${date(a.renewal_date)}</span>
        </div>
      </div>

      <div class="grid two">
        <div class="card">
          <div class="bd">
            <div class="score-hero">
              <div>
                <div class="score-label">P(churn at next renewal)</div>
                <div class="score-big ${s.band}">${pct(s.prob, 1)}</div>
                <div class="meter"><div class="pin" style="left:${Math.min(99, s.prob * 100)}%"></div></div>
                <div class="tag" style="margin-top:6px">${esc(s.confidence)} confidence · ${pct(s.coverage)} source coverage</div>
              </div>
              <div style="flex:1"></div>
              <div class="kv">
                <div class="k">ARR</div><div>${money(a.arr_cents)}</div>
                <div class="k">Expected at risk</div><div><strong>${money(s.expected_arr_at_risk_cents)}</strong> <span class="tag">= ${pct(s.prob,1)} × ${money(a.arr_cents)}</span></div>
                <div class="k">Days to renewal</div><div>${days(s.days_to_renewal)}</div>
                <div class="k">Contract term</div><div>${a.term_months} months</div>
                <div class="k">Status</div><div>${esc(a.status)}</div>
              </div>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="hd"><h3>Why this score</h3><span class="tag">occlusion attribution</span></div>
          <div class="bd" style="max-height:320px;overflow:auto">${reasonHtml}</div>
        </div>
      </div>

      <div class="grid two" style="margin-top:14px">
        <div class="card">
          <div class="hd"><h3>Feature contributions</h3><span class="tag">reset to cohort median · log-odds</span></div>
          <div class="bd">
            <div class="bars">${top.map(bar).join("")}</div>
            <div class="legend" style="margin-top:10px"><span><i class="dot" style="background:#f97066"></i>increases risk</span><span><i class="dot" style="background:#32d583"></i>reduces risk</span></div>
            <div class="footer-note">Approximate additive attribution: log-odds(account) − log-odds(same account with this feature reset to the training median). Interactions mean the sum is not exact.</div>
          </div>
        </div>
        <div class="card">
          <div class="hd"><h3>Evidence timeline</h3><span class="tag">event time, as ingested</span></div>
          <div class="bd" id="timeline" style="max-height:520px;overflow:auto"><div class="loading" style="padding:20px">Loading…</div></div>
        </div>
      </div>

      <div class="section-title"><h2>People & consent</h2><span class="muted">only consented, active contacts are eligible for outreach</span></div>
      <div class="card"><div class="bd tight"><table><thead><tr><th>Contact</th><th>Role</th><th>Email</th><th>Consent</th><th>State</th></tr></thead><tbody>${contactRows}</tbody></table></div></div>

      <div class="grid two" style="margin-top:14px">
        <div class="card"><div class="hd"><h3>Renewal history</h3></div><div class="bd tight"><table><thead><tr><th>Date</th><th>ARR before</th><th>ARR after</th><th>Outcome</th><th>Reason</th></tr></thead><tbody>${renewalRows || '<tr><td colspan="5" class="empty">No settled renewals yet.</td></tr>'}</tbody></table></div></div>
        <div class="card"><div class="hd"><h3>Agent actions</h3><a href="#/actions" class="tag">all actions →</a></div><div class="bd">${actionHtml}</div></div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="hd"><h3>Trace</h3><span class="tag">everything above resolves to these identifiers</span></div>
        <div class="bd">
          <div class="kv">
            <div class="k">score id</div><div class="ref">${esc(s.id)}</div>
            <div class="k">run id</div><div class="ref">${esc(s.run_id)}</div>
            <div class="k">model version</div><div class="ref">${esc(s.model_version)}</div>
            <div class="k">dataset hash</div><div class="ref">${esc(d.model?.dataset_hash || "")}</div>
            <div class="k">feature hash</div><div class="ref">${esc(s.feature_hash)}</div>
            <div class="k">scored at</div><div>${dateTime(s.as_of)} <span class="tag">event-time cut-off</span></div>
            <div class="k">source coverage</div><div>${pct(s.coverage)} · ${Object.entries(d.source_counts || {}).map(([k, v]) => `${esc(k)} ${v}`).join(" · ")}</div>
          </div>
        </div>
      </div>
    `;

    $$("#view [data-raw]").forEach((b) => b.addEventListener("click", () => openRaw(b.dataset.raw)));
    loadTimeline(id);
  }

  async function loadTimeline(id) {
    try {
      const t = await api(`/api/accounts/${encodeURIComponent(id)}/timeline?limit=120`);
      $("#timeline").innerHTML = `<div class="timeline">${t.events.map((e) => `
        <div class="tl-item ${["payment_failed", "ticket"].includes(e.fact_type) ? "hot" : ""}">
          <div class="tl-date">${dateTime(e.occurred_at)}${e.arrived_at && e.arrived_at !== e.occurred_at ? ` · ingested ${shortDate(e.arrived_at)}` : ""}</div>
          <div class="tl-text"><span class="tl-src">${esc(e.source)}</span>${esc(e.summary)}</div>
        </div>`).join("") || '<div class="empty">No events.</div>'}</div>`;
    } catch (e) {
      $("#timeline").innerHTML = `<div class="tag">Could not load timeline: ${esc(e.message)}</div>`;
    }
  }

  async function openRaw(rawId) {
    const m = $("#modal");
    m.classList.remove("hidden");
    m.innerHTML = `<div class="modal-panel"><div class="between"><h3>Source record</h3><button class="btn small" id="close-modal">Close</button></div>
      <p class="muted tiny" style="margin-top:8px">Raw payload as landed, plus the canonical facts derived from it. This is the bottom of the lineage chain.</p>
      <pre class="json" id="raw-json">Loading…</pre></div>`;
    $("#close-modal").onclick = () => m.classList.add("hidden");
    m.onclick = (e) => { if (e.target === m) m.classList.add("hidden"); };
    try {
      const d = await api(`/api/lineage/raw/${encodeURIComponent(rawId)}`);
      $("#raw-json").textContent = JSON.stringify(d, null, 2);
    } catch (e) {
      $("#raw-json").textContent = e.message;
    }
  }

  /* ------------------------------------------------------------------ actions */
  async function renderActions() {
    setNav("actions");
    spinner("Loading actions…");
    const statusMap = { approval: "proposed", tasks: "proposed", sent: "sent", blocked: "blocked" };
    const channelMap = { approval: "email", tasks: "task" };
    let q = `/api/actions?status=${statusMap[state.actionsTab] || "proposed"}`;
    if (channelMap[state.actionsTab]) q += `&channel=${channelMap[state.actionsTab]}`;
    if (state.actionsTab === "all") q = "/api/actions";
    const d = await api(q);
    const c = d.counts || {};
    const cc = d.channel_counts || {};
    const rows = d.rows || [];

    $("#nav-actions").textContent = c.proposed ? "" : "";
    const cards = rows.map((x) => {
      const policy = (x.policy && x.policy.decision) || null;
      const trace = policy ? policy.trace || [] : [];
      return `<div class="action-card">
        <div class="action-head">
          <span class="acct"><a href="#/account/${esc(x.account_id)}">${esc(x.name)}</a></span>
          ${band(x.band)} <span class="tag">${pct(x.prob, 1)} risk · ${money(x.expected_arr_at_risk_cents)} at risk</span>
          <span class="spacer"></span>
          ${statusTag(x.status)}
          <span class="tag">${esc(x.play_name)}</span>
        </div>
        <div class="action-body">
          <div class="tag">Trigger: ${esc(x.trigger_reason)} · ${esc(x.channel)} · due ${date(x.due_at)} · ${esc(x.assigned_to)}</div>
          ${x.draft_body ? `<div class="draft">${x.draft_subject ? `<div class="subj">${esc(x.draft_subject)}</div>` : ""}${esc(x.draft_body)}</div>` : `<div class="draft">${esc(x.draft_body || "")}</div>`}
          ${x.blocked_reason ? `<div class="banner" style="margin:0 0 10px">Blocked: ${esc(x.blocked_reason)}</div>` : ""}
          <div class="row" style="gap:8px">
            ${x.status === "proposed" && x.channel === "email" ? `<button class="btn primary small" data-approve="${esc(x.id)}">Approve &amp; send</button><button class="btn small" data-reject="${esc(x.id)}">Reject</button>` : ""}
            ${x.status === "proposed" && x.channel === "task" ? `<button class="btn primary small" data-complete="${esc(x.id)}">Mark done</button>` : ""}
            ${x.status === "sent" ? `<span class="tag">sent ${dateTime(x.sent_at)}</span>` : ""}
            <button class="btn ghost small" data-detail="${esc(x.id)}">Decision trace</button>
          </div>
        </div>
      </div>`;
    }).join("");

    $("#view").innerHTML = `
      <div class="between" style="margin-bottom:16px">
        <div><h1>Action inbox</h1><div class="muted">The agent does the internal work. Customer email waits for a human unless policy explicitly allows a transactional send.</div></div>
        <div class="legend"><span><i class="dot" style="background:var(--amber)"></i>approval required</span><span><i class="dot" style="background:var(--green)"></i>sent</span></div>
      </div>
      <div class="seg" id="action-tabs" style="margin-bottom:14px">
        <button data-tab="approval" class="${state.actionsTab === "approval" ? "active" : ""}">Awaiting approval ${cc.email_proposed ? `(${cc.email_proposed})` : ""}</button>
        <button data-tab="tasks" class="${state.actionsTab === "tasks" ? "active" : ""}">Tasks ${cc.task_proposed ? `(${cc.task_proposed})` : ""}</button>
        <button data-tab="sent" class="${state.actionsTab === "sent" ? "active" : ""}">Sent ${c.sent ? `(${c.sent})` : ""}</button>
        <button data-tab="blocked" class="${state.actionsTab === "blocked" ? "active" : ""}">Blocked ${c.blocked ? `(${c.blocked})` : ""}</button>
        <button data-tab="all" class="${state.actionsTab === "all" ? "active" : ""}">All ${cc.total ? `(${cc.total})` : ""}</button>
      </div>
      <div class="banner info">Auto-send policy is evaluated at send time. Default is <strong>approval required</strong>; only allowlisted transactional plays can send unattended, and only under consent, frequency, ARR and escalation guardrails.</div>
      ${cards || '<div class="card"><div class="empty">Nothing in this tab.</div></div>'}
    `;

    $$("#action-tabs button").forEach((b) => b.addEventListener("click", () => { state.actionsTab = b.dataset.tab; renderActions(); }));
    $$("[data-approve]").forEach((b) => b.addEventListener("click", () => decide(b.dataset.approve, "approve", b)));
    $$("[data-reject]").forEach((b) => b.addEventListener("click", () => decide(b.dataset.reject, "reject", b)));
    $$("[data-complete]").forEach((b) => b.addEventListener("click", () => decide(b.dataset.complete, "complete", b)));
    $$("[data-detail]").forEach((b) => b.addEventListener("click", () => showActionDetail(b.dataset.detail)));
  }

  async function decide(id, action, btn) {
    btn.disabled = true;
    try {
      await api(`/api/actions/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify({}) });
      toast(`Action ${action === "approve" ? "approved and sent" : action === "reject" ? "rejected" : "completed"}`);
      renderActions();
    } catch (e) {
      toast(e.message);
      btn.disabled = false;
    }
  }

  async function showActionDetail(id) {
    const m = $("#modal");
    m.classList.remove("hidden");
    m.innerHTML = `<div class="modal-panel"><div class="between"><h3>Decision trace</h3><button class="btn small" id="close-modal">Close</button></div><div id="detail-body" class="loading">Loading…</div></div>`;
    $("#close-modal").onclick = () => m.classList.add("hidden");
    m.onclick = (e) => { if (e.target === m) m.classList.add("hidden"); };
    try {
      const d = await api(`/api/actions/${encodeURIComponent(id)}`);
      const trace = (d.action.policy && d.action.policy.decision && d.action.policy.decision.trace) || [];
      $("#detail-body").innerHTML = `
        <div class="kv" style="margin-bottom:14px">
          <div class="k">play</div><div>${esc(d.action.play_name)} (${esc(d.action.play_key)})</div>
          <div class="k">trigger</div><div>${esc(d.action.trigger_reason)}</div>
          <div class="k">idempotency key</div><div class="ref">${esc(d.action.idempotency_key)}</div>
          <div class="k">status</div><div>${statusTag(d.action.status)} ${esc(d.action.blocked_reason || "")}</div>
          <div class="k">score</div><div class="ref">${esc(d.action.score_id)}</div>
        </div>
        <h4 style="margin:12px 0 6px">Policy evaluation</h4>
        ${trace.map((r) => `<div class="rule"><span class="${r.ok ? "ok" : "no"}">${r.ok ? "✓" : "✕"}</span><span><strong>${esc(r.rule)}</strong> — ${esc(r.detail)}</span></div>`).join("") || '<span class="tag">No policy trace recorded.</span>'}
        <h4 style="margin:16px 0 6px">Reason codes carried from the score</h4>
        <div class="chips">${(d.action.reasons || []).map((r) => `<span class="chip ${r.contribution > 0 ? "risk" : "ok"}">${esc(r.label)}: ${esc(r.value_display)}</span>`).join("")}</div>
        <h4 style="margin:16px 0 6px">Event log</h4>
        ${d.events.map((e) => `<div class="rule"><span class="tag">${dateTime(e.at)}</span><span>${esc(e.actor)} · <strong>${esc(e.event)}</strong></span></div>`).join("")}
        ${d.outbox.length ? `<h4 style="margin:16px 0 6px">Outbox</h4>${d.outbox.map((o) => `<div class="rule"><span class="tag">${dateTime(o.sent_at)}</span><span>${esc(o.to_email)} · ${esc(o.provider_message_id)}</span></div>`).join("")}` : ""}
      `;
    } catch (e) {
      $("#detail-body").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    }
  }

  /* ------------------------------------------------------------------ model */
  function reliabilityChart(cal) {
    if (!cal || !cal.length) return '<div class="empty">No calibration data.</div>';
    const W = 380, H = 240, P = 34;
    const x = (v) => P + v * (W - P * 2), y = (v) => H - P - v * (H - P * 2);
    const pts = cal.map((c) => `${x(c.predicted)},${y(c.observed)}`).join(" ");
    const dots = cal.map((c) => `<circle cx="${x(c.predicted)}" cy="${y(c.observed)}" r="${Math.max(3, Math.min(8, Math.sqrt(c.n)))}" fill="#0f766e" opacity=".8"><title>${pct(c.predicted,1)} predicted, ${pct(c.observed,1)} observed (n=${c.n})</title></circle>`).join("");
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:420px">
      <line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}" stroke="#d0d5dd"/><line x1="${P}" y1="${P}" x2="${P}" y2="${H-P}" stroke="#d0d5dd"/>
      <line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${P}" stroke="#98a2b3" stroke-dasharray="4 4"/>
      <polyline points="${pts}" fill="none" stroke="#0f766e" stroke-width="2"/>
      ${dots}
      <text x="${W/2}" y="${H-8}" text-anchor="middle" font-size="10" fill="#667085">predicted probability</text>
      <text x="10" y="${H/2}" text-anchor="middle" font-size="10" fill="#667085" transform="rotate(-90 10 ${H/2})">observed rate</text>
      <text x="${P}" y="${H-P+14}" font-size="9" fill="#98a2b3">0</text><text x="${W-P-4}" y="${H-P+14}" font-size="9" fill="#98a2b3">1</text>
    </svg>`;
  }

  async function renderModel() {
    setNav("model");
    spinner("Loading model card…");
    const d = await api("/api/model/health");
    const m = d.model, met = m.metrics || {};
    const cards = [
      ["ROC AUC", met.auc, "all test horizons"],
      ["ROC AUC @ 90d", met.at_90d?.auc, "operational decision point"],
      ["PR AUC", met.pr_auc, `base rate ${pct(met.base_rate,1)}`],
      ["Brier (calibrated)", met.brier, `uncalibrated ${met.brier_uncalibrated ?? "—"}`],
      ["ECE", met.ece, "10-bin calibration error"],
      ["Top-100 precision", met.queue?.precision_at_k, `ARR recall ${pct(met.queue?.churned_arr_recall_at_k,0)}`],
    ];
    const top = (met.top_features || []);
    const maxImp = Math.max(...top.map((f) => f.importance), 0.0001);
    const dist = d.score_distribution || {};
    $("#view").innerHTML = `
      <div class="between" style="margin-bottom:16px">
        <div><h1>Model health</h1><div class="muted">Trained on renewal outcomes only, split by renewal date. The LLM is not in this path.</div></div>
        <div class="tag">${esc(m.id)} · trained ${dateTime(m.trained_at)}</div>
      </div>
      <div class="grid cards" style="grid-template-columns:repeat(6,1fr)">
        ${cards.map(([k, v, s]) => `<div class="card stat"><div class="k">${esc(k)}</div><div class="v">${typeof v === "number" ? v.toFixed(3) : (v ?? "—")}</div><div class="s">${esc(s || "")}</div></div>`).join("")}
      </div>

      <div class="grid two" style="margin-top:14px">
        <div class="card"><div class="hd"><h3>Reliability</h3><span class="tag">test window · 90-day horizon</span></div>
          <div class="bd"><div class="row" style="gap:24px;align-items:flex-start"><div>${reliabilityChart(met.at_90d?.calibration)}</div>
          <div class="kv" style="grid-template-columns:130px 1fr">
            <div class="k">train renewals</div><div>${met.split?.train_renewals ?? "—"}</div>
            <div class="k">calibration</div><div>${met.split?.calib_renewals ?? "—"} renewals</div>
            <div class="k">test renewals</div><div>${met.split?.test_renewals ?? "—"}</div>
            <div class="k">train through</div><div>${date(met.split?.train_through)}</div>
            <div class="k">test from</div><div>${date(met.split?.test_from)}</div>
            <div class="k">thresholds</div><div>red ≥ ${pct(met.thresholds?.red,0)} · yellow ≥ ${pct(met.thresholds?.yellow,0)}</div>
            <div class="k">dataset hash</div><div class="ref">${esc(m.dataset_hash)}</div>
          </div></div></div></div>
        <div class="card"><div class="hd"><h3>Score distribution</h3><span class="tag">latest run</span></div><div class="bd">
          <div class="kv">
            <div class="k">accounts scored</div><div>${dist.count ?? "—"}</div>
            <div class="k">mean probability</div><div>${pct(dist.mean_prob,1)}</div>
            <div class="k">bands</div><div>${dist.bands ? `red ${dist.bands.red} · yellow ${dist.bands.yellow} · green ${dist.bands.green}` : "—"}</div>
            <div class="k">low confidence</div><div>${dist.low_confidence ?? "—"}</div>
            <div class="k">test base rate</div><div>${pct(met.base_rate,1)}</div>
            <div class="k">train base rate</div><div>${pct(met.train_base_rate,1)}</div>
          </div>
          <div class="banner" style="margin-top:14px">${esc((met.queue && met.queue.k) || 0)}-account queue captured <strong>${pct(met.queue?.churned_arr_recall_at_k,0)}</strong> of churned ARR in the test window. Usage-trend heuristic: ${pct(met.baseline_usage_trend?.churned_arr_recall_at_k,0)}; silence heuristic: ${pct(met.baseline_silence?.churned_arr_recall_at_k,0)}.</div>
        </div></div>
      </div>

      <div class="grid two" style="margin-top:14px">
        <div class="card"><div class="hd"><h3>Global feature importance</h3><span class="tag">permutation, test sample</span></div><div class="bd">
          ${top.map((f) => `<div class="hbar"><div>${esc(f.label)}</div><div class="t"><span style="width:${(f.importance / maxImp) * 100}%"></span></div><div class="tag">${f.importance.toFixed(3)}</div></div>`).join("")}
        </div></div>
        <div class="card"><div class="hd"><h3>How a score is produced</h3></div><div class="bd">
          <ol style="margin:0;padding-left:18px;line-height:1.7" class="muted">
            <li>Raw event lands in <span class="mono">raw_events</span> with both <em>occurred_at</em> and <em>arrived_at</em>.</li>
            <li>ETL dedupes on natural key, resolves the account by domain → contact → fuzzy name, and writes typed facts.</li>
            <li>Features use only facts with <em>arrived_at ≤ as_of</em>, so late data cannot leak into a past score.</li>
            <li>Gradient-boosted trees produce a raw probability; isotonic calibration maps it to the observed renewal outcome.</li>
            <li>Occlusion attribution and raw evidence are written alongside the score, keyed to run and model version.</li>
          </ol>
          <div class="footer-note">Target: churn at next renewal. Downgrades (${met.downgrades_excluded || 0}) are excluded from training and reported separately. Base rate is deliberately not resampled; calibration handles class imbalance.</div>
        </div></div>
      </div>
      <div class="card" style="margin-top:14px"><div class="hd"><h3>Known limits</h3></div><div class="bd">
        <div class="grid three">
          <div><strong>Horizon.</strong><div class="muted tiny">Validated for renewals 30–330 days out. Beyond that the account is still scored but marked outside horizon and kept out of the default queue.</div></div>
          <div><strong>Label noise.</strong><div class="muted tiny">A share of churn is genuinely unforecastable (budget, M&A, a competitor's discount). AUC near 0.7 is expected; the queue uses lift and calibration, not accuracy theatre.</div></div>
          <div><strong>Intervention bias.</strong><div class="muted tiny">CSM touches are excluded from features. Otherwise the model would learn that contacting an at-risk account causes churn.</div></div>
        </div>
      </div></div>
    `;
  }

  /* ------------------------------------------------------------------ pipeline */
  async function renderPipeline() {
    setNav("pipeline");
    spinner("Loading pipeline quality…");
    const d = await api("/api/pipeline/quality");
    const srcs = Object.keys(d.raw_by_source).sort();
    const maxRaw = Math.max(...srcs.map((s) => d.raw_by_source[s]), 1);
    $("#view").innerHTML = `
      <div class="between" style="margin-bottom:16px"><div><h1>Pipeline & data quality</h1><div class="muted">Messy input is expected. We measure it instead of pretending it away.</div></div></div>
      <div class="grid cards" style="grid-template-columns:repeat(5,1fr)">
        <div class="card stat"><div class="k">Raw events</div><div class="v">${d.raw_events.toLocaleString()}</div><div class="s">five sources</div></div>
        <div class="card stat"><div class="k">Canonical facts</div><div class="v">${d.facts.toLocaleString()}</div><div class="s">linked to raw</div></div>
        <div class="card stat"><div class="k">Duplicate rate</div><div class="v">${pct(d.duplicate_rate,1)}</div><div class="s">dropped on natural key</div></div>
        <div class="card stat"><div class="k">Unmatched rate</div><div class="v">${pct(d.unmatched_rate,2)}</div><div class="s">no account match</div></div>
        <div class="card stat"><div class="k">4+ source coverage</div><div class="v">${pct(d.four_source_coverage,0)}</div><div class="s">of active accounts</div></div>
      </div>
      <div class="grid two" style="margin-top:14px">
        <div class="card"><div class="hd"><h3>Volume by source</h3><span class="tag">landing zone vs canonical</span></div><div class="bd">
          ${srcs.map((s) => `<div class="hbar"><div>${esc(s)}</div><div class="t"><span style="width:${(d.raw_by_source[s] / maxRaw) * 100}%"></span></div><div class="tag">${d.raw_by_source[s].toLocaleString()}</div></div>
            <div class="tag" style="margin:-4px 0 8px 229px">${d.facts_by_source[s]?.toLocaleString() || 0} facts</div>`).join("")}
          <div class="footer-note">Raw volume includes deliberately duplicated records. Canonical volume is lower by design.</div>
        </div></div>
        <div class="card"><div class="hd"><h3>Defects handled</h3><span class="tag">ingest issues</span></div><div class="bd">
          <div class="kv">
            ${Object.entries(d.issues).sort((a,b)=>b[1]-a[1]).map(([k,v]) => `<div class="k">${esc(k.replace(/_/g," "))}</div><div>${v.toLocaleString()}</div>`).join("") || '<div class="k">none</div><div>—</div>'}
            <div class="k">tz assumed</div><div>${d.tz_assumed} naive timestamps treated as UTC</div>
          </div>
          <div class="banner" style="margin-top:14px">Naive timestamps are flagged rather than silently trusted. Accounts with fewer than three sources are marked low-confidence and are never auto-contacted.</div>
        </div></div>
      </div>
      <div class="card" style="margin-top:14px"><div class="hd"><h3>Recent runs</h3></div><div class="bd tight">
        <table><thead><tr><th>Run</th><th>Started</th><th>Status</th><th class="num">Read</th><th class="num">Kept</th><th class="num">Duplicates</th><th class="num">Unmatched</th><th class="num">Held back</th></tr></thead><tbody>
        ${(d.recent_runs || []).map((r) => { const ing = (r.stats && r.stats.ingest) || {}; return `<tr><td class="ref">${esc(r.id.slice(0, 22))}</td><td>${dateTime(r.started_at)}</td><td>${statusTag(r.status)}</td>
          <td class="num">${(ing.read ?? 0).toLocaleString()}</td><td class="num">${(ing.kept ?? 0).toLocaleString()}</td>
          <td class="num">${(ing.duplicates ?? 0).toLocaleString()}</td><td class="num">${(ing.unmatched ?? 0).toLocaleString()}</td><td class="num">${(ing.held_back ?? 0).toLocaleString()}</td></tr>`; }).join("") || '<tr><td colspan="8" class="empty">No runs yet.</td></tr>'}
        </tbody></table>
      </div></div>
    `;
  }

  /* ------------------------------------------------------------------ runs */
  async function renderRuns(runId) {
    setNav("runs");
    if (runId) return renderRunDetail(runId);
    spinner("Loading runs…");
    const d = await api("/api/runs");
    const rows = d.rows.map((r) => `<tr class="click" data-run="${esc(r.id)}">
      <td class="ref">${esc(r.id)}</td><td>${dateTime(r.started_at)}</td><td>${statusTag(r.status)}</td>
      <td>${esc(r.trigger)}</td><td class="ref">${esc(r.model_version || "—")}</td>
      <td class="num">${(r.stats.ingest && r.stats.ingest.kept) || 0}</td>
      <td class="num">${(r.stats.actions && r.stats.actions.created) || 0}</td>
      <td>${r.error ? `<span class="tag" style="color:var(--red)">${esc(r.error)}</span>` : ""}</td></tr>`).join("");
    $("#view").innerHTML = `<div class="between" style="margin-bottom:16px"><div><h1>Agent runs</h1><div class="muted">Unattended runs are triggered by the scheduler. Every step is recorded.</div></div></div>
      <div class="card"><div class="bd tight"><table><thead><tr><th>Run</th><th>Started</th><th>Status</th><th>Trigger</th><th>Model</th><th class="num">Events in</th><th class="num">Actions</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="8" class="empty">No runs.</td></tr>'}</tbody></table></div></div>`;
    $$("tr[data-run]").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/runs/${tr.dataset.run}`; }));
  }

  async function renderRunDetail(runId) {
    spinner("Loading run…");
    const d = await api(`/api/runs/${encodeURIComponent(runId)}`);
    const r = d.run, st = r.stats || {};
    const steps = d.steps || [];
    const fmt = (v) => JSON.stringify(v, null, 2);
    $("#view").innerHTML = `
      <div class="crumb"><a href="#/runs">← Agent runs</a> · ${esc(r.id)}</div>
      <div class="between" style="margin-bottom:14px"><div><h1>Run detail</h1><div class="muted">${esc(r.trigger)} · as of ${dateTime(r.as_of)} · ${dateTime(r.started_at)} → ${dateTime(r.finished_at)}</div></div>${statusTag(r.status)}</div>
      <div class="grid cards" style="grid-template-columns:repeat(4,1fr)">
        <div class="card stat"><div class="k">Events kept</div><div class="v">${(st.ingest?.kept || 0).toLocaleString()}</div><div class="s">${(st.ingest?.duplicates || 0).toLocaleString()} duplicates dropped</div></div>
        <div class="card stat"><div class="k">Transitions</div><div class="v">${Object.values(st.transitions || {}).reduce((a,b)=>a+b,0)}</div><div class="s">${Object.entries(st.transitions || {}).map(([k,v]) => `${v} ${k.replace(/_/g," ")}`).join(" · ") || "none"}</div></div>
        <div class="card stat"><div class="k">Actions created</div><div class="v">${st.actions?.created || 0}</div><div class="s">${st.actions?.auto_sent || 0} auto-sent · ${st.actions?.blocked || 0} blocked</div></div>
        <div class="card stat"><div class="k">ARR at risk</div><div class="v">${money(st.digest?.arr_at_risk_cents || 0)}</div><div class="s">${st.digest?.queue || 0} red + yellow accounts</div></div>
      </div>
      <div class="grid two" style="margin-top:14px">
        <div class="card"><div class="hd"><h3>Steps</h3></div><div class="bd">
          ${steps.map((s) => `<div class="step"><div class="n">${s.seq}</div><div><strong>${esc(s.name)}</strong><div class="tag">${esc(s.status)} · ${dateTime(s.started_at)}</div></div><div class="s">${s.finished_at ? Math.round((new Date(s.finished_at)-new Date(s.started_at))/1000)+"s" : ""}</div></div>`).join("")}
        </div></div>
        <div class="card"><div class="hd"><h3>Raw stats</h3><span class="tag">what the run actually produced</span></div><div class="bd"><pre class="json" style="max-height:420px">${esc(fmt(st))}</pre></div></div>
      </div>
      ${r.error ? `<div class="error" style="margin-top:14px"><strong>Run failed</strong><pre class="json">${esc(r.error)}</pre></div>` : ""}
    `;
  }

  /* ------------------------------------------------------------------ header */
  $("#run-btn").addEventListener("click", async () => {
    const btn = $("#run-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Running';
    try {
      const r = await api("/api/runs", { method: "POST", body: JSON.stringify({}) });
      toast(`Run ${r.run_id.slice(0, 18)}… completed: ${r.stats.actions?.created || 0} actions`);
      await boot();
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Run agent";
    }
  });

  $("#kill-btn").addEventListener("click", async () => {
    try {
      const enabled = !state.me.kill_switch;
      await api("/api/admin/kill-switch", { method: "POST", body: JSON.stringify({ enabled }) });
      state.me.kill_switch = enabled;
      $("#kill-btn").classList.toggle("danger", enabled);
      $("#kill-btn").textContent = enabled ? "Kill switch ON" : "Kill switch";
      toast(enabled ? "Outbound automation stopped for this tenant." : "Outbound automation re-enabled.");
    } catch (e) { toast(e.message); }
  });

  $("#switch-btn").addEventListener("click", logout);

  boot();
})();
