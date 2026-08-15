/* Studio boot — bind events and restore session.
 *
 * app.html loads this file as type=module.
 */
window.__helixStudioBooted = true;
import {
  $,
  goTab,
  refreshAll,
  state,
  toast,
  updatePipeEta,
  updatePipeQualityUI,
  updateSynthEta,
  updateSynthQualityUI,
} from "./core.js";
import { authState, bindAuthEvents, bootstrap, logout, parseAuthQuery } from "./auth.js";
import { bindAccountEvents } from "./account.js";
import { bindJobsEvents } from "./jobs.js";
import { bindLibraryEvents } from "./library.js";
import { bindRiuEvents } from "./riu.js";
import { bindHomeEvents } from "./home.js";

bindAuthEvents();
bindAccountEvents();
bindJobsEvents();
bindLibraryEvents();
bindRiuEvents();
bindHomeEvents();

document.querySelectorAll(".nav-pill").forEach((btn) => {
  btn.addEventListener("click", () => goTab(btn.dataset.tab));
});

document.querySelectorAll("[data-goto]").forEach((btn) => {
  btn.addEventListener("click", () => goTab(btn.dataset.goto));
});

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-goto]");
  if (t && t.dataset.goto) goTab(t.dataset.goto);
});

parseAuthQuery();
$("refreshBtn").onclick = () =>
  refreshAll()
    .then(() => toast("Refreshed"))
    .catch((e) => toast(e.message, "err"));
$("tenantSelect").onchange = () => refreshAll().catch((e) => toast(e.message, "err"));

if ($("pipeQuality")) $("pipeQuality").addEventListener("input", updatePipeQualityUI);
if ($("pipeBatches")) $("pipeBatches").addEventListener("input", updatePipeEta);
if ($("pipeBatchSize")) $("pipeBatchSize").addEventListener("input", updatePipeEta);
if ($("synthQuality")) $("synthQuality").addEventListener("input", updateSynthQualityUI);
if ($("synthBatches")) $("synthBatches").addEventListener("input", updateSynthEta);
if ($("synthMaxGolds")) $("synthMaxGolds").addEventListener("input", updateSynthEta);

updatePipeQualityUI();
updateSynthQualityUI();

if (state.token && !authState.token) {
  bootstrap().catch(() => logout());
}
window.addEventListener("hashchange", () => {
  const hash = (location.hash || "").replace(/^#/, "");
  if (hash && $(`tab-${hash}`)) goTab(hash);
});
