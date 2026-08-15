/* Jobs — live step log, 2× usage, polling. */
import { $, api, escapeHtml, hooks, state, toast } from "./core.js";

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
      ${escapeHtml(msg || "Job trajectory would exceed the spend cap for this job.")}
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
  const usage =
    j.user_charge_usd != null
      ? j.user_charge_usd
      : total != null
        ? Number(total)
        : null;
  if (usage != null) bits.push(`usage $${Number(usage).toFixed(4)}`);
  if (cap != null && Number(cap) > 0) bits.push(`cap $${Number(cap).toFixed(4)}`);
  return bits.length
    ? `<div class="hint" style="margin-top:4px">Cost: ${bits.join(" · ")}</div>`
    : "";
}

function _jobRenderKey(j) {
  const lastEv = (j.events && j.events.length) ? j.events[j.events.length - 1].message : "";
  return [
    j.id,
    j.status,
    j.completed_batches,
    j.items_processed,
    j.progress_pct,
    j.progress_message,
    j.updated_at,
    j.live_state,
    j.eta_seconds,
    lastEv,
    JSON.stringify(j.result_summary || {}),
  ].join("|");
}

function _fmtClock(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return "";
  }
}

function _livePill(j) {
  const st = j.live_state || "idle";
  const cls =
    st === "live" ? "live-ok" : st === "quiet" ? "live-quiet" : st === "stale" ? "live-stale" : "live-idle";
  return `<span class="live-pill ${cls}"><i class="live-dot"></i>${escapeHtml(j.live_label || st)}</span>`;
}

function _activityHtml(events) {
  const evs = (events || []).slice(-14).reverse();
  if (!evs.length) return "";
  return `<ol class="live-log">${evs
    .map(
      (e) =>
        `<li class="${escapeHtml(e.level || "info")}"><time>${escapeHtml(
          _fmtClock(e.created_at)
        )}</time>${escapeHtml(e.message || "")}</li>`
    )
    .join("")}</ol>`;
}

function renderLiveProcessPanel(jobs, train) {
  const panel = $("liveProcessPanel");
  const body = $("liveProcessBody");
  const badge = $("liveProcessBadge");
  const label = $("liveProcessLabel");
  if (!panel || !body) return;
  const runningJobs = (jobs || []).filter((j) =>
    ["pending", "running", "paused_spend_cap"].includes(j.status)
  );
  const trainActive =
    train && ["queued", "uploading", "running", "packaging"].includes(train.status);
  if (!runningJobs.length && !trainActive) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const sources = [];
  runningJobs.forEach((j) => {
    sources.push({
      kind: j.job_type === "synthesis" ? "Synthesis" : "Mining",
      ...j,
    });
  });
  if (trainActive) {
    sources.push({
      kind: "Double Helix train",
      progress_message: train.progress,
      ...train,
    });
  }
  const worst =
    sources.some((s) => s.live_state === "stale")
      ? "stale"
      : sources.some((s) => s.live_state === "quiet")
        ? "quiet"
        : "live";
  const cls =
    worst === "live" ? "live-ok" : worst === "quiet" ? "live-quiet" : "live-stale";
  if (badge) {
    badge.className = `live-pill ${cls}`;
    badge.innerHTML = `<i class="live-dot"></i>${
      worst === "live" ? "Live" : worst === "quiet" ? "Working" : "Check this"
    }`;
  }
  if (label) {
    label.textContent = sources
      .map((s) => `${s.kind}: ${s.progress_message || s.progress || s.status}`)
      .join(" · ");
  }
  const merged = [];
  sources.forEach((s) => {
    (s.events || []).forEach((e) => {
      merged.push({
        ...e,
        message: `[${s.kind}] ${e.message || ""}`,
      });
    });
  });
  merged.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
  const evs = merged.slice(-14).reverse();
  body.innerHTML = evs.length
    ? evs
        .map(
          (e) =>
            `<li class="${escapeHtml(e.level || "info")}"><time>${escapeHtml(
              _fmtClock(e.created_at)
            )}</time>${escapeHtml(e.message || "")}</li>`
        )
        .join("")
    : "<li>Waiting for the first step…</li>";
}

async function loadJobs() {
  if (!$("jobsList") || !state.tenantSlug) return;
  if (state._jobsLoading) return; // prevent stacked polls from racing
  state._jobsLoading = true;
  try {
    const data = await api(`/api/t/${state.tenantSlug}/jobs`);
    const jobs = data.jobs || [];
    let train = null;
    try {
      const t = await api(`/api/t/${state.tenantSlug}/library/double-helix/train`);
      train = t.job || null;
    } catch (_) {
      train = null;
    }
    renderLiveProcessPanel(jobs, train);
    const active = (data.active || []).length + (train && ["queued", "uploading", "running", "packaging"].includes(train.status) ? 1 : 0);
    if ($("jobsLiveHint")) {
      $("jobsLiveHint").textContent = active
        ? `${active} process(es) live — real steps + heartbeat (poll 2s). A frozen bar without a new log line means wait; a red pill means it may be stuck.`
        : "No active jobs";
    }
    if (!jobs.length) {
      $("jobsList").innerHTML = `<div class="empty"><div class="icon">⏱️</div>No jobs yet. Ask Riu to start collecting, or start synthesis from My data.</div>`;
      state._jobsRenderKey = train ? _jobRenderKey(train) : "";
      return;
    }
    const renderKey = jobs.map(_jobRenderKey).join("||") + (train ? `||T:${_jobRenderKey(train)}` : "");
    // Still re-render if anything moved; skip only exact same snapshot
    if (state._jobsRenderKey === renderKey && $("jobsList").children.length) {
      return;
    }
    state._jobsRenderKey = renderKey;

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
        const pct = Number(j.progress_pct) || 0;
        const updated = j.updated_at ? new Date(j.updated_at).toLocaleTimeString() : "—";
        const costBits = [];
        const usage =
          j.user_charge_usd != null ? j.user_charge_usd : j.cost_usd;
        if (usage != null) costBits.push(`usage $${Number(usage).toFixed(3)}`);
        if (j.spend_cap_usd != null && Number(j.spend_cap_usd) > 0)
          costBits.push(`cap $${Number(j.spend_cap_usd).toFixed(3)}`);
        return `<div class="job-card" data-job="${escapeHtml(j.id)}" data-updated="${escapeHtml(j.updated_at || "")}">
          <div class="job-head">
            <div>
              <strong>${typeLabel}</strong>
              <span class="badge ${badge}">${escapeHtml(statusLabel)}</span>
              <span class="badge">Q${j.quality_mode}</span>
              ${j.status === "running" || j.status === "pending" ? _livePill(j) : ""}
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
          ${_activityHtml(j.events)}
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
          "This job is over the spend-cap trajectory for its gold/synthetic target.\n\n" +
            "Continue remaining batches anyway?\n" +
            "You will be charged for further model + gather usage."
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
      if (typeof hooks.refreshers.library === "function") {
        hooks.refreshers.library({ settings: false }).catch(() => {});
      }
      if (typeof hooks.loadDoubleHelixTrain === "function") {
        hooks.loadDoubleHelixTrain().catch(() => {});
      }
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

export function bindJobsEvents() {
  if ($("startPipeJobBtn")) $("startPipeJobBtn").onclick = startPipelineJob;
}

hooks.onTab.home = () => loadJobs().catch(() => {});
hooks.refreshers.jobs = loadJobs;
hooks.startJobPolling = startJobPolling;

export {
  _activityHtml,
  _fmtClock,
  _jobCostLine,
  _jobRenderKey,
  _jobResultBanner,
  _livePill,
  loadJobs,
  renderLiveProcessPanel,
  startJobPolling,
  startPipelineJob,
};
