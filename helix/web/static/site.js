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
