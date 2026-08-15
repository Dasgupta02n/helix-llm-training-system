/* Home dashboard — plan summary, escalations, leftover studio forms. */
import {
  $,
  DEFAULT_SCHEMA,
  FRIENDLY_AGENTS,
  api,
  csvToList,
  escapeHtml,
  formatMetrics,
  formatTargets,
  goTab,
  hooks,
  linesToList,
  parseMetrics,
  parseTargets,
  slugify,
  state,
  toast,
} from "./core.js";

async function refreshDashboard() {
  const dash = await api(`/api/t/${state.tenantSlug}/dashboard`);
  const m = dash.metrics || {};
  const budget = m.budget || {};
  const spent = budget.user_charge_usd != null ? budget.user_charge_usd : budget.spent_usd || 0;
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
      <div class="sub">usage $${Number(spent).toFixed(2)} · budget $${Number(limit).toFixed(0)}</div>
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
        No helper runs yet. Collection is started by <strong>Riu</strong>.
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
  if (!$("schemaFormTitle")) return;
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
  if (!$("schemaList")) return;
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
  if (!list) return;
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

export function bindHomeEvents() {
  if ($("saveBriefBtn")) $("saveBriefBtn").onclick = saveBrief;
  if ($("saveSchemaBtn")) $("saveSchemaBtn").onclick = saveSchema;
  if ($("newSchemaBtn")) $("newSchemaBtn").onclick = clearSchemaForm;
  if ($("newSchemaBtn2")) $("newSchemaBtn2").onclick = clearSchemaForm;
  if ($("runBtn")) $("runBtn").onclick = runAgent;
  if ($("pipelineBtn")) $("pipelineBtn").onclick = runPipeline;
}

hooks.refreshers.dashboard = refreshDashboard;
hooks.refreshers.brief = loadBrief;
hooks.renderAgents = renderAgents;

export {
  clearSchemaForm,
  editSchema,
  fillBriefForm,
  loadBrief,
  loadSchemas,
  refreshDashboard,
  renderAgents,
  runAgent,
  runPipeline,
  saveBrief,
  saveSchema,
};
