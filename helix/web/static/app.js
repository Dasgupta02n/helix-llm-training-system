/* Helix studio console moved to ES modules under /static/js/.
 *
 * Current entry: /static/js/boot.js (type=module) from app.html.
 * This shim exists so a cached /app page that still loads /static/app.js
 * does not 404 — it boots the same modules once.
 */
if (!window.__helixStudioBooted) {
  window.__helixStudioBooted = true;
  import(`/static/js/boot.js?v=${Date.now()}`).catch((err) => {
    console.error("Helix studio failed to load", err);
  });
}
