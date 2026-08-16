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
