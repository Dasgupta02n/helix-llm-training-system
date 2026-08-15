/* Auth screens — login, signup, reset, bootstrap. */
import {
  $,
  api,
  escapeHtml,
  goTab,
  hooks,
  refreshAll,
  showApp,
  state,
  toast,
} from "./core.js";

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
  box.innerHTML = `Email was not sent (mail is not configured). <strong>Dev link:</strong><br><a href="${escapeHtml(link)}">${escapeHtml(link)}</a>`;
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
  if (state.jobPollTimer) {
    clearInterval(state.jobPollTimer);
    state.jobPollTimer = null;
  }
  for (const fn of hooks.onLogout) {
    try {
      fn();
    } catch (_) {
      /* ignore */
    }
  }
  showApp(false);
  if (!authState.token) setAuthMode("login");
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
  if (typeof hooks.fillAccountForm === "function") hooks.fillAccountForm();
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
  if ($("agentList") && typeof hooks.renderAgents === "function") hooks.renderAgents();
  await refreshAll();
  const hash = (location.hash || "").replace(/^#/, "");
  if (hash && $(`tab-${hash}`)) goTab(hash);
}

export function bindAuthEvents() {
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
}

export {
  authState,
  bootstrap,
  clearAuthMessages,
  forgotPassword,
  login,
  logout,
  parseAuthQuery,
  setAuthMode,
  showDevLink,
  signup,
  submitNewPassword,
  verifyEmailToken,
};
