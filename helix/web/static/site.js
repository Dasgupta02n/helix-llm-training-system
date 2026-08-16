(function () {
  var btn = document.querySelector("[data-nav-toggle]");
  var nav = document.getElementById("siteNav");
  if (btn && nav) {
    btn.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.textContent = open ? "Close" : "Menu";
    });
  }

  document.querySelectorAll("[data-film]").forEach(function (film) {
    var slides = Array.prototype.slice.call(film.querySelectorAll("[data-slide]"));
    var buttons = Array.prototype.slice.call(film.querySelectorAll("[data-film-go]"));
    if (!slides.length) return;
    var i = 0;
    var timer;
    function show(n) {
      i = (n + slides.length) % slides.length;
      slides.forEach(function (s, idx) {
        s.classList.toggle("is-on", idx === i);
      });
      buttons.forEach(function (b, idx) {
        b.classList.toggle("is-on", idx === i);
      });
    }
    function start() { timer = window.setInterval(function () { show(i + 1); }, 4200); }
    function stop() { if (timer) window.clearInterval(timer); }
    buttons.forEach(function (b, idx) {
      b.addEventListener("click", function () {
        stop();
        show(idx);
        start();
      });
    });
    film.addEventListener("mouseenter", stop);
    film.addEventListener("mouseleave", start);
    show(0);
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) start();
  });

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
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
})();
