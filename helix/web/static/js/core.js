/* Helix studio — shared state, fetch, tabs, usage helpers.
 *
 * Other modules import from here. Cross-panel calls go through `hooks`
 * so auth/jobs/library/riu do not import each other in a cycle.
 *
 * Usage shown to the user is 2 × billed service spend.
 */
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

function stripVendorNames(s) {
  return String(s || "")
    .replace(/\(Apify\/code\)/gi, "")
    .replace(/Apify\/code/gi, "the gather step")
    .replace(/\bOpenRouter\b/gi, "the model")
    .replace(/\bApify\b/gi, "gather")
    .replace(/\bRunPod\b/gi, "training")
    .replace(/\bHugging\s*Face\b/gi, "model storage")
    .replace(/\bHuggingFace\b/gi, "model storage")
    .replace(/\bHostinger\b/gi, "the server")
    .replace(/\bResend\b/gi, "email")
    .replace(/\bHF Hub\b/gi, "model storage")
    .replace(/\bOR \$/gi, "model $")
    .replace(/ {2,}/g, " ")
    .trim();
}

function friendlyError(msg) {
  const m = stripVendorNames(msg);
  if (/not authenticated|invalid token|401/i.test(m)) return "Please sign in again.";
  if (/budget/i.test(m)) return "This workspace has used its monthly budget.";
  if (/No LLM key|OPENROUTER/i.test(m))
    return "AI helpers need a model key set up on the server.";
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


/** Late-bound callbacks so feature modules never import each other. */
export const hooks = {
  onTab: {},
  onLogout: [],
  refreshers: {},
};

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
  const tabHook = hooks.onTab[name];
  if (typeof tabHook === "function") {
    Promise.resolve(tabHook()).catch((e) => {
      if (e && e.message) toast(e.message, "err");
    });
  }
  if (location.hash !== `#${name}`) {
    history.replaceState(null, "", `#${name}`);
  }
}

async function refreshAll() {
  state.tenantSlug = $("tenantSelect").value;
  localStorage.setItem("helix_tenant", state.tenantSlug);
  if (!state.tenantSlug) return;
  const tasks = [
    hooks.refreshers.dashboard,
    hooks.refreshers.brief,
    hooks.refreshers.datasets,
    hooks.refreshers.library,
    hooks.refreshers.jobs,
  ]
    .filter((fn) => typeof fn === "function")
    .map((fn) => fn());
  const results = await Promise.allSettled(tasks);
  const failed = results.find((r) => r.status === "rejected");
  if (failed) toast(failed.reason?.message || "Some panels failed to refresh", "err");
  updatePipeEta();
  updateSynthEta();
  if (typeof hooks.startJobPolling === "function") hooks.startJobPolling();
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

function updateSynthHint() {
  if (!$("goldTarget") || !$("synthTargetHint")) return;
  const g = parseInt($("goldTarget").value || "0", 10);
  const v = parseInt($("varPerGold").value || "0", 10);
  const total = (isFinite(g) ? g : 0) * (isFinite(v) ? v : 0);
  $("synthTargetHint").textContent = `Synthesized goal: ${total.toLocaleString()} (gold × variations)`;
}

export {
  $,
  DEFAULT_SCHEMA,
  FRIENDLY_AGENTS,
  QUALITY_COPY,
  api,
  csvToList,
  escapeHtml,
  formatMetrics,
  formatTargets,
  friendlyError,
  goTab,
  linesToList,
  parseMetrics,
  parseTargets,
  refreshAll,
  showApp,
  slugify,
  state,
  stripVendorNames,
  toast,
  updatePipeEta,
  updatePipeQualityUI,
  updateSynthEta,
  updateSynthHint,
  updateSynthQualityUI,
  _humanEta,
};
