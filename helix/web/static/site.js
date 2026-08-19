(function () {
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var btn = document.querySelector("[data-nav-toggle]");
  var nav = document.getElementById("siteNav");
  if (btn && nav) {
    btn.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.textContent = open ? "Close" : "Menu";
    });
  }

  var loader = document.querySelector("[data-loader]");
  var hero = document.querySelector("[data-hero]");
  var places = ["India", "Tiruppur", "Pune", "Hyderabad", "On your desk"];
  function readyHero() {
    if (hero) hero.classList.add("is-ready");
  }
  if (loader && !reduce) {
    var countEl = loader.querySelector("[data-load-count]");
    var placeEl = loader.querySelector("[data-load-place]");
    var n = 0;
    var started = Date.now();
    function tick() {
      var t = Math.min(1, (Date.now() - started) / 1700);
      n = Math.round(t * 100);
      if (countEl) countEl.textContent = n < 10 ? "0" + n : String(n);
      if (placeEl) placeEl.textContent = places[Math.min(places.length - 1, Math.floor(t * places.length))];
      if (t < 1) {
        window.requestAnimationFrame(tick);
      } else {
        loader.classList.add("is-done");
        window.setTimeout(readyHero, 200);
      }
    }
    window.requestAnimationFrame(tick);
  } else {
    if (loader) loader.classList.add("is-done");
    readyHero();
  }

  var globe = document.querySelector(".uc-hero-globe");
  if (globe && !reduce && window.matchMedia("(pointer:fine)").matches) {
    var gx = 0, gy = 0, tx = 0, ty = 0;
    window.addEventListener("pointermove", function (e) {
      tx = (e.clientX / window.innerWidth - 0.5) * 24;
      ty = (e.clientY / window.innerHeight - 0.5) * 16;
    });
    (function loop() {
      gx += (tx - gx) * 0.06;
      gy += (ty - gy) * 0.06;
      globe.style.transform = "translate3d(" + gx + "px," + gy + "px,0)";
      window.requestAnimationFrame(loop);
    })();
  }

  document.querySelectorAll("[data-film]").forEach(function (film) {
    var slides = Array.prototype.slice.call(film.querySelectorAll("[data-slide]"));
    var buttons = Array.prototype.slice.call(film.querySelectorAll("[data-film-go]"));
    if (!slides.length) return;
    var i = 0;
    var timer;
    function show(n) {
      i = (n + slides.length) % slides.length;
      slides.forEach(function (s, idx) { s.classList.toggle("is-on", idx === i); });
      buttons.forEach(function (b, idx) { b.classList.toggle("is-on", idx === i); });
    }
    function start() { timer = window.setInterval(function () { show(i + 1); }, 4200); }
    function stop() { if (timer) window.clearInterval(timer); }
    buttons.forEach(function (b, idx) {
      b.addEventListener("click", function () { stop(); show(idx); start(); });
    });
    film.addEventListener("mouseenter", stop);
    film.addEventListener("mouseleave", start);
    show(0);
    if (!reduce) start();
  });

  var reveals = document.querySelectorAll("[data-reveal]");
  if (reduce || !("IntersectionObserver" in window)) {
    Array.prototype.forEach.call(reveals, function (el) { el.classList.add("is-in"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.18 });
    Array.prototype.forEach.call(reveals, function (el) { io.observe(el); });
  }

  var cine = document.querySelector("[data-cine]");
  if (cine) {
    var FRAME_COUNT = parseInt(cine.getAttribute("data-frame-count"), 10) || 43;
    var canvas = document.getElementById("cineCanvas");
    var spacer = document.getElementById("cineSpacer");
    var ctx = canvas ? canvas.getContext("2d", { alpha: false }) : null;
    var frames = new Array(FRAME_COUNT);
    var last = -1;
    function path(i) {
      var n = String(i + 1);
      while (n.length < 4) n = "0" + n;
      return "/static/site/cinema/frames/frame_" + n + ".jpg";
    }
    function cover(img) {
      var cw = window.innerWidth, ch = window.innerHeight;
      var ir = img.width / img.height, cr = cw / ch;
      var dw, dh, dx, dy;
      if (cr > ir) { dw = cw; dh = cw / ir; dx = 0; dy = (ch - dh) / 2; }
      else { dh = ch; dw = ch * ir; dy = 0; dx = (cw - dw) / 2; }
      ctx.drawImage(img, dx, dy, dw, dh);
    }
    function draw(i) {
      if (!ctx || !frames[i]) return;
      last = i;
      cover(frames[i]);
    }
    function resize() {
      if (!canvas || !ctx) return;
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(window.innerWidth * dpr);
      canvas.height = Math.floor(window.innerHeight * dpr);
      canvas.style.width = window.innerWidth + "px";
      canvas.style.height = window.innerHeight + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw(last < 0 ? 0 : last);
    }
    function preload() {
      var i = 0;
      function batch() {
        var end = Math.min(i + 8, FRAME_COUNT);
        for (; i < end; i++) {
          (function (idx) {
            var img = new Image();
            img.onload = function () {
              frames[idx] = img;
              if (idx === 0) draw(0);
            };
            img.src = path(idx);
          })(i);
        }
        if (i < FRAME_COUNT) window.requestAnimationFrame(batch);
      }
      batch();
    }
    function progress() {
      var max = document.documentElement.scrollHeight - window.innerHeight;
      if (max <= 0) return 0;
      return Math.min(1, Math.max(0, window.scrollY / max));
    }
    function onScroll() {
      var p = progress();
      document.documentElement.style.setProperty("--scroll-p", String(p));
      var idx = reduce ? FRAME_COUNT - 1 : Math.round(p * (FRAME_COUNT - 1));
      if (idx !== last) draw(idx);
      cine.querySelectorAll("[data-cine-range]").forEach(function (el) {
        var parts = el.getAttribute("data-cine-range").split(",");
        var a = parseFloat(parts[0]), b = parseFloat(parts[1]);
        el.classList.toggle("is-on", p >= a && p <= b);
      });
    }
    var ticking = false;
    window.addEventListener("scroll", function () {
      if (!ticking) {
        window.requestAnimationFrame(function () { onScroll(); ticking = false; });
        ticking = true;
      }
    }, { passive: true });
    window.addEventListener("resize", resize);
    if (spacer && !reduce) spacer.style.height = "420vh";
    preload();
    resize();
    onScroll();
  }

  var counts = document.querySelectorAll("[data-count]");
  if (counts.length) {
    function runCount(el) {
      var end = parseInt(el.getAttribute("data-count"), 10) || 0;
      if (reduce) { el.textContent = String(end); return; }
      var startAt = Date.now();
      (function step() {
        var p = Math.min(1, (Date.now() - startAt) / 900);
        el.textContent = String(Math.round(end * p));
        if (p < 1) window.requestAnimationFrame(step);
      })();
    }
    if (!("IntersectionObserver" in window)) {
      Array.prototype.forEach.call(counts, runCount);
    } else {
      var cio = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            runCount(entry.target);
            cio.unobserve(entry.target);
          }
        });
      }, { threshold: 0.4 });
      Array.prototype.forEach.call(counts, function (el) { cio.observe(el); });
    }
  }
})();
