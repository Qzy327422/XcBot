(function () {
  "use strict";
  var doc = document, root = doc.documentElement;

  // theme
  var stored = null;
  try { stored = localStorage.getItem("xcbot-theme"); } catch (e) {}
  if (stored) { root.setAttribute("data-theme", stored); }
  var toggle = doc.querySelector(".theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("xcbot-theme", next); } catch (e) {}
    });
  }

  // mobile menu
  var menuBtn = doc.querySelector(".menu-toggle");
  var links = doc.getElementById("nav-links");
  if (menuBtn && links) {
    menuBtn.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    links.addEventListener("click", function (e) {
      if (e.target.tagName === "A") {
        links.classList.remove("open");
        menuBtn.setAttribute("aria-expanded", "false");
      }
    });
  }

  // sticky header shadow
  var header = doc.querySelector(".site-header");
  function onScroll() {
    if (header) { header.classList.toggle("scrolled", window.scrollY > 12); }
  }
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });

  // reveal on scroll
  var reveals = doc.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && reveals.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("visible"); io.unobserve(en.target); }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
    reveals.forEach(function (el) { io.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add("visible"); });
  }

  // copy buttons
  doc.querySelectorAll(".copy-button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      var done = function () {
        var old = btn.textContent; btn.textContent = "已复制";
        setTimeout(function () { btn.textContent = old; }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var ta = doc.createElement("textarea");
        ta.value = text; doc.body.appendChild(ta); ta.select();
        try { doc.execCommand("copy"); } catch (e) {}
        doc.body.removeChild(ta); done();
      }
    });
  });

  // year
  var year = doc.getElementById("year");
  if (year) { year.textContent = new Date().getFullYear(); }

  // docs scroll spy
  var spy = doc.querySelectorAll(".docs-sidebar a[href^='#']");
  if (spy.length && "IntersectionObserver" in window) {
    var map = {};
    spy.forEach(function (a) { map[a.getAttribute("href").slice(1)] = a; });
    var secObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          spy.forEach(function (a) { a.classList.remove("active"); });
          var active = map[en.target.id];
          if (active) { active.classList.add("active"); }
        }
      });
    }, { rootMargin: "-40% 0px -55% 0px" });
    doc.querySelectorAll(".docs-content section[id]").forEach(function (s) { secObserver.observe(s); });
  }
})();
