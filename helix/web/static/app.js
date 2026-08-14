/* Helix console — designed for non-technical operators */

const state = {
  token: localStorage.getItem("helix_token") || "",
  me: null,
  tenantSlug: localStorage.getItem("helix_tenant") || "",
  brief: null,
  schemas: [],
  agents: [],
  selectedAgent: "research_director",
  jobPollTimer: null,
};

const QUALITY_COPY = {
  1: {
    label: "Mode 1 — Best quality",
    desc: "All 15 AI helpers. Highest token use, strongest judgment.",
    // Recalibrated ~2–3× after live runs (conservative)
    etaBatch: { pipeline: 180, synthesis: 90 },
  },
  2: {
    label: "Mode 2 — High quality",
    desc: "Core AI helpers + code for mechanical steps. Domain follows your Plan.",
    etaBatch: { pipeline: 120, synthesis: 55 },
  },
  3: {
    label: "Mode 3 — Balanced",
    desc: "Mostly code; LLM only on quality gates (or templates for synthesis).",
    etaBatch: { pipeline: 75, synthesis: 30 },
  },
  4: {
    label: "Mode 4 — Lowest cost",
    desc: "Ultra lean: deterministic tools / templates only.",
    etaBatch: { pipeline: 40, synthesis: 12 },
  },
};

const $ = (id) => document.getElementById(id);

const FRIENDLY_AGENTS = {
  research_director: {
    title: "Project director",
    blurb: "Decides what to collect next based on your plan.",
    role: "Leader",
  },
  scope_guardian: {
    title: "Boundary checker",
    blurb: "Keeps work inside the topics you allowed.",
    role: "Guard",
  },
  discovery: {
    title: "Finder",
    blurb: "Looks for candidate material worth collecting.",
    role: "Collect",
  },
  evidence_collector: {
    title: "Detail gatherer",
    blurb: "Pulls the full content behind each candidate.",
    role: "Collect",
  },
  duplicate_resolver: {
    title: "Duplicate checker",
    blurb: "Avoids saving the same thing twice.",
    role: "Quality",
  },
  fact_verification: {
    title: "Quality reviewer",
    blurb: "Approves only trustworthy material.",
    role: "Quality",
  },
  strategy_synthesizer: {
    title: "Strategy synthesizer",
    blurb: "Turns verified clusters into training-ready strategic notes.",
    role: "Structure",
  },
  training_quality_reviewer: {
    title: "Training quality reviewer",
    blurb: "Tries to break draft training rows before they become gold.",
    role: "Quality",
  },
  campaign_strategist: {
    title: "Strategy synthesizer",
    blurb: "Turns verified clusters into training-ready strategic notes.",
    role: "Structure",
  },
  adversarial_reviewer: {
    title: "Training quality reviewer",
    blurb: "Tries to break draft training rows before they become gold.",
    role: "Quality",
  },
  knowledge_extraction: {
    title: "Fact extractor",
    blurb: "Pulls clean facts from approved material.",
    role: "Structure",
  },
  knowledge_graph: {
    title: "Knowledge keeper",
    blurb: "Stores facts and notes contradictions.",
    role: "Structure",
  },
  campaign_strategist: {
    title: "Strategy synthesizer",
    blurb: "Turns verified evidence into domain insights for gold examples (not limited to marketing).",
    role: "Insights",
  },
  dataset_curator: {
    title: "Dataset organizer",
    blurb: "Cleans, splits, and packages final training files.",
    role: "Package",
  },
  synthetic_generator: {
    title: "Example expander",
    blurb: "Creates more practice examples from good seeds.",
    role: "Expand",
  },
  adversarial_reviewer: {
    title: "Tough critic",
    blurb: "Tries to break weak examples before they ship.",
    role: "Quality",
  },
  benchmark_builder: {
    title: "Test-set builder",
    blurb: "Holds out fair tests that training never sees.",
    role: "Evaluate",
  },
  trainer: {
    title: "Practice trainer",
    blurb: "Simulates a training run and score check.",
    role: "Train",
  },
  operations_dashboard: {
    title: "Status reporter",
    blurb: "Summarizes health and open questions for you.",
    role: "Report",
  },
};

const DEFAULT_SCHEMA = {
  type: "object",
  required: ["input", "output", "difficulty"],
  properties: {
    input: { type: "string", description: "What the AI sees" },
    output: { type: "string", description: "The ideal answer" },
    rationale: { type: "string", description: "Why this answer is correct" },
    difficulty: {
      type: "string",
      enum: ["canonical", "moderate", "edge-case"],
    },
    is_negative: {
      type: "boolean",
      description: "True if this is a bad example to avoid",
    },
  },
};

// ── Helpers ─────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (!(opts.body instanceof FormData) && opts.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const method = (opts.method || "GET").toUpperCase();
  // Prevent stale job/library counters after redeploy or mid-batch progress
  const fetchOpts = {
    ...opts,
    headers,
    cache: opts.cache || "no-store",
  };
  // Cache-bust GETs so polling always hits the live API (not a frozen browser cache)
  let url = path;
  if (method === "GET" && path.startsWith("/api/")) {
    const sep = path.includes("?") ? "&" : "?";
    url = `${path}${sep}_=${Date.now()}`;
  }
  const res = await fetch(url, fetchOpts);
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const detail = data.detail;
    const msg =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : data.error || "Something went wrong. Please try again.";
    throw new Error(friendlyError(msg));
  }
  return data;
}

function friendlyError(msg) {
  const m = String(msg || "");
  if (/not authenticated|invalid token|401/i.test(m)) return "Please sign in again.";
  if (/budget/i.test(m)) return "This workspace has used its monthly budget.";
  if (/No LLM key|OPENROUTER/i.test(m))
    return "AI helpers need an OpenRouter key set up on the server.";
  if (/already exists/i.test(m)) return "That name is already in use. Try another.";
  return m;
}

function toast(message, type = "ok") {
  const host = $("toastHost");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function linesToList(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function csvToList(text) {
  return String(text || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseTargets(text) {
  const out = {};
  for (const line of linesToList(text)) {
    // "billing 40" or "billing: 40" or "billing, 40"
    const m = line.match(/^([a-zA-Z0-9_\- ]+?)[\s,:]+(\d+)\s*$/);
    if (m) out[m[1].trim()] = parseInt(m[2], 10);
  }
  return out;
}

function formatTargets(obj) {
  if (!obj || typeof obj !== "object") return "";
  return Object.entries(obj)
    .map(([k, v]) => `${k} ${v}`)
    .join("\n");
}

function parseMetrics(text) {
  return linesToList(text).map((line) => {
    const parts = line.split(/[:–-]/).map((s) => s.trim()).filter(Boolean);
    if (parts.length >= 2) return { name: parts[0], target: parts.slice(1).join(" - ") };
    return { name: line, target: "improve over time" };
  });
}

function formatMetrics(arr) {
  if (!Array.isArray(arr)) return "";
  return arr
    .map((m) => {
      if (typeof m === "string") return m;
      if (m && m.name) return m.target ? `${m.name}: ${m.target}` : m.name;
      return JSON.stringify(m);
    })
    .join("\n");
}

function slugify(name) {
  return String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 40) || "format";
}

function showApp(show) {
  $("loginPanel").classList.toggle("hidden", show);
  $("appPanel").classList.toggle("hidden", !show);
  // logout lives in the ink sidebar (only visible when appPanel is shown)
  if ($("logoutBtn")) $("logoutBtn").classList.toggle("hidden", !show);
}

function goTab(name) {
  if (name === "plan" || name === "formats" || name === "helpers") name = "riu";
  document.querySelectorAll(".nav-pill").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
  const panel = $(`tab-${name}`);
  if (panel) panel.classList.remove("hidden");
  if (name === "riu") {
    loadRiuSession().catch((e) => toast(e.message, "err"));
  }
  if (name === "home") {
    loadJobs().catch(() => {});
  }
  if (name === "library") {
    loadDoubleHelixModels().catch(() => {});
  }
}

// ── Auth screens ────────────────────────────────────────────────────

const authState = {
  mode: "login", // login | signup | forgot | set-password | reset | verify
  token: "",
};

function clearAuthMessages() {
  $("loginError").textContent = "";
  $("loginSuccess").textContent = "";
  $("devLinkBox").classList.add("hidden");
  $("devLinkBox").textContent = "";
}

function showDevLink(link) {
  if (!link) return;
  const box = $("devLinkBox");
  box.classList.remove("hidden");
  box.innerHTML = `Email was not sent (no Resend key). <strong>Dev link:</strong><br><a href="${escapeHtml(link)}">${escapeHtml(link)}</a>`;
}

function setAuthMode(mode) {
  authState.mode = mode;
  clearAuthMessages();
  ["authLogin", "authSignup", "authForgot", "authSetPassword", "authVerify"].forEach((id) => {
    $(id).classList.add("hidden");
  });

  const titles = {
    login: {
      eye: "Welcome",
      title: "Collect great examples for your AI",
      lead: "Sign in to plan what to collect, run your AI helpers, and download training data.",
      panel: "authLogin",
    },
    signup: {
      eye: "New account",
      title: "Create your Helix account",
      lead: "We’ll set up a private workspace for your training data.",
      panel: "authSignup",
    },
    forgot: {
      eye: "Password help",
      title: "Forgot your password?",
      lead: "Enter your email and we’ll send a secure reset link.",
      panel: "authForgot",
    },
    "set-password": {
      eye: "Almost there",
      title: "Create your password",
      lead: "Choose a password to finish setting up your account.",
      panel: "authSetPassword",
    },
    reset: {
      eye: "Password reset",
      title: "Choose a new password",
      lead: "Pick something memorable and secure (at least 8 characters).",
      panel: "authSetPassword",
    },
    verify: {
      eye: "Email confirmation",
      title: "Confirming your email…",
      lead: "One moment while we confirm your address.",
      panel: "authVerify",
    },
  };
  const t = titles[mode] || titles.login;
  $("authEyebrow").textContent = t.eye;
  $("authTitle").textContent = t.title;
  $("authLead").textContent = t.lead;
  $(t.panel).classList.remove("hidden");
}

function parseAuthQuery() {
  const params = new URLSearchParams(window.location.search);
  const mode = params.get("mode") || "";
  const token = params.get("token") || "";
  if (token && (mode === "reset" || mode === "set-password" || mode === "verify")) {
    authState.token = token;
    if (mode === "verify") {
      setAuthMode("verify");
      verifyEmailToken(token);
    } else {
      setAuthMode(mode === "reset" ? "reset" : "set-password");
    }
    // Clean URL without losing app path
    window.history.replaceState({}, "", "/app");
  }
}

async function login() {
  clearAuthMessages();
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: $("email").value.trim(),
        password: $("password").value,
      }),
    });
    state.token = data.access_token;
    localStorage.setItem("helix_token", state.token);
    await bootstrap();
    toast("Welcome back!");
  } catch (e) {
    $("loginError").textContent = e.message;
  }
}

async function signup() {
  clearAuthMessages();
  const password = $("signupPassword").value;
  if (password.length < 8) {
    $("loginError").textContent = "Password must be at least 8 characters.";
    return;
  }
  try {
    const data = await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        email: $("signupEmail").value.trim(),
        full_name: $("signupName").value.trim(),
        password,
      }),
    });
    $("loginSuccess").textContent = data.message || "Account created.";
    showDevLink(data.dev_link);
    toast("Account created");
    if (!data.dev_link) {
      setTimeout(() => setAuthMode("login"), 1200);
    }
  } catch (e) {
    $("loginError").textContent = e.message;
  }
}

async function forgotPassword() {
  clearAuthMessages();
  try {
    const data = await api("/api/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify({ email: $("forgotEmail").value.trim() }),
    });
    $("loginSuccess").textContent = data.message || "Check your email.";
    showDevLink(data.dev_link);
    toast("Check your email");
  } catch (e) {
    $("loginError").textContent = e.message;
  }
}

async function submitNewPassword() {
  clearAuthMessages();
  const p1 = $("newPassword").value;
  const p2 = $("newPassword2").value;
  if (p1.length < 8) {
    $("loginError").textContent = "Password must be at least 8 characters.";
    return;
  }
  if (p1 !== p2) {
    $("loginError").textContent = "Passwords do not match.";
    return;
  }
  if (!authState.token) {
    $("loginError").textContent = "Missing reset link. Request a new email.";
    return;
  }
  const endpoint =
    authState.mode === "set-password" ? "/api/auth/set-password" : "/api/auth/reset-password";
  try {
    const data = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ token: authState.token, password: p1 }),
    });
    $("loginSuccess").textContent = data.message || "Password saved.";
    toast("Password saved — you can sign in");
    authState.token = "";
    setTimeout(() => setAuthMode("login"), 900);
  } catch (e) {
    $("loginError").textContent = e.message;
  }
}

async function verifyEmailToken(token) {
  clearAuthMessages();
  $("verifyMessage").textContent = "Confirming your email…";
  try {
    const data = await api(`/api/auth/verify-email?token=${encodeURIComponent(token)}`);
    $("verifyMessage").textContent = data.message || "Email confirmed.";
    $("authTitle").textContent = "Email confirmed";
    $("authLead").textContent =
      "If admin approval is required, wait for the activation email before signing in.";
    toast("Email confirmed");
  } catch (e) {
    $("verifyMessage").textContent = e.message;
    $("authTitle").textContent = "Link problem";
    $("loginError").textContent = e.message;
  }
}

function logout() {
  state.token = "";
  state.me = null;
  localStorage.removeItem("helix_token");
  showApp(false);
  setAuthMode("login");
}

async function bootstrap() {
  state.me = await api("/api/auth/me");
  const email = state.me.email || "";
  const name = (state.me && (state.me.full_name || state.me.name)) || email;
  $("userLabel").textContent = name;
  $("userAvatar").textContent = (name[0] || email[0] || "U").toUpperCase();
  if ($("userRole")) {
    $("userRole").textContent = state.me && state.me.is_superadmin ? "Admin" : "Owner";
  }
  showApp(true);

  let tenants = state.me.tenants || [];
  if (!tenants.length && state.me.is_superadmin) {
    tenants = await api("/api/tenants");
  }

  const sel = $("tenantSelect");
  sel.innerHTML = "";
  tenants.forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t.slug;
    opt.textContent = t.name || t.slug;
    sel.appendChild(opt);
  });

  if (!tenants.length) {
    sel.innerHTML = "<option value=''>No workspace yet</option>";
    toast("No workspace found. Ask your admin to create one.", "err");
    return;
  }

  if (!state.tenantSlug || ![...sel.options].some((o) => o.value === state.tenantSlug)) {
    state.tenantSlug = tenants[0].slug;
  }
  sel.value = state.tenantSlug;
  localStorage.setItem("helix_tenant", state.tenantSlug);

  state.agents = await api(`/api/t/${state.tenantSlug}/agents`);
  renderAgents();
  clearSchemaForm();
  await refreshAll();
}

async function refreshAll() {
  state.tenantSlug = $("tenantSelect").value;
  localStorage.setItem("helix_tenant", state.tenantSlug);
  if (!state.tenantSlug) return;
  await Promise.all([
    refreshDashboard(),
    loadBrief(),
    loadSchemas(),
    loadDatasets(),
    loadLibrary(),
    loadJobs(),
  ]);
  updatePipeEta();
  updateSynthEta();
  startJobPolling();
}

function updatePipeQualityUI() {
  const m = parseInt($("pipeQuality")?.value || "2", 10);
  const c = QUALITY_COPY[m] || QUALITY_COPY[2];
  if ($("pipeQualityLabel")) $("pipeQualityLabel").textContent = c.label;
  if ($("pipeQualityDesc")) $("pipeQualityDesc").textContent = c.desc;
  updatePipeEta();
}

function updateSynthQualityUI() {
  const m = parseInt($("synthQuality")?.value || "2", 10);
  const c = QUALITY_COPY[m] || QUALITY_COPY[2];
  if ($("synthQualityLabel")) $("synthQualityLabel").textContent = c.label;
  updateSynthEta();
}

function updatePipeEta() {
  if (!$("pipeEtaHint")) return;
  const m = parseInt($("pipeQuality").value || "2", 10);
  const batches = parseInt($("pipeBatches").value || "1", 10);
  const sec = (QUALITY_COPY[m]?.etaBatch.pipeline || 30) * Math.max(1, batches);
  $("pipeEtaHint").textContent = `Estimated total time: ${_humanEta(sec)} (updates after first batch)`;
}

function updateSynthEta() {
  if (!$("synthEtaHint")) return;
  const m = parseInt($("synthQuality").value || "2", 10);
  const batches = parseInt($("synthBatches")?.value || "1", 10);
  const sec = (QUALITY_COPY[m]?.etaBatch.synthesis || 15) * Math.max(1, batches);
  $("synthEtaHint").textContent = `Estimated total time: ${_humanEta(sec)} (updates live while running)`;
}

function _humanEta(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `~${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return `~${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `~${h}h ${m % 60}m`;
}

function _jobResultBanner(j) {
  const s = j.result_summary || {};
  const last = s.last_batch || {};
  // Prefer job-level cumulative totals over last-batch-only counters
  const goldNew =
    s.total_gold_new != null
      ? s.total_gold_new
      : s.gold_new != null
        ? s.gold_new
        : last.gold_new != null
          ? last.gold_new
          : 0;
  const msg =
    s.job_user_message ||
    j.progress_message ||
    s.user_message ||
    last.user_message ||
    "";
  const zero =
    (s.zero_evidence || last.zero_evidence) && Number(goldNew) === 0;
  const costLine = _jobCostLine(j);
  if (j.status === "paused_spend_cap") {
    return `<div class="banner warn" style="margin-top:8px">
      <strong>Paused — spend cap (consent required).</strong>
      ${escapeHtml(msg || "Job trajectory would exceed $35 per 1,000 gold.")}
      ${costLine}
      <p class="hint mb-0" style="margin-top:6px">
        Confirm to run remaining batches past the cap, or Cancel to stop. No further spend until you choose.
      </p>
    </div>`;
  }
  if (j.status === "completed" && Number(goldNew) === 0) {
    return `<div class="banner warn" style="margin-top:8px">
      <strong>No new gold this job.</strong>
      ${escapeHtml(msg || "0 new on-topic examples. Existing library items were not produced by this job.")}
      ${costLine}
    </div>`;
  }
  if (Number(goldNew) > 0 && (j.status === "completed" || j.status === "running")) {
    return `<div class="banner ok" style="margin-top:8px">
      <strong>${goldNew}</strong> new gold example(s) from this job.
      ${escapeHtml(msg)}
      Seed/demo rows stay labeled separately in My data.
      ${costLine}
    </div>`;
  }
  if (zero) {
    return `<div class="banner warn" style="margin-top:8px">
      <strong>No verifiable sources this batch.</strong>
      ${escapeHtml(msg || "")}
      ${costLine}
    </div>`;
  }
  return costLine
    ? `<div class="hint" style="margin-top:6px">${costLine}</div>`
    : "";
}

function _jobCostLine(j) {
  const s = j.result_summary || {};
  const orC =
    j.openrouter_cost_usd != null
      ? j.openrouter_cost_usd
      : s.openrouter_cost_usd != null
        ? s.openrouter_cost_usd
        : null;
  const apC =
    j.apify_cost_usd != null
      ? j.apify_cost_usd
      : s.apify_cost_usd != null
        ? s.apify_cost_usd
        : null;
  const total =
    j.cost_usd != null
      ? j.cost_usd
      : s.cost_usd != null
        ? s.cost_usd
        : orC != null || apC != null
          ? Number(orC || 0) + Number(apC || 0)
          : null;
  if (total == null && orC == null && apC == null) return "";
  const cap = j.spend_cap_usd != null ? j.spend_cap_usd : s.spend_cap_usd;
  const bits = [];
  if (orC != null) bits.push(`OpenRouter $${Number(orC).toFixed(4)}`);
  if (apC != null) bits.push(`Apify $${Number(apC).toFixed(4)}`);
  if (total != null) bits.push(`total $${Number(total).toFixed(4)}`);
  if (cap != null && Number(cap) > 0) bits.push(`cap $${Number(cap).toFixed(4)}`);
  return bits.length
    ? `<div class="hint" style="margin-top:4px">Cost: ${bits.join(" · ")}</div>`
    : "";
}

function _jobRenderKey(j) {
  // Force re-render whenever any live counter changes
  return [
    j.id,
    j.status,
    j.completed_batches,
    j.items_processed,
    j.progress_pct,
    j.progress_message,
    j.updated_at,
    j.eta_seconds,
    JSON.stringify(j.result_summary || {}),
  ].join("|");
}

async function loadJobs() {
  if (!$("jobsList") || !state.tenantSlug) return;
  if (state._jobsLoading) return; // prevent stacked polls from racing
  state._jobsLoading = true;
  try {
    const data = await api(`/api/t/${state.tenantSlug}/jobs`);
    const jobs = data.jobs || [];
    const active = (data.active || []).length;
    if ($("jobsLiveHint")) {
      $("jobsLiveHint").textContent = active
        ? `${active} job(s) running — live counters (no-cache poll every 2s)`
        : "No active jobs";
    }
    if (!jobs.length) {
      $("jobsList").innerHTML = `<div class="empty"><div class="icon">⏱️</div>No jobs yet. Start a mining or synthesis job above.</div>`;
      state._jobsRenderKey = "";
      return;
    }
    const renderKey = jobs.map(_jobRenderKey).join("||");
    // Still re-render if anything moved; skip only exact same snapshot
    if (state._jobsRenderKey === renderKey && $("jobsList").children.length) {
      return;
    }
    state._jobsRenderKey = renderKey;

    // Visual progress: while a batch is running, show partial credit so UI doesn't freeze at 0%
    $("jobsList").innerHTML = jobs
      .map((j) => {
        const badge =
          j.status === "completed"
            ? "ok"
            : j.status === "failed" || j.status === "cancelled"
              ? "err"
              : j.status === "paused_spend_cap"
                ? "warn"
                : j.status === "running"
                  ? "accent"
                  : "warn";
        const statusLabel =
          j.status === "paused_spend_cap" ? "paused (spend cap)" : j.status;
        const typeLabel = j.job_type === "synthesis" ? "Synthesis" : "Mining";
        let pct = Number(j.progress_pct) || 0;
        if (j.status === "running" && pct < 100) {
          // mid-batch pulse based on updated_at recency so bar moves during long gathers
          const tick = (Date.now() / 1000) % 1;
          const partial =
            ((j.completed_batches + 0.2 + 0.6 * tick) / Math.max(j.total_batches, 1)) * 100;
          pct = Math.max(pct, Math.min(99, Math.round(partial * 10) / 10));
        }
        const updated = j.updated_at ? new Date(j.updated_at).toLocaleTimeString() : "—";
        const costBits = [];
        if (j.openrouter_cost_usd != null)
          costBits.push(`OR $${Number(j.openrouter_cost_usd).toFixed(3)}`);
        if (j.apify_cost_usd != null)
          costBits.push(`Apify $${Number(j.apify_cost_usd).toFixed(3)}`);
        if (j.cost_usd != null) costBits.push(`Σ $${Number(j.cost_usd).toFixed(3)}`);
        if (j.spend_cap_usd != null && Number(j.spend_cap_usd) > 0)
          costBits.push(`cap $${Number(j.spend_cap_usd).toFixed(3)}`);
        return `<div class="job-card" data-job="${escapeHtml(j.id)}" data-updated="${escapeHtml(j.updated_at || "")}">
          <div class="job-head">
            <div>
              <strong>${typeLabel}</strong>
              <span class="badge ${badge}">${escapeHtml(statusLabel)}</span>
              <span class="badge">Q${j.quality_mode}</span>
            </div>
            <div class="flex" style="gap:6px;flex-wrap:wrap">
              ${
                j.status === "paused_spend_cap"
                  ? `<button class="btn btn-primary btn-sm" data-continue-cap="${escapeHtml(j.id)}" type="button">Continue past cap</button>
                     <button class="btn btn-secondary btn-sm" data-cancel="${escapeHtml(j.id)}" type="button">Cancel job</button>`
                  : j.status === "pending" || j.status === "running"
                    ? `<button class="btn btn-secondary btn-sm" data-cancel="${escapeHtml(j.id)}" type="button">Cancel</button>`
                    : ""
              }
            </div>
          </div>
          <div class="progress-bar"><i style="width:${pct}%"></i></div>
          <p class="hint mb-0 job-counters">
            Batches <strong class="job-batches">${j.completed_batches}</strong>/${j.total_batches}
            · size ${j.batch_size}
            · items <strong class="job-items">${j.items_processed}</strong>
            · ETA ${escapeHtml(j.eta_human || "—")}
            · updated ${escapeHtml(updated)}
            ${costBits.length ? ` · ${costBits.join(" · ")}` : ""}
          </p>
          <p class="hint mb-0 job-progress-msg"><strong>${escapeHtml(j.progress_message || "…")}</strong></p>
          ${_jobResultBanner(j)}
        </div>`;
      })
      .join("");
    $("jobsList").querySelectorAll("[data-cancel]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/t/${state.tenantSlug}/jobs/${btn.dataset.cancel}/cancel`, {
            method: "POST",
          });
          toast("Job cancel requested");
          state._jobsRenderKey = "";
          await loadJobs();
        } catch (e) {
          toast(e.message, "err");
        }
      };
    });
    $("jobsList").querySelectorAll("[data-continue-cap]").forEach((btn) => {
      btn.onclick = async () => {
        const ok = window.confirm(
          "This job is over the $35-per-1,000-gold spend trajectory.\n\n" +
            "Continue remaining batches anyway?\n" +
            "You will be charged for further OpenRouter + Apify usage."
        );
        if (!ok) {
          toast("Still paused — no further spend until you continue or cancel");
          return;
        }
        try {
          await api(
            `/api/t/${state.tenantSlug}/jobs/${btn.dataset.continueCap}/continue-past-cap`,
            {
              method: "POST",
              body: JSON.stringify({ confirm: true }),
            }
          );
          toast("Spend-cap consent recorded — job resuming");
          state._jobsRenderKey = "";
          await loadJobs();
        } catch (e) {
          toast(e.message, "err");
        }
      };
    });
  } catch (e) {
    $("jobsList").innerHTML = `<p class="status-line error">${escapeHtml(e.message)}</p>`;
  } finally {
    state._jobsLoading = false;
  }
}

function startJobPolling() {
  if (state.jobPollTimer) clearInterval(state.jobPollTimer);
  state.jobPollTimer = setInterval(() => {
    if (!state.token || !state.tenantSlug) return;
    // Always refresh jobs + library counts so healthy jobs never look dead
    loadJobs().catch(() => {});
    if ($("tab-library") && !$("tab-library").classList.contains("hidden")) {
      loadLibrary().catch(() => {});
    }
  }, 2000);
}

async function startPipelineJob() {
  if (!$("pipeBatchSize")) {
    toast("Start mining from Riu — setup pages were removed.", "err");
    return;
  }
  $("runStatus").textContent = "Queueing mining job…";
  $("runStatus").className = "status-line";
  try {
    const r = await api(`/api/t/${state.tenantSlug}/jobs/pipeline`, {
      method: "POST",
      body: JSON.stringify({
        quality_mode: parseInt($("pipeQuality").value, 10),
        batch_size: parseInt($("pipeBatchSize").value, 10),
        total_batches: parseInt($("pipeBatches").value, 10),
        auto_continue: $("pipeAuto").checked,
      }),
    });
    $("runStatus").textContent = r.message || "Job queued.";
    $("runStatus").className = "status-line ok";
    toast("Mining job started — safe to sign out");
    await loadJobs();
  } catch (e) {
    $("runStatus").textContent = e.message;
    $("runStatus").className = "status-line error";
  }
}

// ── Library (user-owned gold + synthetic) ───────────────────────────

function updateSynthHint() {
  const g = parseInt($("goldTarget").value || "0", 10);
  const v = parseInt($("varPerGold").value || "0", 10);
  const total = (isFinite(g) ? g : 0) * (isFinite(v) ? v : 0);
  $("synthTargetHint").textContent = `Synthesized goal: ${total.toLocaleString()} (gold × variations)`;
}

function renderParamPicker(available, selected) {
  const sel = new Set(selected || []);
  $("paramPicker").innerHTML = (available || [])
    .map(
      (p) => `
    <label class="item" style="cursor:pointer;align-items:center">
      <div class="flex" style="width:100%">
        <input type="checkbox" data-param="${escapeHtml(p.key)}" ${sel.has(p.key) ? "checked" : ""} style="width:auto;margin:0" />
        <div>
          <h4 style="margin:0">${escapeHtml(p.label)}</h4>
          <p>${escapeHtml(p.description || "")}</p>
        </div>
      </div>
    </label>`
    )
    .join("");
}

function selectedParams() {
  return [...$("paramPicker").querySelectorAll("input[data-param]:checked")].map(
    (el) => el.dataset.param
  );
}

async function loadCorpus() {
  if (!$("corpusList") || !state.tenantSlug) return;
  try {
    const data = await api(`/api/t/${state.tenantSlug}/library/corpus`);
    const items = data.items || [];
    if (!items.length) {
      $("corpusList").innerHTML =
        `<p class="hint mb-0">No corpus docs yet — paste a FAQ or add a support URL for niche domains.</p>`;
      return;
    }
    $("corpusList").innerHTML = items
      .map(
        (d) => `<div class="item">
        <div>
          <h4>${escapeHtml(d.title || "Document")}</h4>
          <p class="hint mb-0">${escapeHtml(d.category || "")} · ${d.content_length || 0} chars
          ${d.url ? ` · <a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">source</a>` : ""}</p>
          <p>${escapeHtml((d.content_text || "").slice(0, 160))}</p>
        </div>
        <button class="btn btn-ghost btn-sm" data-corpus-del="${escapeHtml(d.id)}" type="button">Remove</button>
      </div>`
      )
      .join("");
    $("corpusList").querySelectorAll("[data-corpus-del]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/t/${state.tenantSlug}/library/corpus/${btn.dataset.corpusDel}`, {
            method: "DELETE",
          });
          toast("Corpus doc removed");
          await loadCorpus();
        } catch (e) {
          toast(e.message, "err");
        }
      };
    });
  } catch (e) {
    $("corpusList").innerHTML = `<p class="status-line error">${escapeHtml(e.message)}</p>`;
  }
}

async function loadLibrary() {
  if (!$("libraryProgress")) return;
  try {
    const [stats, settings, gold, synth] = await Promise.all([
      api(`/api/t/${state.tenantSlug}/library/stats`),
      api(`/api/t/${state.tenantSlug}/library/settings`),
      api(`/api/t/${state.tenantSlug}/library/gold?limit=20`),
      api(`/api/t/${state.tenantSlug}/library/synthetic?limit=20`),
    ]);

    $("goldTarget").value = settings.gold_target_count;
    $("varPerGold").value = settings.variations_per_gold;
    $("autoPromote").checked = !!settings.auto_promote_approved;
    updateSynthHint();
    renderParamPicker(settings.available_parameters, settings.vary_parameters);

    const seedN = stats.gold_seed_count || 0;
    const userN =
      stats.gold_user_count != null ? stats.gold_user_count : stats.gold_count;
    const uploadN = stats.gold_user_upload_count || 0;
    const matN = stats.gold_user_material_count || 0;
    $("libraryProgress").innerHTML = `
      <div class="stat-card tone-accent">
        <div class="label">Your generated gold</div>
        <div class="value">${Number(userN).toLocaleString()}</div>
        <div class="sub">of ${stats.gold_target.toLocaleString()} goal · ${stats.gold_progress_pct}% · kept forever${
          seedN
            ? ` · ${seedN} seed/demo row(s) listed separately`
            : ""
        }${
          uploadN
            ? ` · ${uploadN} labeled upload(s)`
            : ""
        }${
          matN
            ? ` · ${matN} from materials`
            : ""
        }</div>
      </div>
      <div class="stat-card tone-ok">
        <div class="label">Synthesized in your account</div>
        <div class="value">${stats.synthetic_count.toLocaleString()}</div>
        <div class="sub">of ${stats.synthetic_target.toLocaleString()} goal · ${stats.synthetic_progress_pct}% · kept forever</div>
      </div>`;
    if ($("userUploadCountHint")) {
      const bits = [];
      if (uploadN) bits.push(`${uploadN} labeled gold upload(s)`);
      if (matN) bits.push(`${matN} material-converted row(s)`);
      $("userUploadCountHint").textContent = bits.length
        ? `${bits.join(" · ")}. Export my uploads / Export materials / Export all trainable for Double Helix.`
        : "No personal uploads yet. Use the zip uploaders above or finish Riu setup.";
    }

    if (!gold.items.length) {
      $("goldList").innerHTML = `<div class="empty"><div class="icon">🥇</div>No gold yet. Run helpers, then “Save vetted data as gold”.</div>`;
    } else {
      const seedItems = gold.items.filter((g) => g.is_seed);
      const userItems = gold.items.filter((g) => !g.is_seed);
      const row = (g) => {
        const seed = !!g.is_seed;
        const rejected = (g.verification_status || "").toLowerCase() === "rejected";
        const origin = g.origin_label || (seed ? "Seed / demo" : "Your generated data");
        const rejReason =
          g.rejection_reason ||
          (Array.isArray(g.rejection_reasons) && g.rejection_reasons.length
            ? g.rejection_reasons.join("; ")
            : "");
        const badgeClass = rejected ? "err" : seed ? "warn" : "ok";
        const badgeText = rejected
          ? "Rejected"
          : seed
            ? "Seed / demo"
            : origin.toLowerCase().includes("corpus")
              ? "Corpus"
              : "Your data";
        return `<div class="item ${seed ? "item-seed" : "item-user"}${rejected ? " item-rejected" : ""}">
          <div>
            <h4>${escapeHtml(g.topic)} ${
              seed
                ? `<span class="badge warn" title="Bootstrap/demo training row">Seed / demo</span>`
                : `<span class="badge ok" title="${escapeHtml(origin)}">${escapeHtml(badgeText)}</span>`
            }${
              rejected
                ? ` <span class="badge err" title="${escapeHtml(rejReason || "Failed quality gates")}">Quality reject</span>`
                : ""
            }</h4>
            <p><strong>Q:</strong> ${escapeHtml((g.input || "").slice(0, 120))}</p>
            <p><strong>A:</strong> ${escapeHtml((g.output || "").slice(0, 120))}</p>
            <p class="hint mb-0 origin-label">${escapeHtml(origin)}${
              g.verification_status ? ` · ${escapeHtml(g.verification_status)}` : ""
            }</p>
            ${
              rejected && rejReason
                ? `<p class="hint mb-0" style="color:var(--color-danger, #b91c1c)"><strong>Rejection reason:</strong> ${escapeHtml(rejReason)}</p>`
                : ""
            }
          </div>
          <span class="badge ${badgeClass}">${escapeHtml(badgeText)}</span>
        </div>`;
      };
      $("goldList").innerHTML =
        (seedItems.length
          ? `<div class="banner warn" style="margin-bottom:12px"><strong>Seed / demo data</strong> — not created by your latest mining run (${seedItems.length}).</div>`
          : "") +
        (userItems.length
          ? `<div class="section-label">Your generated data (${userItems.length})</div>`
          : `<div class="banner warn" style="margin-bottom:12px">No generated gold yet — only seed/demo or empty. A finished job with 0 new gold will not change this list.</div>`) +
        [...userItems, ...seedItems].map(row).join("") +
        `<p class="hint">Showing ${gold.items.length} of ${gold.total}</p>`;
    }

    if (!synth.items.length) {
      $("synthList").innerHTML = `<div class="empty"><div class="icon">✨</div>No variations yet. Choose parameters and press “Create variations”.</div>`;
    } else {
      $("synthList").innerHTML = synth.items
        .map(
          (s) => `<div class="item">
          <div>
            <h4>${escapeHtml(s.topic)} · var ${s.variation_index}</h4>
            <p>${escapeHtml((s.input || "").slice(0, 140))}</p>
            <p class="hint">${escapeHtml(JSON.stringify(s.varied_parameters || {}))}</p>
          </div>
          <span class="badge accent">synthetic</span>
        </div>`
        )
        .join("") + `<p class="hint">Showing ${synth.items.length} of ${synth.total}</p>`;
    }
    await loadCorpus();
  } catch (e) {
    $("libraryProgress").innerHTML = `<p class="status-line error">${escapeHtml(e.message)}</p>`;
  }
}

async function saveScope() {
  $("scopeStatus").textContent = "Saving…";
  $("scopeStatus").className = "status-line";
  try {
    await api(`/api/t/${state.tenantSlug}/library/settings`, {
      method: "PUT",
      body: JSON.stringify({
        gold_target_count: parseInt($("goldTarget").value, 10),
        variations_per_gold: parseInt($("varPerGold").value, 10),
        vary_parameters: selectedParams(),
        auto_promote_approved: $("autoPromote").checked,
      }),
    });
    $("scopeStatus").textContent = "Goals saved.";
    $("scopeStatus").className = "status-line ok";
    toast("Collection goals saved");
    updateSynthHint();
    await loadLibrary();
  } catch (e) {
    $("scopeStatus").textContent = e.message;
    $("scopeStatus").className = "status-line error";
  }
}

async function promoteGold() {
  $("synthStatus").textContent = "Saving vetted examples into your gold library…";
  $("synthStatus").className = "status-line";
  try {
    const r = await api(`/api/t/${state.tenantSlug}/library/gold/promote`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    $("synthStatus").textContent = r.message || `Saved ${r.promoted} gold rows.`;
    $("synthStatus").className = "status-line ok";
    toast(r.message || "Gold saved to your account");
    await loadLibrary();
  } catch (e) {
    $("synthStatus").textContent = e.message;
    $("synthStatus").className = "status-line error";
  }
}

async function synthesize() {
  $("synthStatus").innerHTML = `<span class="loading-dot"></span>Queueing synthesis job…`;
  $("synthStatus").className = "status-line";
  $("synthesizeBtn").disabled = true;
  try {
    await api(`/api/t/${state.tenantSlug}/library/settings`, {
      method: "PUT",
      body: JSON.stringify({
        vary_parameters: selectedParams(),
        variations_per_gold: parseInt($("varPerGold").value, 10),
      }),
    });
    const r = await api(`/api/t/${state.tenantSlug}/jobs/synthesis`, {
      method: "POST",
      body: JSON.stringify({
        quality_mode: parseInt($("synthQuality").value, 10),
        batch_size: Math.min(10, parseInt($("synthMaxGolds").value, 10) || 5),
        total_batches: parseInt($("synthBatches").value, 10) || 1,
        auto_continue: $("synthAuto") ? $("synthAuto").checked : true,
        variations_per_gold: parseInt($("varPerGold").value, 10) || 4,
        parameters: selectedParams(),
      }),
    });
    $("synthStatus").textContent = r.message || "Synthesis job queued.";
    $("synthStatus").className = "status-line ok";
    toast("Synthesis job started — safe to sign out");
    await loadJobs();
    await loadLibrary();
  } catch (e) {
    $("synthStatus").textContent = e.message;
    $("synthStatus").className = "status-line error";
    toast(e.message, "err");
  } finally {
    $("synthesizeBtn").disabled = false;
  }
}

async function exportLibrary(kind, fmt) {
  try {
    const format = fmt || ($("exportChatFormat") && $("exportChatFormat").checked ? "chat" : "jsonl");
    const res = await fetch(
      `/api/t/${state.tenantSlug}/library/export?kind=${encodeURIComponent(kind)}&format=${encodeURIComponent(format)}`,
      { headers: { Authorization: `Bearer ${state.token}` } }
    );
    if (!res.ok) throw new Error("Download failed");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `helix_${state.tenantSlug}_${kind}.jsonl`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast("Download started");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function uploadGoldZip(fileInput, statusEl, { viaRiu = false, materials = false } = {}) {
  if (!state.tenantSlug) throw new Error("Pick a workspace first");
  const file = fileInput?.files?.[0];
  if (!file) throw new Error("Choose a .zip file first");
  if (!/\.zip$/i.test(file.name)) throw new Error("File must be a .zip");
  if (statusEl) {
    statusEl.textContent = materials
      ? "Uploading materials & converting to trainable format…"
      : "Uploading & converting to gold format…";
    statusEl.className = "status-line";
  }
  const fd = new FormData();
  fd.append("file", file);
  if (!materials) fd.append("topic", "user_upload");
  let path;
  if (materials) {
    path = viaRiu
      ? `/api/t/${state.tenantSlug}/riu/upload-materials-zip`
      : `/api/t/${state.tenantSlug}/library/gold/upload-materials-zip`;
  } else {
    path = viaRiu
      ? `/api/t/${state.tenantSlug}/riu/upload-gold-zip`
      : `/api/t/${state.tenantSlug}/library/gold/upload-zip`;
  }
  const res = await fetch(path, {
    method: "POST",
    headers: { Authorization: `Bearer ${state.token}` },
    body: fd,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || "Upload failed");
  if (statusEl) {
    statusEl.textContent = data.message || `Saved ${data.created || 0} rows.`;
    statusEl.className = "status-line ok";
  }
  toast(`Saved ${data.created || 0} trainable example(s)`);
  if (fileInput) fileInput.value = "";
  if (viaRiu && data.session) {
    renderRiuMessages(data.session.messages || []);
    renderRiuState(data.session.state || {});
    updateRiuUploadPanel(data.session);
  }
  loadLibrary().catch(() => {});
  return data;
}

function updateRiuUploadPanel(session) {
  const goldPanel = $("riuUploadPanel");
  const matPanel = $("riuMaterialsPanel");
  const showGold =
    !!session?.show_gold_zip_upload ||
    !!session?.state?.own_data_awaiting_upload ||
    (session?.phase === "own_data" && !!session?.state?.has_own_data);
  const showMat =
    !!session?.show_materials_zip_upload ||
    !!session?.state?.materials_awaiting_upload ||
    (session?.phase === "materials" && !!session?.state?.has_materials);
  if (goldPanel) goldPanel.classList.toggle("hidden", !showGold);
  if (matPanel) matPanel.classList.toggle("hidden", !showMat);
}

// ── Home ────────────────────────────────────────────────────────────

async function refreshDashboard() {
  const dash = await api(`/api/t/${state.tenantSlug}/dashboard`);
  const m = dash.metrics || {};
  const budget = m.budget || {};
  const spent = budget.spent_usd || 0;
  const orSpent = budget.openrouter_usd != null ? budget.openrouter_usd : spent;
  const apSpent = budget.apify_usd != null ? budget.apify_usd : 0;
  const limit = budget.monthly_usd || 0;
  const openEsc = m.open_escalations ?? 0;
  const openCon = m.open_contradictions ?? 0;

  $("statsRow").innerHTML = `
    <div class="stat-card tone-warn">
      <div class="label">Needs your decision</div>
      <div class="value">${openEsc}</div>
      <div class="sub">${openEsc ? "Open questions waiting" : "You’re all caught up"}</div>
    </div>
    <div class="stat-card tone-accent">
      <div class="label">Open quality checks</div>
      <div class="value">${openCon}</div>
      <div class="sub">Conflicting facts to review later</div>
    </div>
    <div class="stat-card tone-ok">
      <div class="label">Usage this month (all-in)</div>
      <div class="value">$${Number(spent).toFixed(2)}</div>
      <div class="sub">OpenRouter $${Number(orSpent).toFixed(2)} · Apify $${Number(apSpent).toFixed(2)} · budget $${Number(limit).toFixed(0)}</div>
    </div>
  `;

  const esc = (dash.escalations && dash.escalations.escalations) || [];
  if (!esc.length) {
    $("escalations").innerHTML = `
      <div class="empty">
        <div class="icon">✓</div>
        Nothing waiting — great job.
      </div>`;
  } else {
    $("escalations").innerHTML = `<div class="item-list">
      ${esc
        .map((e) => {
          const p = e.payload || {};
          const helper = FRIENDLY_AGENTS[e.source_agent]?.title || e.source_agent;
          const needsInput = !!p.needs_input;
          const label = p.action_label || (needsInput ? "Save decision" : "Acknowledge");
          const msg =
            p.message || p.reason || p.description || e.kind || "Needs your attention";
          // Surface candidate fact content when present
          let detail = "";
          if (e.kind === "low_extraction_confidence" || p.value) {
            const bits = [
              p.entity ? `Entity: ${p.entity}` : "",
              p.fact_type ? `Type: ${p.fact_type}` : "",
              p.value ? `Value: ${p.value}` : "",
              p.citation ? `Citation: ${p.citation}` : "",
              p.extraction_confidence != null
                ? `Confidence: ${p.extraction_confidence}`
                : "",
            ].filter(Boolean);
            if (bits.length) {
              detail = `<div class="hint" style="margin-top:6px;white-space:pre-wrap">${bits
                .map((b) => escapeHtml(b))
                .join("<br>")}</div>`;
            }
          }
          const inputBlock = needsInput
            ? `<label class="field-help" for="esc-in-${escapeHtml(e.id)}">${escapeHtml(
                p.prompt || "Your decision"
              )}</label>
               <input id="esc-in-${escapeHtml(e.id)}" class="esc-input" type="text" placeholder="Type your decision…" style="width:100%;margin:4px 0 8px" />`
            : `<p class="hint mb-0" style="margin-top:4px">No free-text answer required — acknowledge to clear.</p>`;
          return `<div class="item" data-esc-card="${escapeHtml(e.id)}">
            <div style="flex:1;min-width:0">
              <h4>${escapeHtml(helper)} · <span class="badge">${escapeHtml(
                e.kind || "note"
              )}</span></h4>
              <p>${escapeHtml(msg)}</p>
              ${detail}
              ${inputBlock}
            </div>
            <button class="btn btn-secondary btn-sm" data-esc="${escapeHtml(
              e.id
            )}" data-needs-input="${needsInput ? "1" : "0"}" type="button">${escapeHtml(
              label
            )}</button>
          </div>`;
        })
        .join("")}
    </div>`;
    $("escalations").querySelectorAll("button[data-esc]").forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.esc;
        const needs = btn.dataset.needsInput === "1";
        let decision = "acknowledged";
        if (needs) {
          const inp = document.getElementById(`esc-in-${id}`);
          decision = (inp && inp.value.trim()) || "";
          if (!decision) {
            toast("Please enter a decision before saving", "err");
            return;
          }
        }
        try {
          await api(`/api/t/${state.tenantSlug}/escalations/${id}/resolve`, {
            method: "POST",
            body: JSON.stringify({ decision }),
          });
          toast(needs ? "Saved your decision" : "Acknowledged");
          await refreshDashboard();
        } catch (err) {
          toast(err.message, "err");
        }
      };
    });
  }

  const runs = await api(`/api/t/${state.tenantSlug}/runs?limit=12`);
  if (!runs.length) {
    $("runs").innerHTML = `
      <div class="empty">
        <div class="icon">✨</div>
        No helper runs yet. Go to <strong>AI helpers</strong> and press “Run everything”.
      </div>`;
    return;
  }

  $("runs").innerHTML = `<div class="table-wrap"><table class="table">
    <thead><tr><th>Helper</th><th>Status</th><th>Cost</th><th>Summary</th></tr></thead>
    <tbody>
      ${runs
        .map((r) => {
          const title = FRIENDLY_AGENTS[r.agent]?.title || r.agent;
          const status =
            r.status === "completed"
              ? "Done"
              : r.status === "error"
                ? "Failed"
                : r.status;
          const badge =
            r.status === "completed" ? "ok" : r.status === "error" ? "err" : "warn";
          const preview = (r.output_preview || r.error || "—").slice(0, 100);
          return `<tr>
            <td><strong>${escapeHtml(title)}</strong></td>
            <td><span class="badge ${badge}">${escapeHtml(status)}</span></td>
            <td title="${escapeHtml(r.cost_source || "")}">$${(r.cost_usd || 0).toFixed(4)}${r.cost_source === "estimate" ? " ~" : ""}</td>
            <td>${escapeHtml(preview)}</td>
          </tr>`;
        })
        .join("")}
    </tbody>
  </table></div>`;
}

// ── Plan ────────────────────────────────────────────────────────────

function fillBriefForm(b) {
  if (!b) {
    if ($("briefSummary")) {
      $("briefSummary").innerHTML = `
      <div class="project-badge">No setup yet</div>
      <h2>Talk to Riu</h2>
      <p class="hint">Riu is the only setup path. She collects role, examples, edge cases, and cost — then starts mining.</p>
      <button class="btn btn-primary" type="button" data-goto="riu">Open Riu</button>`;
      $("briefSummary").querySelector("[data-goto]")?.addEventListener("click", () => goTab("riu"));
    }
    return;
  }
  if (!$("briefName")) {
    if ($("briefSummary")) {
      $("briefSummary").innerHTML = `
      <div class="project-badge">${escapeHtml(b.name || "Active setup")}</div>
      <h2>${escapeHtml(b.domain || b.name || "Your project")}</h2>
      <p class="hint">${escapeHtml(b.mission || "Set up through Riu.")}</p>
      <button class="btn btn-secondary" type="button" data-goto="riu">Continue with Riu</button>`;
      $("briefSummary").querySelector("[data-goto]")?.addEventListener("click", () => goTab("riu"));
    }
    return;
  }
  $("briefSlug").value = b.slug || "default";
  $("briefName").value = b.name || "";
  $("briefDomain").value = b.domain || "";
  $("briefMission").value = b.mission || "";
  $("briefQuestions").value = (b.research_questions || []).join("\n");
  $("briefCategories").value = (b.categories || []).join(", ");
  $("briefSources").value = (b.sources || []).join(", ");
  $("briefTargets").value = formatTargets(b.phase_targets || {});
  $("briefMetrics").value = formatMetrics(b.success_metrics || []);
  $("briefTopics").value = (b.topic_keys || []).join(", ");
  $("briefInstructions").value = b.agent_instructions || "";
  $("briefOutput").value = b.output_notes || "";

  $("briefSummary").innerHTML = `
    <div class="project-badge">● Active plan</div>
    <h2>${escapeHtml(b.name || "Untitled project")}</h2>
    <p class="hint">${escapeHtml(b.domain || "No domain set yet.")}</p>
    <dl class="kv">
      <div class="kv-row"><dt>Goal</dt><dd>${escapeHtml(b.mission || "—")}</dd></div>
      <div class="kv-row"><dt>Topics</dt><dd>${escapeHtml((b.categories || []).join(", ") || "—")}</dd></div>
      <div class="kv-row"><dt>Sources</dt><dd>${escapeHtml((b.sources || []).join(", ") || "—")}</dd></div>
      <div class="kv-row"><dt>Formats</dt><dd>${escapeHtml((b.topic_keys || []).join(", ") || "—")}</dd></div>
    </dl>
    <div class="flex" style="margin-top:1rem">
      <button class="btn btn-secondary btn-sm" type="button" data-goto="plan">Edit plan</button>
      <button class="btn btn-primary btn-sm" type="button" data-goto="helpers">Run helpers</button>
    </div>`;
  $("briefSummary").querySelectorAll("[data-goto]").forEach((btn) => {
    btn.addEventListener("click", () => goTab(btn.dataset.goto));
  });
}

async function loadBrief() {
  const data = await api(`/api/t/${state.tenantSlug}/projects/active`);
  state.brief = data.brief;
  fillBriefForm(state.brief);
}

async function saveBrief() {
  $("briefStatus").textContent = "Saving…";
  $("briefStatus").className = "status-line";
  try {
    const name = $("briefName").value.trim() || "My training project";
    const payload = {
      slug: $("briefSlug").value.trim() || "default",
      name,
      domain: $("briefDomain").value.trim(),
      mission: $("briefMission").value.trim(),
      research_questions: linesToList($("briefQuestions").value),
      categories: csvToList($("briefCategories").value),
      sources: csvToList($("briefSources").value),
      phase_targets: parseTargets($("briefTargets").value),
      success_metrics: parseMetrics($("briefMetrics").value),
      topic_keys: csvToList($("briefTopics").value),
      agent_instructions: $("briefInstructions").value.trim(),
      output_notes: $("briefOutput").value.trim(),
      is_active: true,
    };

    const projects = await api(`/api/t/${state.tenantSlug}/projects`);
    const existing = projects.find((p) => p.slug === payload.slug);
    if (existing) {
      const { slug, ...update } = payload;
      await api(`/api/t/${state.tenantSlug}/projects/${payload.slug}`, {
        method: "PUT",
        body: JSON.stringify(update),
      });
      await api(`/api/t/${state.tenantSlug}/projects/${payload.slug}/activate`, {
        method: "POST",
      });
    } else {
      await api(`/api/t/${state.tenantSlug}/projects`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
    }
    $("briefStatus").textContent = "Plan saved.";
    $("briefStatus").className = "status-line ok";
    if ($("briefSavedChip")) $("briefSavedChip").textContent = "Saved just now";
    toast("Your plan is saved and active");
    await loadBrief();
  } catch (e) {
    $("briefStatus").textContent = e.message;
    $("briefStatus").className = "status-line error";
    toast(e.message, "err");
  }
}

// ── Formats ─────────────────────────────────────────────────────────

function clearSchemaForm() {
  $("schemaFormTitle").textContent = "New format";
  $("schemaTopic").value = "";
  $("schemaTopic").readOnly = false;
  $("schemaDisplay").value = "";
  $("schemaDesc").value = "";
  $("schemaSampleInput").value = "";
  $("schemaSampleOutput").value = "";
  $("schemaSampleRationale").value = "";
  $("schemaFormat").value = "jsonl";
  $("schemaJson").value = JSON.stringify(DEFAULT_SCHEMA, null, 2);
  $("schemaStatus").textContent = "";
  $("schemaStatus").className = "status-line";
}

function editSchema(s) {
  $("schemaFormTitle").textContent = `Edit: ${s.display_name || s.topic}`;
  $("schemaTopic").value = s.topic;
  $("schemaTopic").readOnly = true;
  $("schemaDisplay").value = s.display_name || "";
  $("schemaDesc").value = s.description || "";
  const sample = s.sample_row || {};
  $("schemaSampleInput").value = sample.input || sample.question || "";
  $("schemaSampleOutput").value = sample.output || sample.answer || "";
  $("schemaSampleRationale").value = sample.rationale || "";
  $("schemaFormat").value = s.export_format || "jsonl";
  $("schemaJson").value = JSON.stringify(s.schema || DEFAULT_SCHEMA, null, 2);
  goTab("formats");
}

async function loadSchemas() {
  state.schemas = await api(`/api/t/${state.tenantSlug}/schemas`);
  const active = state.schemas.filter((s) => s.is_active !== false);
  if (!active.length) {
    $("schemaList").innerHTML = `
      <div class="empty">
        <div class="icon">📝</div>
        No formats yet. Create one on the right — start with a sample question and answer.
      </div>`;
    return;
  }
  $("schemaList").innerHTML = active
    .map(
      (s) => `
    <div class="item">
      <div>
        <h4>${escapeHtml(s.display_name || s.topic)}</h4>
        <p>${escapeHtml(s.description || "Training example format")}</p>
        <div style="margin-top:.4rem"><span class="badge accent">${escapeHtml(s.topic)}</span></div>
      </div>
      <button class="btn btn-secondary btn-sm" data-topic="${escapeHtml(s.topic)}" type="button">Edit</button>
    </div>`
    )
    .join("");
  $("schemaList").querySelectorAll("button[data-topic]").forEach((btn) => {
    btn.onclick = () => {
      const s = state.schemas.find((x) => x.topic === btn.dataset.topic);
      if (s) editSchema(s);
    };
  });
}

async function saveSchema() {
  $("schemaStatus").textContent = "Saving…";
  $("schemaStatus").className = "status-line";
  try {
    let topic = $("schemaTopic").value.trim();
    const display = $("schemaDisplay").value.trim() || topic || "Untitled format";
    if (!topic) topic = slugify(display);
    $("schemaTopic").value = topic;

    let jsonSchema;
    try {
      jsonSchema = JSON.parse($("schemaJson").value || "{}");
    } catch {
      jsonSchema = DEFAULT_SCHEMA;
    }

    const sample_row = {
      input: $("schemaSampleInput").value.trim(),
      output: $("schemaSampleOutput").value.trim(),
      rationale: $("schemaSampleRationale").value.trim(),
      difficulty: "canonical",
      is_negative: false,
    };

    const payload = {
      topic,
      display_name: display,
      description: $("schemaDesc").value.trim(),
      json_schema: jsonSchema,
      sample_row,
      export_format: $("schemaFormat").value,
      is_active: true,
    };

    const exists = state.schemas.some((s) => s.topic === topic);
    if (exists) {
      const { topic: _t, ...update } = payload;
      await api(`/api/t/${state.tenantSlug}/schemas/${topic}`, {
        method: "PUT",
        body: JSON.stringify(update),
      });
    } else {
      await api(`/api/t/${state.tenantSlug}/schemas`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
    }
    $("schemaStatus").textContent = "Format saved.";
    $("schemaStatus").className = "status-line ok";
    toast("Example format saved");
    await loadSchemas();
  } catch (e) {
    $("schemaStatus").textContent = e.message;
    $("schemaStatus").className = "status-line error";
    toast(e.message, "err");
  }
}

// ── Helpers (agents) ────────────────────────────────────────────────

function renderAgents() {
  const list = $("agentList");
  list.innerHTML = "";
  state.agents.forEach((a) => {
    const meta = FRIENDLY_AGENTS[a.key] || {
      title: a.name,
      blurb: a.goal,
      role: a.role,
    };
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "agent-card" + (state.selectedAgent === a.key ? " selected" : "");
    btn.dataset.key = a.key;
    btn.innerHTML = `
      <div class="role">${escapeHtml(meta.role)}</div>
      <strong>${escapeHtml(meta.title)}</strong>
      <p>${escapeHtml(meta.blurb)}</p>`;
    btn.onclick = () => {
      state.selectedAgent = a.key;
      $("runAgent").value = a.key;
      renderAgents();
    };
    list.appendChild(btn);
  });
  $("runAgent").value = state.selectedAgent;
}

async function runAgent() {
  const agent = state.selectedAgent || $("runAgent").value;
  const message = $("runMessage").value.trim() || null;
  const title = FRIENDLY_AGENTS[agent]?.title || agent;
  $("runStatus").innerHTML = `<span class="loading-dot"></span>Running ${escapeHtml(title)}… this may take a minute.`;
  $("runStatus").className = "status-line";
  $("runOutput").classList.add("hidden");
  $("runBtn").disabled = true;
  $("pipelineBtn").disabled = true;
  try {
    const result = await api(`/api/t/${state.tenantSlug}/agents/${agent}/run`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    $("runStatus").textContent = `Finished · about $${(result.cost_usd || 0).toFixed(3)} used`;
    $("runStatus").className = "status-line ok";
    $("runOutput").textContent =
      result.output || "Done. Open Home to see recent activity.";
    $("runOutput").classList.remove("hidden");
    toast(`${title} finished`);
    await refreshDashboard();
  } catch (e) {
    $("runStatus").textContent = e.message;
    $("runStatus").className = "status-line error";
    toast(e.message, "err");
  } finally {
    $("runBtn").disabled = false;
    $("pipelineBtn").disabled = false;
  }
}

async function runPipeline() {
  if (
    !confirm(
      "Run the full collection process?\n\nThis uses all AI helpers in order and may take several minutes and use API budget."
    )
  ) {
    return;
  }
  $("runStatus").innerHTML = `<span class="loading-dot"></span>Running full process… please keep this tab open.`;
  $("runStatus").className = "status-line";
  $("runOutput").classList.add("hidden");
  $("runBtn").disabled = true;
  $("pipelineBtn").disabled = true;
  try {
    const result = await api(`/api/t/${state.tenantSlug}/pipeline/run`, {
      method: "POST",
      body: JSON.stringify({
        message: $("runMessage").value.trim() || null,
      }),
    });
    const lines = (result.results || []).map((r) => {
      const title = FRIENDLY_AGENTS[r.agent]?.title || r.agent;
      if (r.status === "error") return `• ${title}: failed — ${r.error || "error"}`;
      return `• ${title}: done ($${(r.cost_usd || 0).toFixed(3)})`;
    });
    $("runStatus").textContent = `Full process finished (${(result.results || []).length} steps)`;
    $("runStatus").className = "status-line ok";
    $("runOutput").textContent = lines.join("\n") || "Completed.";
    $("runOutput").classList.remove("hidden");
    toast("Full process finished");
    await refreshDashboard();
  } catch (e) {
    $("runStatus").textContent = e.message;
    $("runStatus").className = "status-line error";
    toast(e.message, "err");
  } finally {
    $("runBtn").disabled = false;
    $("pipelineBtn").disabled = false;
  }
}

// ── Download ────────────────────────────────────────────────────────

async function loadDatasets() {
  const rows = await api(`/api/t/${state.tenantSlug}/datasets`);
  if (!rows.length) {
    $("datasetList").innerHTML = `
      <div class="empty">
        <div class="icon">📦</div>
        No saved versions yet. When you have approved examples, name a version and click “Save current examples”.
      </div>`;
    return;
  }
  $("datasetList").innerHTML = `<div class="table-wrap"><table class="table">
    <thead><tr><th>Version</th><th>Examples</th><th>Saved</th><th></th></tr></thead>
    <tbody>
      ${rows
        .map(
          (r) => `<tr>
        <td><strong>${escapeHtml(r.version)}</strong></td>
        <td>${r.example_count}</td>
        <td>${escapeHtml((r.created_at || "").replace("T", " ").slice(0, 16))}</td>
        <td class="flex">
          <button class="btn btn-primary btn-sm" data-dl="${escapeHtml(r.version)}" type="button">Download</button>
        </td>
      </tr>`
        )
        .join("")}
    </tbody>
  </table></div>`;
  $("datasetList").querySelectorAll("button[data-dl]").forEach((btn) => {
    btn.onclick = () => downloadExport(btn.dataset.dl, "jsonl");
  });
}

async function downloadExport(version, format) {
  $("exportStatus").textContent = "Preparing your file…";
  $("exportStatus").className = "status-line";
  try {
    const res = await fetch(
      `/api/t/${state.tenantSlug}/datasets/${encodeURIComponent(version)}/export?format=${format}`,
      { headers: { Authorization: `Bearer ${state.token}` } }
    );
    if (!res.ok) {
      const t = await res.text();
      throw new Error(friendlyError(t || res.statusText));
    }
    const blob = await res.blob();
    if (blob.size === 0) {
      $("exportStatus").textContent =
        "This file is empty — no approved examples yet. Run helpers and approve examples first.";
      $("exportStatus").className = "status-line error";
      toast("No examples to download yet", "err");
      return;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `helix_${state.tenantSlug}_${version}.jsonl`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    $("exportStatus").textContent = "Download started.";
    $("exportStatus").className = "status-line ok";
    toast("Download started");
  } catch (e) {
    $("exportStatus").textContent = e.message;
    $("exportStatus").className = "status-line error";
    toast(e.message, "err");
  }
}

async function snapshotPool() {
  const version =
    $("snapshotVersion").value.trim() ||
    `version_${new Date().toISOString().slice(0, 10)}`;
  $("exportStatus").textContent = "Saving…";
  $("exportStatus").className = "status-line";
  try {
    const r = await api(
      `/api/t/${state.tenantSlug}/datasets/snapshot?version=${encodeURIComponent(version)}`,
      { method: "POST" }
    );
    $("exportStatus").textContent = `Saved “${r.version}” with ${r.count} examples.`;
    $("exportStatus").className = "status-line ok";
    toast(`Saved ${r.count} examples as ${r.version}`);
    await loadDatasets();
  } catch (e) {
    $("exportStatus").textContent = e.message;
    $("exportStatus").className = "status-line error";
    toast(e.message, "err");
  }
}

// ── Riu conversational helper ───────────────────────────────────────

function renderMarkdownLite(text) {
  // minimal **bold** + newlines for Riu replies
  return escapeHtml(text || "")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}

function renderRiuState(state) {
  const el = $("riuStateCard");
  if (!el) return;
  const s = state || {};
  const rows = [
    ["Project", s.project_name],
    ["Domain", s.domain],
    ["Mission", s.mission],
    ["Topics", (s.categories || []).join(", ")],
    ["Format", s.format_name || s.topic_key],
    ["Gold goal", s.gold_target],
    ["Variations", s.variations_per_gold],
    ["Quality mode", s.quality_mode],
    [
      "Labeled uploads",
      s.own_data_uploaded
        ? `${s.own_data_count || 0} gold row(s)`
        : s.has_own_data
          ? "awaiting zip"
          : null,
    ],
    [
      "Materials",
      s.materials_uploaded
        ? `${s.materials_count || 0} converted row(s)`
        : s.has_materials
          ? "awaiting zip"
          : null,
    ],
  ].filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== "");
  if (!rows.length) {
    el.innerHTML = `<p class="hint mb-0">Nothing yet — start the chat.</p>`;
    return;
  }
  el.innerHTML = `<dl>${rows
    .map(
      ([k, v]) =>
        `<div><dt>${escapeHtml(String(k))}</dt><dd>${escapeHtml(String(v))}</dd></div>`
    )
    .join("")}</dl>`;
}

function renderRiuMessages(messages) {
  const box = $("riuMessages");
  if (!box) return;
  box.innerHTML = "";
  (messages || []).forEach((m) => {
    const div = document.createElement("div");
    const role = m.role === "user" ? "user" : "assistant";
    div.className = `riu-msg ${role}`;
    const who =
      role === "user" ? "You" : m.name || "Riu";
    div.innerHTML = `<span class="who">${escapeHtml(who)}</span><div class="body">${renderMarkdownLite(
      m.content || ""
    )}</div>`;
    if (m.progress != null && role === "assistant") {
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `Setup ~${m.progress}% · phase ${m.phase || "—"}`;
      div.appendChild(meta);
    }
    box.appendChild(div);
  });
  box.scrollTop = box.scrollHeight;
}

async function loadRiuSession() {
  if (!state.tenantSlug || !$("riuMessages")) return;
  $("riuStatus").textContent = "Loading Riu…";
  const data = await api(`/api/t/${state.tenantSlug}/riu/session`);
  renderRiuMessages(data.messages || []);
  renderRiuState(data.state || {});
  updateRiuUploadPanel(data);
  const last = (data.messages || []).filter((m) => m.role === "assistant").slice(-1)[0];
  const prog = last?.progress ?? 0;
  if ($("riuProgressBadge")) $("riuProgressBadge").textContent = `Setup ${prog}%`;
  $("riuStatus").textContent = data.status === "active" ? "Riu is ready" : `Session ${data.status}`;
  return data;
}

async function sendRiuMessage(text) {
  if (!state.tenantSlug) throw new Error("Pick a workspace first");
  const msg = (text || "").trim();
  if (!msg) return;
  $("riuInput").value = "";
  $("riuSendBtn").disabled = true;
  $("riuStatus").textContent = "Riu is thinking…";

  // optimistic user bubble
  const box = $("riuMessages");
  if (box) {
    const div = document.createElement("div");
    div.className = "riu-msg user";
    div.innerHTML = `<span class="who">You</span><div class="body">${renderMarkdownLite(msg)}</div>`;
    box.appendChild(div);
    const typing = document.createElement("div");
    typing.className = "riu-typing";
    typing.id = "riuTyping";
    typing.textContent = "Riu is typing…";
    box.appendChild(typing);
    box.scrollTop = box.scrollHeight;
  }

  try {
    const data = await api(`/api/t/${state.tenantSlug}/riu/message`, {
      method: "POST",
      body: JSON.stringify({ message: msg }),
    });
    $("riuTyping")?.remove();
    renderRiuMessages(data.messages || []);
    renderRiuState(data.state || {});
    updateRiuUploadPanel(data);
    if ($("riuProgressBadge")) {
      $("riuProgressBadge").textContent = `Setup ${data.progress ?? 0}%`;
    }
    const jobsStarted = (data.action_results || []).filter(
      (r) => r.ok && (r.action === "start_pipeline" || r.action === "start_synthesis")
    );
    if (jobsStarted.length) {
      toast("Riu started a job — it keeps running if you leave");
      loadJobs().catch(() => {});
      startJobPolling();
      // refresh plan/library views in background
      refreshAll().catch(() => {});
    } else if ((data.action_results || []).some((r) => r.ok && r.action === "save_plan")) {
      loadBrief().catch(() => {});
    } else if ((data.action_results || []).some((r) => r.ok && r.action === "save_goals")) {
      loadLibrary().catch(() => {});
    }
    $("riuStatus").textContent = data.used_llm ? "Riu (AI) replied" : "Riu replied";
  } catch (e) {
    $("riuTyping")?.remove();
    $("riuStatus").textContent = e.message;
    toast(e.message, "err");
  } finally {
    $("riuSendBtn").disabled = false;
    $("riuInput")?.focus();
  }
}

async function restartRiu() {
  if (!state.tenantSlug) return;
  $("riuStatus").textContent = "Starting new chat…";
  const data = await api(`/api/t/${state.tenantSlug}/riu/session`, { method: "POST" });
  renderRiuMessages(data.messages || []);
  renderRiuState(data.state || {});
  if ($("riuProgressBadge")) $("riuProgressBadge").textContent = "Setup 0%";
  $("riuStatus").textContent = "New chat with Riu";
  toast("New chat with Riu");
}

// ── Wire up ─────────────────────────────────────────────────────────

document.querySelectorAll(".nav-pill").forEach((btn) => {
  btn.addEventListener("click", () => goTab(btn.dataset.tab));
});

document.querySelectorAll("[data-goto]").forEach((btn) => {
  btn.addEventListener("click", () => goTab(btn.dataset.goto));
});

// steps on home use data-goto via delegation after render; also bind static ones
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-goto]");
  if (t && t.dataset.goto) goTab(t.dataset.goto);
});

$("loginBtn").onclick = login;
$("password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") login();
});
$("signupBtn").onclick = signup;
$("forgotBtn").onclick = forgotPassword;
$("setPasswordBtn").onclick = submitNewPassword;
$("showSignupBtn").onclick = () => setAuthMode("signup");
$("showForgotBtn").onclick = () => setAuthMode("forgot");
$("backToLoginFromSignup").onclick = () => setAuthMode("login");
$("backToLoginFromForgot").onclick = () => setAuthMode("login");
$("verifyDoneBtn").onclick = () => setAuthMode("login");
$("logoutBtn").onclick = logout;

parseAuthQuery();
$("refreshBtn").onclick = () =>
  refreshAll()
    .then(() => toast("Refreshed"))
    .catch((e) => toast(e.message, "err"));
$("tenantSelect").onchange = () =>
  refreshAll().catch((e) => toast(e.message, "err"));
if ($("saveBriefBtn")) $("saveBriefBtn").onclick = saveBrief;
if ($("saveSchemaBtn")) $("saveSchemaBtn").onclick = saveSchema;
if ($("newSchemaBtn")) $("newSchemaBtn").onclick = clearSchemaForm;
if ($("newSchemaBtn2")) $("newSchemaBtn2").onclick = clearSchemaForm;
if ($("snapshotBtn")) $("snapshotBtn").onclick = snapshotPool;
if ($("exportPoolBtn")) $("exportPoolBtn").onclick = () => downloadExport("approved-pool", "jsonl");
if ($("runBtn")) $("runBtn").onclick = runAgent;
if ($("pipelineBtn")) $("pipelineBtn").onclick = runPipeline;
if ($("goldTarget")) $("goldTarget").addEventListener("input", updateSynthHint);
if ($("varPerGold")) $("varPerGold").addEventListener("input", updateSynthHint);
if ($("saveScopeBtn")) $("saveScopeBtn").onclick = saveScope;
if ($("corpusPasteBtn")) {
  $("corpusPasteBtn").onclick = async () => {
    if (!$("corpusStatus")) return;
    $("corpusStatus").textContent = "Saving paste…";
    $("corpusStatus").className = "status-line";
    try {
      await api(`/api/t/${state.tenantSlug}/library/corpus/paste`, {
        method: "POST",
        body: JSON.stringify({
          title: $("corpusTitle")?.value || "Pasted document",
          content: $("corpusPaste")?.value || "",
          category: $("corpusCategory")?.value || "general",
        }),
      });
      $("corpusStatus").textContent = "Document added to your corpus.";
      $("corpusStatus").className = "status-line ok";
      if ($("corpusPaste")) $("corpusPaste").value = "";
      await loadCorpus();
      toast("Corpus document saved");
    } catch (e) {
      $("corpusStatus").textContent = e.message;
      $("corpusStatus").className = "status-line error";
    }
  };
}
if ($("corpusUrlBtn")) {
  $("corpusUrlBtn").onclick = async () => {
    if (!$("corpusStatus")) return;
    $("corpusStatus").textContent = "Fetching URL (may take a minute)…";
    $("corpusStatus").className = "status-line";
    try {
      await api(`/api/t/${state.tenantSlug}/library/corpus/url`, {
        method: "POST",
        body: JSON.stringify({
          url: $("corpusUrl")?.value || "",
          title: $("corpusTitle")?.value || "",
          category: $("corpusCategory")?.value || "general",
          fetch: true,
        }),
      });
      $("corpusStatus").textContent = "URL content added to your corpus.";
      $("corpusStatus").className = "status-line ok";
      await loadCorpus();
      toast("Corpus URL saved");
    } catch (e) {
      $("corpusStatus").textContent = e.message;
      $("corpusStatus").className = "status-line error";
    }
  };
}
if ($("qualityBackfillBtn")) {
  $("qualityBackfillBtn").onclick = async () => {
    if (!$("corpusStatus")) return;
    $("corpusStatus").textContent = "Re-checking historical gold quality…";
    $("corpusStatus").className = "status-line";
    try {
      const r = await api(`/api/t/${state.tenantSlug}/library/quality-backfill`, {
        method: "POST",
      });
      $("corpusStatus").textContent =
        `Scanned ${r.scanned || 0}; newly rejected ${r.newly_rejected || 0}` +
        (r.skipped_seed != null ? `; skipped seed ${r.skipped_seed}` : "") +
        (r.restored_seed ? `; restored seed ${r.restored_seed}` : "") +
        ".";
      $("corpusStatus").className = "status-line ok";
      await loadLibrary();
      toast("Quality backfill complete");
    } catch (e) {
      $("corpusStatus").textContent = e.message;
      $("corpusStatus").className = "status-line error";
    }
  };
}
if ($("promoteGoldBtn")) $("promoteGoldBtn").onclick = promoteGold;
if ($("synthesizeBtn")) $("synthesizeBtn").onclick = synthesize;
if ($("exportGoldBtn")) $("exportGoldBtn").onclick = () => exportLibrary("gold");
if ($("exportSynthBtn")) $("exportSynthBtn").onclick = () => exportLibrary("synthetic");
if ($("exportAllLibraryBtn")) $("exportAllLibraryBtn").onclick = () => exportLibrary("all");
if ($("exportUserUploadBtn"))
  $("exportUserUploadBtn").onclick = () => exportLibrary("user_upload");
if ($("exportMaterialsBtn"))
  $("exportMaterialsBtn").onclick = () => exportLibrary("user_material");
if ($("exportTrainableBtn"))
  $("exportTrainableBtn").onclick = () => exportLibrary("trainable");
async function loadDoubleHelixModels() {
  const sel = $("doubleHelixModel");
  if (!sel || !state.tenantSlug) return;
  try {
    const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/models`);
    const cur = sel.value;
    sel.innerHTML = (data.models || [])
      .map(
        (m) =>
          `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)} · ${m.params_b}B · ${escapeHtml(m.license)}</option>`
      )
      .join("");
    if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
  } catch (_) {
    /* ignore */
  }
}

if ($("doubleHelixZipBtn")) {
  $("doubleHelixZipBtn").onclick = async () => {
    try {
      const mid = $("doubleHelixModel")?.value || "";
      const q = mid ? `?model_id=${encodeURIComponent(mid)}` : "";
      const res = await fetch(`/api/t/${state.tenantSlug}/library/double-helix/package${q}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${state.token}` },
      });
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "helix_double_helix_v1.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast("Double Helix zip downloaded");
    } catch (e) {
      toast(e.message || "Package failed", "err");
    }
  };
}
if ($("libraryGoldZipBtn")) {
  $("libraryGoldZipBtn").onclick = () =>
    uploadGoldZip($("libraryGoldZip"), $("libraryUploadStatus"), { viaRiu: false }).catch(
      (e) => {
        if ($("libraryUploadStatus")) {
          $("libraryUploadStatus").textContent = e.message;
          $("libraryUploadStatus").className = "status-line error";
        }
        toast(e.message, "err");
      }
    );
}
if ($("libraryMaterialsZipBtn")) {
  $("libraryMaterialsZipBtn").onclick = () =>
    uploadGoldZip($("libraryMaterialsZip"), $("libraryMaterialsStatus"), {
      viaRiu: false,
      materials: true,
    }).catch((e) => {
      if ($("libraryMaterialsStatus")) {
        $("libraryMaterialsStatus").textContent = e.message;
        $("libraryMaterialsStatus").className = "status-line error";
      }
      toast(e.message, "err");
    });
}
if ($("riuGoldZipBtn")) {
  $("riuGoldZipBtn").onclick = () =>
    uploadGoldZip($("riuGoldZip"), $("riuUploadStatus"), { viaRiu: true }).catch((e) => {
      if ($("riuUploadStatus")) {
        $("riuUploadStatus").textContent = e.message;
        $("riuUploadStatus").className = "status-line error";
      }
      toast(e.message, "err");
    });
}
if ($("riuMaterialsZipBtn")) {
  $("riuMaterialsZipBtn").onclick = () =>
    uploadGoldZip($("riuMaterialsZip"), $("riuMaterialsStatus"), {
      viaRiu: true,
      materials: true,
    }).catch((e) => {
      if ($("riuMaterialsStatus")) {
        $("riuMaterialsStatus").textContent = e.message;
        $("riuMaterialsStatus").className = "status-line error";
      }
      toast(e.message, "err");
    });
}
if ($("pipeQuality")) $("pipeQuality").addEventListener("input", updatePipeQualityUI);
if ($("pipeBatches")) $("pipeBatches").addEventListener("input", updatePipeEta);
if ($("pipeBatchSize")) $("pipeBatchSize").addEventListener("input", updatePipeEta);
if ($("startPipeJobBtn")) $("startPipeJobBtn").onclick = startPipelineJob;
if ($("synthQuality")) $("synthQuality").addEventListener("input", updateSynthQualityUI);
if ($("synthBatches")) $("synthBatches").addEventListener("input", updateSynthEta);
if ($("synthMaxGolds")) $("synthMaxGolds").addEventListener("input", updateSynthEta);
if ($("riuForm")) {
  $("riuForm").addEventListener("submit", (e) => {
    e.preventDefault();
    sendRiuMessage($("riuInput")?.value || "").catch((err) => toast(err.message, "err"));
  });
}
if ($("riuInput")) {
  $("riuInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendRiuMessage($("riuInput").value || "").catch((err) => toast(err.message, "err"));
    }
  });
}
if ($("riuRestartBtn")) {
  $("riuRestartBtn").onclick = () =>
    restartRiu().catch((e) => toast(e.message, "err"));
}
document.querySelectorAll("[data-riu-quick]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const q = btn.getAttribute("data-riu-quick") || "";
    goTab("riu");
    sendRiuMessage(q).catch((e) => toast(e.message, "err"));
  });
});
updatePipeQualityUI();
updateSynthQualityUI();

if (state.token) {
  bootstrap().catch(() => logout());
}
