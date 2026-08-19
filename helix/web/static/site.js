(function () {
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var paused = reduce || window.localStorage.getItem("c7x-motion") === "paused";
  var html = document.documentElement;

  function applyMotion() {
    html.classList.toggle("motion-paused", paused);
    var btn = document.querySelector("[data-motion-toggle]");
    if (btn) {
      btn.setAttribute("aria-pressed", paused ? "true" : "false");
      btn.textContent = paused ? "Play motion" : "Pause motion";
    }
  }
  applyMotion();
  var motionBtn = document.querySelector("[data-motion-toggle]");
  if (motionBtn) {
    motionBtn.addEventListener("click", function () {
      paused = !paused;
      window.localStorage.setItem("c7x-motion", paused ? "paused" : "on");
      applyMotion();
    });
  }

  var btn = document.querySelector("[data-nav-toggle]");
  var nav = document.getElementById("siteNav");
  function closeNav() {
    if (!nav || !btn) return;
    nav.classList.remove("is-open");
    btn.setAttribute("aria-expanded", "false");
    btn.textContent = "Menu";
  }
  if (btn && nav) {
    btn.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.textContent = open ? "Close" : "Menu";
      if (open) {
        var first = nav.querySelector("a");
        if (first) first.focus();
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        closeNav();
        btn.focus();
      }
    });
  }

  document.querySelectorAll("[data-studio]").forEach(function (win) {
    var tabs = Array.prototype.slice.call(win.querySelectorAll("[role='tab']"));
    var panels = Array.prototype.slice.call(win.querySelectorAll("[role='tabpanel']"));
    function show(i) {
      tabs.forEach(function (t, idx) {
        var on = idx === i;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
      });
      panels.forEach(function (p, idx) { p.classList.toggle("is-on", idx === i); });
    }
    tabs.forEach(function (t, idx) {
      t.addEventListener("click", function () { show(idx); });
      t.addEventListener("keydown", function (e) {
        var n = idx;
        if (e.key === "ArrowRight") n = (idx + 1) % tabs.length;
        else if (e.key === "ArrowLeft") n = (idx - 1 + tabs.length) % tabs.length;
        else return;
        e.preventDefault();
        show(n);
        tabs[n].focus();
      });
    });
    show(0);
  });

  var geo = document.getElementById("geoStage");
  if (geo && !reduce) {
    var gctx = geo.getContext("2d");
    var bits = [];
    var colors = ["#ff2d95", "#00e5ff", "#b6ff3b", "#ff6a00", "#111", "#fff200"];
    var raf = 0;
    var running = false;
    function resizeGeo() {
      var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      geo.width = Math.floor(window.innerWidth * dpr);
      geo.height = Math.floor(window.innerHeight * dpr);
      geo.style.width = window.innerWidth + "px";
      geo.style.height = window.innerHeight + "px";
      gctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function seed() {
      bits = [];
      var i, w = window.innerWidth, h = window.innerHeight;
      for (i = 0; i < 22; i++) {
        bits.push({
          t: i % 3,
          x: Math.random() * w,
          y: Math.random() * h,
          s: 18 + Math.random() * 70,
          r: Math.random() * Math.PI,
          v: 0.002 + Math.random() * 0.008,
          c: colors[i % colors.length],
          vx: -0.25 + Math.random() * 0.5,
          vy: -0.2 + Math.random() * 0.4
        });
      }
    }
    function drawGeo() {
      if (paused || document.hidden) {
        running = false;
        return;
      }
      var w = window.innerWidth, h = window.innerHeight, i, b;
      gctx.clearRect(0, 0, w, h);
      for (i = 0; i < bits.length; i++) {
        b = bits[i];
        b.r += b.v;
        b.x += b.vx;
        b.y += b.vy;
        if (b.x < -80) b.x = w + 80;
        if (b.x > w + 80) b.x = -80;
        if (b.y < -80) b.y = h + 80;
        if (b.y > h + 80) b.y = -80;
        gctx.save();
        gctx.translate(b.x, b.y);
        gctx.rotate(b.r);
        gctx.fillStyle = b.c;
        if (b.t === 0) {
          gctx.fillRect(-b.s / 2, -b.s / 2, b.s, b.s);
        } else if (b.t === 1) {
          gctx.beginPath();
          gctx.arc(0, 0, b.s / 2, 0, Math.PI * 2);
          gctx.fill();
        } else {
          gctx.beginPath();
          gctx.moveTo(0, -b.s / 2);
          gctx.lineTo(b.s / 2, b.s / 2);
          gctx.lineTo(-b.s / 2, b.s / 2);
          gctx.closePath();
          gctx.fill();
        }
        gctx.restore();
      }
      raf = window.requestAnimationFrame(drawGeo);
    }
    function start() {
      if (running || paused) return;
      running = true;
      drawGeo();
    }
    function stop() {
      running = false;
      window.cancelAnimationFrame(raf);
    }
    function boot() {
      resizeGeo();
      seed();
      start();
    }
    if ("requestIdleCallback" in window) window.requestIdleCallback(boot, { timeout: 1200 });
    else window.addEventListener("load", boot);
    window.addEventListener("resize", function () { resizeGeo(); seed(); });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) stop();
      else start();
    });
    if (motionBtn) {
      motionBtn.addEventListener("click", function () {
        if (paused) { stop(); gctx && gctx.clearRect(0, 0, window.innerWidth, window.innerHeight); }
        else start();
      });
    }
  }

  if (window.location.hash) {
    var target = document.getElementById(window.location.hash.slice(1));
    if (target) target.setAttribute("tabindex", "-1");
  }
})();
