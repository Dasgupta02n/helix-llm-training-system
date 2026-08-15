/* Account tab — profile, password, stored-data snapshot. */
import { $, api, escapeHtml, goTab, hooks, state, toast } from "./core.js";

function fillAccountForm() {
  if (!$("accountName") || !state.me) return;
  $("accountName").value = state.me.full_name || "";
  if ($("accountEmail")) $("accountEmail").value = state.me.email || "";
  const bits = [];
  if (state.me.email_verified) bits.push("Email confirmed");
  else bits.push("Email not confirmed yet");
  if (state.me.admin_approved) bits.push("Approved");
  else bits.push("Waiting for approval");
  if (state.me.created_at) bits.push(`Joined ${new Date(state.me.created_at).toLocaleDateString()}`);
  if (state.me.last_login_at)
    bits.push(`Last sign-in ${new Date(state.me.last_login_at).toLocaleString()}`);
  if ($("accountFlags")) $("accountFlags").textContent = bits.join(" · ");
}

async function loadAccount() {
  fillAccountForm();
  if ($("accountWorkspaceHint")) {
    $("accountWorkspaceHint").textContent = state.tenantSlug
      ? `Workspace: ${state.tenantSlug}`
      : "No workspace selected.";
  }
  const host = $("accountStoreStats");
  if (!host || !state.tenantSlug) return;
  try {
    const stats = await api(`/api/t/${state.tenantSlug}/library/stats`);
    const gold = stats.gold_user_count != null ? stats.gold_user_count : stats.gold_count || 0;
    const synth = stats.synthetic_count || 0;
    const uploads = stats.gold_user_upload_count || 0;
    const mats = stats.gold_user_material_count || 0;
    host.innerHTML = `
      <div class="stat-card tone-accent">
        <div class="label">Gold in this account</div>
        <div class="value">${Number(gold).toLocaleString()}</div>
        <div class="sub">of ${Number(stats.gold_target || 0).toLocaleString()} goal</div>
      </div>
      <div class="stat-card tone-ok">
        <div class="label">Synthesized rows</div>
        <div class="value">${Number(synth).toLocaleString()}</div>
        <div class="sub">kept in your library</div>
      </div>
      <div class="stat-card">
        <div class="label">Your uploads</div>
        <div class="value">${Number(uploads + mats).toLocaleString()}</div>
        <div class="sub">${uploads} labeled · ${mats} from materials</div>
      </div>`;
  } catch (e) {
    host.innerHTML = `<p class="status-line error">${escapeHtml(e.message)}</p>`;
  }
}

async function saveAccountProfile() {
  const name = ($("accountName") && $("accountName").value.trim()) || "";
  const status = $("accountProfileStatus");
  try {
    const data = await api("/api/auth/me", {
      method: "PATCH",
      body: JSON.stringify({ full_name: name }),
    });
    state.me = { ...state.me, ...data };
    const email = state.me.email || "";
    const shown = state.me.full_name || email;
    if ($("userLabel")) $("userLabel").textContent = shown;
    if ($("userAvatar")) $("userAvatar").textContent = (shown[0] || "U").toUpperCase();
    fillAccountForm();
    if (status) {
      status.textContent = "Profile saved.";
      status.className = "status-line ok";
    }
    toast("Profile saved");
  } catch (e) {
    if (status) {
      status.textContent = e.message;
      status.className = "status-line error";
    }
    toast(e.message, "err");
  }
}

async function saveAccountPassword() {
  const status = $("accountPwStatus");
  const current = ($("accountCurrentPw") && $("accountCurrentPw").value) || "";
  const next = ($("accountNewPw") && $("accountNewPw").value) || "";
  try {
    const data = await api("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    if ($("accountCurrentPw")) $("accountCurrentPw").value = "";
    if ($("accountNewPw")) $("accountNewPw").value = "";
    if (status) {
      status.textContent = data.message || "Password updated.";
      status.className = "status-line ok";
    }
    toast("Password updated");
  } catch (e) {
    if (status) {
      status.textContent = e.message;
      status.className = "status-line error";
    }
    toast(e.message, "err");
  }
}

export function bindAccountEvents() {
  if ($("openAccountBtn")) $("openAccountBtn").onclick = () => goTab("account");
  if ($("accountSaveBtn")) $("accountSaveBtn").onclick = () => saveAccountProfile();
  if ($("accountPwBtn")) $("accountPwBtn").onclick = () => saveAccountPassword();
  document.querySelectorAll("[data-delete-account]").forEach((btn) => {
    btn.onclick = async () => {
      const sure = window.prompt(
        "This removes your login. Signed declarations stay on file keyed to your email and cannot be deleted. Type DELETE to continue."
      );
      if ((sure || "").trim().toUpperCase() !== "DELETE") return;
      const password = window.prompt("Enter your password to delete this account.");
      if (!password) return;
      try {
        await api("/api/auth/delete-account", {
          method: "POST",
          body: JSON.stringify({ password, confirm: "DELETE" }),
        });
        toast("Account removed. Declarations retained.");
        state.token = "";
        try {
          localStorage.removeItem("helix_token");
        } catch (_) {
          /* ignore */
        }
        location.reload();
      } catch (e) {
        toast(e.message || "Could not delete account", "err");
      }
    };
  });
}

hooks.onTab.account = () => loadAccount().catch((e) => toast(e.message, "err"));
hooks.fillAccountForm = fillAccountForm;

export { fillAccountForm, loadAccount, saveAccountProfile, saveAccountPassword };
