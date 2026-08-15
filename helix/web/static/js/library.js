/* My data — gold, synthetics, corpus, C7X-IO, declarations, snapshots. */
import {
  $,
  api,
  escapeHtml,
  friendlyError,
  hooks,
  state,
  toast,
  updateSynthHint,
} from "./core.js";
import { _activityHtml, _livePill, loadJobs } from "./jobs.js";

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

async function loadLibrary(opts = {}) {
  if (!$("libraryProgress")) return;
  loadDeclarations().catch(() => {});
  try {
    const [stats, settings, gold, synth] = await Promise.all([
      api(`/api/t/${state.tenantSlug}/library/stats`),
      api(`/api/t/${state.tenantSlug}/library/settings`),
      api(`/api/t/${state.tenantSlug}/library/gold?limit=20`),
      api(`/api/t/${state.tenantSlug}/library/synthetic?limit=20`),
    ]);

    if (opts.settings !== false) {
      if ($("goldTarget")) $("goldTarget").value = settings.gold_target_count;
      if ($("varPerGold")) $("varPerGold").value = settings.variations_per_gold;
      if ($("autoPromote")) $("autoPromote").checked = !!settings.auto_promote_approved;
      updateSynthHint();
      renderParamPicker(settings.available_parameters, settings.vary_parameters);
    }

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
        ? `${bits.join(" · ")}. Export my uploads / Export materials / Export all trainable for C7X-IO.`
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
      $("synthList").innerHTML = `<div class="empty"><div class="icon">✨</div>No variations yet. Choose parameters and press “Start synthesis job”.</div>`;
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

function _exportFormat() {
  return $("exportChatFormat") && $("exportChatFormat").checked ? "chat" : "jsonl";
}

function _saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function _filenameFromDisposition(header, fallback) {
  const m = String(header || "").match(/filename="([^"]+)"/i);
  return (m && m[1]) || fallback;
}

async function downloadLibraryZip({ scope = "library", version = "" } = {}) {
  if (!state.tenantSlug) throw new Error("Pick a workspace first");
  const format = _exportFormat();
  let path;
  if (version) {
    path = `/api/t/${state.tenantSlug}/library/snapshots/${encodeURIComponent(version)}/download?format=${encodeURIComponent(format)}`;
  } else {
    path = `/api/t/${state.tenantSlug}/library/export-zip?scope=${encodeURIComponent(scope)}&format=${encodeURIComponent(format)}`;
  }
  const res = await fetch(path, { headers: { Authorization: `Bearer ${state.token}` } });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(friendlyError(t || res.statusText));
  }
  const blob = await res.blob();
  const name = _filenameFromDisposition(
    res.headers.get("content-disposition"),
    `c7x_${state.tenantSlug}_${version || scope}.zip`
  );
  _saveBlob(blob, name);
  const empty = res.headers.get("X-Helix-Pack-Empty");
  if (empty) toast(empty, "err");
  else toast("Zip downloaded — gold, synthetics, and corpus are separate files");
  return blob;
}

async function exportLibrary(kind, fmt) {
  try {
    if (kind === "all" || kind === "gold" || kind === "synthetic") {
      const pick = ($("exportSavePick") && $("exportSavePick").value) || "";
      await downloadLibraryZip({ scope: "library", version: pick });
      return;
    }
    const format = fmt || _exportFormat();
    const res = await fetch(
      `/api/t/${state.tenantSlug}/library/export?kind=${encodeURIComponent(kind)}&format=${encodeURIComponent(format)}`,
      { headers: { Authorization: `Bearer ${state.token}` } }
    );
    if (!res.ok) throw new Error("Download failed");
    const blob = await res.blob();
    _saveBlob(blob, `c7x_${state.tenantSlug}_${kind}.${format === "json" ? "json" : "jsonl"}`);
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
  if (viaRiu && data.session && typeof hooks.applyRiuSession === "function") {
    hooks.applyRiuSession(data.session);
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

async function loadDatasets() {
  if (!$("datasetList") || !state.tenantSlug) return;
  const rows = await api(`/api/t/${state.tenantSlug}/datasets`);
  if (!rows.length) {
    $("datasetList").innerHTML = `
      <div class="empty">
        <div class="icon">📦</div>
        No named session packs yet. Generate gold this chat, then “Save current examples”.
      </div>`;
    _fillExportSavePick([]);
    return;
  }
  $("datasetList").innerHTML = `<div class="table-wrap"><table class="table">
    <thead><tr><th>Name</th><th>In this pack</th><th>Saved</th><th></th></tr></thead>
    <tbody>
      ${rows
        .map((r) => {
          const c = r.counts || {};
          const bits = r.pack
            ? [
                `${c.gold || 0} gold`,
                `${c.synthetic || 0} synth`,
                `${c.structured || 0} labeled`,
                `${c.unstructured || 0} materials`,
              ].join(" · ")
            : `${r.example_count} examples`;
          return `<tr>
        <td><strong>${escapeHtml(r.version)}</strong></td>
        <td>${escapeHtml(bits)}</td>
        <td>${escapeHtml((r.created_at || "").replace("T", " ").slice(0, 16))}</td>
        <td class="flex">
          <button class="btn btn-primary btn-sm" data-dl="${escapeHtml(r.version)}" type="button">Download zip</button>
        </td>
      </tr>`;
        })
        .join("")}
    </tbody>
  </table></div>`;
  $("datasetList").querySelectorAll("button[data-dl]").forEach((btn) => {
    btn.onclick = () =>
      downloadLibraryZip({ version: btn.dataset.dl }).catch((e) => toast(e.message, "err"));
  });
  _fillExportSavePick(rows);
}

function _fillExportSavePick(rows) {
  const sel = $("exportSavePick");
  if (!sel) return;
  const cur = sel.value;
  const packs = (rows || []).filter((r) => r.pack || r.manifest?.kind === "library_pack");
  sel.innerHTML =
    `<option value="">Everything so far</option>` +
    packs
      .map((r) => `<option value="${escapeHtml(r.version)}">${escapeHtml(r.version)}</option>`)
      .join("");
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

async function downloadExport(version, format) {
  $("exportStatus").textContent = "Preparing your zip…";
  $("exportStatus").className = "status-line";
  try {
    await downloadLibraryZip({ version });
    $("exportStatus").textContent = "Zip downloaded.";
    $("exportStatus").className = "status-line ok";
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
    const c = r.counts || {};
    const bits = `${c.gold || 0} gold · ${c.synthetic || 0} synth · ${c.structured || 0} labeled · ${c.unstructured || 0} materials`;
    $("exportStatus").textContent = r.empty_reason
      ? r.empty_reason
      : `Saved “${r.version}” — ${bits}.`;
    $("exportStatus").className = r.empty_reason ? "status-line error" : "status-line ok";
    toast(r.empty_reason || `Saved this session as ${r.version}`);
    await loadDatasets();
  } catch (e) {
    $("exportStatus").textContent = e.message;
    $("exportStatus").className = "status-line error";
    toast(e.message, "err");
  }
}

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

let _dhTrainTimer = null;
function renderDoubleHelixTrain(job) {
  const statusEl = $("doubleHelixTrainStatus");
  const hint = $("doubleHelixTrainHint");
  const wrap = $("doubleHelixTrainDownloadWrap");
  if (!statusEl) return;
  if (!job) {
    statusEl.textContent = "";
    if (hint) hint.textContent = "";
    if (wrap) wrap.classList.add("hidden");
    if ($("doubleHelixTrainCancelBtn")) $("doubleHelixTrainCancelBtn").classList.add("hidden");
    if ($("doubleHelixLiveBadge")) $("doubleHelixLiveBadge").innerHTML = "";
    if ($("doubleHelixActivity")) $("doubleHelixActivity").innerHTML = "";
    return;
  }
  statusEl.textContent = `${job.status}: ${job.progress || ""}`;
  statusEl.className = job.status === "failed" ? "status-line error" : "status-line";
  if ($("doubleHelixLiveBadge")) {
    $("doubleHelixLiveBadge").innerHTML = _livePill(job);
  }
  if ($("doubleHelixActivity")) {
    const html = _activityHtml(job.events);
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    const ol = tmp.querySelector("ol");
    $("doubleHelixActivity").innerHTML = ol
      ? ol.innerHTML
      : "<li>No steps yet. A heartbeat will appear once training starts.</li>";
  }
  if (hint) {
    hint.textContent = job.error
      ? job.error
      : job.download_ready
        ? job.declaration_accepted
          ? "Declaration on file. You can download the trained zip (adapter, tokenizer, gold, load_adapter.py)."
          : "Training finished. Accept the ownership/liability declaration to download."
        : "C7X is using gold already in this account. You can still download the data zip anytime.";
  }
  if (wrap) wrap.classList.toggle("hidden", !job.download_ready);
  const cancelBtn = $("doubleHelixTrainCancelBtn");
  if (cancelBtn) {
    const canCancel = ["queued", "uploading", "running", "packaging"].includes(job.status);
    cancelBtn.classList.toggle("hidden", !canCancel);
  }
  const active = ["queued", "uploading", "running", "packaging"].includes(job.status);
  if (active && !_dhTrainTimer) {
    _dhTrainTimer = setInterval(() => loadDoubleHelixTrain().catch(() => {}), 2000);
  }
  if (!active && _dhTrainTimer) {
    clearInterval(_dhTrainTimer);
    _dhTrainTimer = null;
  }
}
async function loadDoubleHelixTrain() {
  if (!state.tenantSlug) return;
  const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/train`);
  renderDoubleHelixTrain(data.job);
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
      a.download = "c7x_io_gold_v1.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast("Gold zip downloaded — train it anywhere");
    } catch (e) {
      toast(e.message || "Package failed", "err");
    }
  };
}
if ($("doubleHelixTrainCancelBtn")) {
  $("doubleHelixTrainCancelBtn").onclick = async () => {
    try {
      const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/train`);
      const job = data.job;
      if (!job) throw new Error("No train job");
      const out = await api(
        `/api/t/${state.tenantSlug}/library/double-helix/train/${encodeURIComponent(job.id)}/cancel`,
        { method: "POST" }
      );
      renderDoubleHelixTrain(out.job);
      toast("Training cancelled");
    } catch (e) {
      toast(e.message || "Could not cancel", "err");
    }
  };
}
if ($("doubleHelixTrainBtn")) {
  $("doubleHelixTrainBtn").onclick = async () => {
    const box = $("doubleHelixTrainConfirm");
    if (!box || !box.checked) {
      toast("Tick the confirm box first — this starts a paid GPU job.", "err");
      return;
    }
    try {
      const mid = $("doubleHelixModel")?.value || "";
      const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/train`, {
        method: "POST",
        body: JSON.stringify({
          model_id: mid,
          confirm: true,
          include_synthetics: !!(
            $("doubleHelixIncludeSynth") && $("doubleHelixIncludeSynth").checked
          ),
        }),
      });
      renderDoubleHelixTrain(data.job);
      toast("Training queued from rows in your account");
    } catch (e) {
      toast(e.message || "Could not start training", "err");
    }
  };
}
async function loadDeclarations() {
  const host = $("declarationList");
  if (!host || !state.tenantSlug) return;
  try {
    const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/declarations`);
    const items = data.items || [];
    if (!items.length) {
      host.innerHTML = `<p class="hint">No signed declarations yet.</p>`;
      return;
    }
    host.innerHTML = items
      .map(
        (d) =>
          `<div class="pane pad" style="margin-bottom:8px">
            <strong>${escapeHtml(d.declaration_version || "")}</strong>
            <span class="hint"> · ${escapeHtml((d.accepted_at || "").slice(0, 19).replace("T", " "))} · job ${escapeHtml(d.train_job_id || "—")}</span>
            <p class="hint mb-0">Cannot delete. Email copy: ${escapeHtml(d.email_status || "—")}</p>
          </div>`
      )
      .join("");
  } catch (_) {
    host.innerHTML = "";
  }
}

async function downloadTrainedZip(jobId) {
  const res = await fetch(
    `/api/t/${state.tenantSlug}/library/double-helix/train/${encodeURIComponent(jobId)}/download`,
    { headers: { Authorization: `Bearer ${state.token}` } }
  );
  if (!res.ok) {
    let msg = await res.text();
    try {
      const j = JSON.parse(msg);
      const d = j.detail;
      msg = d && d.message ? d.message : typeof d === "string" ? d : msg;
    } catch (_) {
      /* keep text */
    }
    throw new Error(msg || "Download failed");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `c7x_io_trained_${jobId}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function openDeclarationModal(job) {
  const modal = $("declarationModal");
  const body = $("declarationBody");
  const box = $("declarationConfirmBox");
  if (!modal || !body) return;
  const decl = await api(`/api/t/${state.tenantSlug}/library/double-helix/declaration`);
  body.textContent = decl.text || "";
  if (box) box.checked = false;
  modal.dataset.jobId = job.id;
  modal.classList.remove("hidden");
}

function closeDeclarationModal() {
  const modal = $("declarationModal");
  if (modal) modal.classList.add("hidden");
}

export function bindLibraryEvents() {
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
  if ($("exportAllLibraryBtn"))
    $("exportAllLibraryBtn").onclick = () =>
      downloadLibraryZip({ scope: "library" }).catch((e) => toast(e.message, "err"));
  if ($("exportUserUploadBtn"))
    $("exportUserUploadBtn").onclick = () => exportLibrary("user_upload");
  if ($("exportMaterialsBtn"))
    $("exportMaterialsBtn").onclick = () => exportLibrary("user_material");
  if ($("exportTrainableBtn"))
    $("exportTrainableBtn").onclick = () => exportLibrary("trainable");
  if ($("snapshotBtn")) $("snapshotBtn").onclick = snapshotPool;
  if ($("exportPoolBtn"))
    $("exportPoolBtn").onclick = () => {
      if ($("exportStatus")) {
        $("exportStatus").textContent = "Preparing this session’s zip…";
        $("exportStatus").className = "status-line";
      }
      downloadLibraryZip({ scope: "session" })
        .then(() => {
          if ($("exportStatus")) {
            $("exportStatus").textContent = "Session zip downloaded.";
            $("exportStatus").className = "status-line ok";
          }
        })
        .catch((e) => {
          if ($("exportStatus")) {
            $("exportStatus").textContent = e.message;
            $("exportStatus").className = "status-line error";
          }
          toast(e.message, "err");
        });
    };
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
        a.download = "c7x_io_gold_v1.zip";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        toast("Gold zip downloaded — train it anywhere");
      } catch (e) {
        toast(e.message || "Package failed", "err");
      }
    };
  }
  if ($("doubleHelixTrainCancelBtn")) {
    $("doubleHelixTrainCancelBtn").onclick = async () => {
      try {
        const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/train`);
        const job = data.job;
        if (!job) throw new Error("No train job");
        const out = await api(
          `/api/t/${state.tenantSlug}/library/double-helix/train/${encodeURIComponent(job.id)}/cancel`,
          { method: "POST" }
        );
        renderDoubleHelixTrain(out.job);
        toast("Training cancelled");
      } catch (e) {
        toast(e.message || "Could not cancel", "err");
      }
    };
  }
  if ($("doubleHelixTrainBtn")) {
    $("doubleHelixTrainBtn").onclick = async () => {
      const box = $("doubleHelixTrainConfirm");
      if (!box || !box.checked) {
        toast("Tick the confirm box first — this starts a paid GPU job.", "err");
        return;
      }
      try {
        const mid = $("doubleHelixModel")?.value || "";
        const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/train`, {
          method: "POST",
          body: JSON.stringify({
            model_id: mid,
            confirm: true,
            include_synthetics: !!(
              $("doubleHelixIncludeSynth") && $("doubleHelixIncludeSynth").checked
            ),
          }),
        });
        renderDoubleHelixTrain(data.job);
        toast("Training queued from rows in your account");
      } catch (e) {
        toast(e.message || "Could not start training", "err");
      }
    };
  }
  if ($("doubleHelixTrainDownloadBtn")) {
    $("doubleHelixTrainDownloadBtn").onclick = async () => {
      try {
        const data = await api(`/api/t/${state.tenantSlug}/library/double-helix/train`);
        const job = data.job;
        if (!job || !job.download_ready) throw new Error("Trained zip is not ready yet");
        if (!job.declaration_accepted) {
          await openDeclarationModal(job);
          return;
        }
        await downloadTrainedZip(job.id);
        toast("Trained zip downloaded");
      } catch (e) {
        toast(e.message || "Download failed", "err");
      }
    };
  }
  if ($("declarationCancelBtn")) $("declarationCancelBtn").onclick = closeDeclarationModal;
  if ($("declarationAcceptBtn")) {
    $("declarationAcceptBtn").onclick = async () => {
      const box = $("declarationConfirmBox");
      const modal = $("declarationModal");
      const jobId = modal && modal.dataset.jobId;
      if (!box || !box.checked) {
        toast("Tick the declaration box to accept.", "err");
        return;
      }
      if (!jobId) return;
      try {
        await api(
          `/api/t/${state.tenantSlug}/library/double-helix/train/${encodeURIComponent(jobId)}/accept-declaration`,
          { method: "POST", body: JSON.stringify({ confirm: true }) }
        );
        closeDeclarationModal();
        await downloadTrainedZip(jobId);
        loadDeclarations().catch(() => {});
        loadDoubleHelixTrain().catch(() => {});
        toast("Declaration accepted. Copy emailed. Zip downloading.");
      } catch (e) {
        toast(e.message || "Could not accept declaration", "err");
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
}

hooks.onTab.library = () => {
  loadLibrary().catch(() => {});
  loadDoubleHelixModels().catch(() => {});
  loadDoubleHelixTrain().catch(() => {});
  loadDeclarations().catch(() => {});
};
hooks.onTab.download = () => loadDatasets().catch(() => {});
hooks.refreshers.library = loadLibrary;
hooks.refreshers.datasets = loadDatasets;
hooks.loadDoubleHelixModels = loadDoubleHelixModels;
hooks.loadDoubleHelixTrain = loadDoubleHelixTrain;
hooks.loadDeclarations = loadDeclarations;
hooks.onLogout.push(() => {
  if (_dhTrainTimer) {
    clearInterval(_dhTrainTimer);
    _dhTrainTimer = null;
  }
});

export {
  closeDeclarationModal,
  downloadExport,
  downloadTrainedZip,
  exportLibrary,
  loadCorpus,
  loadDatasets,
  loadDeclarations,
  loadDoubleHelixModels,
  loadDoubleHelixTrain,
  loadLibrary,
  openDeclarationModal,
  promoteGold,
  renderDoubleHelixTrain,
  renderParamPicker,
  saveScope,
  selectedParams,
  snapshotPool,
  synthesize,
  updateRiuUploadPanel,
  uploadGoldZip,
};
