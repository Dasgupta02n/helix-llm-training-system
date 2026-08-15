/* Riu setup chat. */
import { $, api, escapeHtml, goTab, hooks, refreshAll, state, toast } from "./core.js";
import { updateRiuUploadPanel, uploadGoldZip } from "./library.js";

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
      (r) =>
        r.ok &&
        (r.action === "start_pipeline" ||
          r.action === "start_proof_batch" ||
          r.action === "start_scale_batch" ||
          r.action === "start_synthesis" ||
          r.action === "start_double_helix_train")
    );
    if (jobsStarted.length) {
      const trained = jobsStarted.some((r) => r.action === "start_double_helix_train");
      toast(
        trained
          ? "Riu queued Double Helix training — watch My data for the download"
          : "Riu started a job — it keeps running if you leave"
      );
      if (typeof hooks.refreshers.jobs === "function") hooks.refreshers.jobs().catch(() => {});
      if (typeof hooks.startJobPolling === "function") hooks.startJobPolling();
      if (typeof hooks.loadDoubleHelixTrain === "function") hooks.loadDoubleHelixTrain().catch(() => {});
      refreshAll().catch(() => {});
    } else if ((data.action_results || []).some((r) => r.ok && r.action === "save_plan")) {
      if (typeof hooks.refreshers.brief === "function") hooks.refreshers.brief().catch(() => {});
    } else if ((data.action_results || []).some((r) => r.ok && r.action === "save_goals")) {
      if (typeof hooks.refreshers.library === "function") hooks.refreshers.library().catch(() => {});
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
  updateRiuUploadPanel(data);
  if ($("riuProgressBadge")) $("riuProgressBadge").textContent = "Setup 0%";
  $("riuStatus").textContent = "New chat with Riu";
  toast("New chat with Riu");
}

export function bindRiuEvents() {
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
    $("riuRestartBtn").onclick = () => restartRiu().catch((e) => toast(e.message, "err"));
  }
  document.querySelectorAll("[data-riu-quick]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const q = btn.getAttribute("data-riu-quick") || "";
      goTab("riu");
      sendRiuMessage(q).catch((e) => toast(e.message, "err"));
    });
  });
}

hooks.onTab.riu = () => loadRiuSession().catch((e) => toast(e.message, "err"));
hooks.applyRiuSession = (session) => {
  renderRiuMessages(session.messages || []);
  renderRiuState(session.state || {});
  updateRiuUploadPanel(session);
};

export {
  loadRiuSession,
  renderMarkdownLite,
  renderRiuMessages,
  renderRiuState,
  restartRiu,
  sendRiuMessage,
};
