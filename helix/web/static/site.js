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
        b.setAttribute("aria-selected", idx === i ? "true" : "false");
      });
    }
    function tick() { show(i + 1); }
    function start() { timer = window.setInterval(tick, 4200); }
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
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      start();
    }
  });

  var cursor = document.querySelector("[data-cursor]");
  if (cursor && window.matchMedia("(pointer:fine)").matches) {
    window.addEventListener("pointermove", function (e) {
      cursor.style.opacity = "1";
      cursor.style.transform = "translate3d(" + e.clientX + "px," + e.clientY + "px,0)";
    });
    window.addEventListener("pointerleave", function () {
      cursor.style.opacity = "0";
    });
  }
})();
