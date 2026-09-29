/* Replyworthy landing — vanilla JS, no dependencies.

   Everything here is progressive enhancement: with JS off (or
   prefers-reduced-motion on) the page renders complete and static.

   Reveal-on-scroll is driven by a plain scroll check instead of
   IntersectionObserver, so content can never get stuck hidden in
   environments that throttle observer callbacks. */

(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var coarse = window.matchMedia("(pointer: coarse)").matches;
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  /* Declared up here because the reduced-motion branch below returns early
     and still needs them. */
  var EMAIL = "Hi Dr. Lee, noticed Cedar Park Dental has 200+ five-star reviews " +
              "but no way to book online. Mind if I show you how nearby clinics " +
              "turn that traffic into booked chairs?";
  /* 8 = every row the cycle pushes, so nothing is trimmed and "Scanning Austin,
     TX" stays on screen to the end. The box heights in styles.css are sized to
     hold all 8; raising this without raising those would clip the newest row. */
  var MAX_ROWS = 8;

  /* ---------- sticky header state ---------- */
  var head = $(".site-head");
  function headState() {
    if (head) head.classList.toggle("scrolled", window.scrollY > 24);
  }
  window.addEventListener("scroll", headState, { passive: true });
  headState();

  /* ---------- mobile nav ---------- */
  var toggle = $("#navToggle");
  var nav = $("#siteNav");
  if (toggle && nav) {
    var setNav = function (open) {
      nav.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    toggle.addEventListener("click", function () {
      setNav(toggle.getAttribute("aria-expanded") !== "true");
    });
    nav.addEventListener("click", function (e) {
      if (e.target.tagName === "A") setNav(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setNav(false);
    });
    window.addEventListener("resize", function () {
      if (window.innerWidth > 900) setNav(false);
    }, { passive: true });
  }

  /* CSS hides .console-wrap below 560px, where the demo could never fit. Behind
     display:none the loop would still churn a timer every 17ms through the whole
     typewriter, forever, for nothing. offsetParent is null exactly when an
     ancestor is display:none, so this asks the layout rather than duplicating
     the breakpoint here. */
  function consoleVisible() {
    var log = $("#log");
    return !!log && log.offsetParent !== null;
  }

  /* Starts the console the first time it is actually on screen, and picks it up
     on rotate or resize if it appears later. `started` keeps a second call from
     running a competing timer chain against the same element. */
  var started = false;
  function startConsoleWhenVisible(finished) {
    if (started || !consoleVisible()) return;
    started = true;
    renderConsole(finished);
  }
  window.addEventListener("resize", function () {
    startConsoleWhenVisible(reduced);
  }, { passive: true });

  if (reduced) {
    // Static page from here on: show the finished console, skip every loop.
    startConsoleWhenVisible(true);
    return;
  }

  function inView(el, margin) {
    var r = el.getBoundingClientRect();
    return r.top < window.innerHeight - (margin || 0) && r.bottom > 0;
  }

  /* ---------- count-up numbers (setInterval survives throttling) ---------- */
  function countUp(el) {
    var target = parseInt(el.getAttribute("data-count"), 10);
    var steps = 30;
    var i = 0;
    var timer = setInterval(function () {
      i++;
      var p = i / steps;
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased);
      if (i >= steps) { el.textContent = target; clearInterval(timer); }
    }, 33);
  }

  /* ---------- hero word rotor ---------- */
  function startRotor() {
    var rotor = $("#rotor");
    if (!rotor) return;
    var words = ["found.", "emailed.", "followed up.", "closed."];
    var i = 0;
    setTimeout(function tick() {
      i = (i + 1) % words.length;
      rotor.style.transform = "translateY(-105%)";
      rotor.style.opacity = "0";
      setTimeout(function () {
        rotor.textContent = words[i];
        rotor.style.transition = "none";
        rotor.style.transform = "translateY(105%)";
        void rotor.offsetHeight; // force reflow so the jump does not animate
        rotor.style.transition = "transform .34s cubic-bezier(.2,.8,.2,1), opacity .34s ease";
        rotor.style.transform = "translateY(0)";
        rotor.style.opacity = "1";
        setTimeout(tick, 2100);
      }, 340);
    }, 1400);
  }

  /* ---------- pricing card tilt ---------- */
  function initTilt() {
    if (coarse) return;
    $$("[data-tilt]").forEach(function (card) {
      card.addEventListener("mousemove", function (ev) {
        var r = card.getBoundingClientRect();
        var px = (ev.clientX - r.left) / r.width - 0.5;
        var py = (ev.clientY - r.top) / r.height - 0.5;
        card.style.transform = "perspective(900px) rotateX(" + (-py * 4).toFixed(2) +
          "deg) rotateY(" + (px * 4).toFixed(2) + "deg) translateY(-4px)";
      });
      card.addEventListener("mouseleave", function () { card.style.transform = "none"; });
    });
  }

  /* ---------- live agent console ---------- */
  function el(html) {
    var d = document.createElement("div");
    d.innerHTML = html.trim();
    return d.firstChild;
  }
  function statusRow(text, opts) {
    opts = opts || {};
    return el('<div class="c-line"><i class="c-dot' + (opts.active ? " on" : "") +
      '"></i><span>' + text + "</span>" +
      (opts.time ? "<em>" + opts.time + "</em>" : "") + "</div>");
  }
  function leadRow() {
    return el('<div class="c-lead"><b>Cedar Park Dental</b>' +
      "<span>4.9 stars &middot; 212 reviews &middot; no online booking</span></div>");
  }
  function mailRow() {
    return el('<div class="c-mail"><div class="to">To: hello@cedarparkdental.com</div>' +
      '<div class="body"></div></div>');
  }
  function replyRow() {
    return el('<div class="c-reply"><div class="who">Reply from Dr. Lee</div>' +
      '<div class="said">&ldquo;What does this cost?&rdquo;</div></div>');
  }
  function winRow() {
    return el('<div class="c-win"><i></i>Marked interested &middot; pinging you now</div>');
  }

  /* Renders the console. `finished` draws the completed state in one shot
     (used for reduced motion); otherwise it runs the loop. */
  function renderConsole(finished) {
    var log = $("#log");
    if (!log) return;
    log.innerHTML = "";

    if (finished) {
      [statusRow("Scanning Austin, TX", { active: true }),
       statusRow("142 businesses qualified", { time: "9:01 AM" }),
       leadRow(),
       replyRow(),
       winRow()].forEach(function (n) { log.appendChild(n); });
      var m = mailRow();
      log.insertBefore(m, log.children[3]);
      $(".body", m).textContent = EMAIL;
      return;
    }

    var push = function (node) {
      log.appendChild(node);
      while (log.children.length > MAX_ROWS) log.removeChild(log.firstChild);
      return node;
    };
    var at = function (ms, fn) { setTimeout(fn, ms); };

    var cycle = function () {
      log.innerHTML = "";
      push(statusRow("Scanning Austin, TX", { active: true }));
      at(1100, function () {
        push(statusRow("142 businesses qualified", { time: "9:01 AM" }));
        at(900, function () {
          push(leadRow());
          at(1100, function () {
            push(statusRow("Reading their site, writing a personal email", { active: true }));
            at(900, function () { typeEmail(cycleTail); });
          });
        });
      });
    };

    var typeEmail = function (done) {
      var wrap = push(mailRow());
      var body = $(".body", wrap);
      var caret = document.createElement("span");
      caret.className = "c-caret";
      caret.textContent = "▋";
      body.appendChild(caret);
      var i = 0;
      var step = function () {
        if (!document.body.contains(body)) { done(); return; }
        if (i >= EMAIL.length) {
          at(500, function () { if (caret.parentNode) caret.remove(); done(); });
          return;
        }
        caret.insertAdjacentText("beforebegin", EMAIL[i]);
        i++;
        at(EMAIL[i - 1] === " " ? 26 : 17, step);
      };
      step();
    };

    var cycleTail = function () {
      at(400, function () {
        push(statusRow("Sent &middot; follow-up queued for Tue 8:30 AM", { time: "9:04 AM" }));
        at(1400, function () {
          push(replyRow());
          at(1300, function () {
            push(winRow());
            at(3400, cycle);
          });
        });
      });
    };

    at(600, cycle);
  }

  /* ---------- one scroll-driven engine for all triggers ---------- */
  var pending = [];

  $$("[data-reveal]").forEach(function (el, i) {
    el.style.transitionDelay = (i % 4) * 70 + "ms";
    pending.push({ el: el, margin: 40, fire: function () { el.classList.add("in"); } });
  });

  $$(".count").forEach(function (el) {
    pending.push({ el: el, margin: 0, fire: function () { countUp(el); } });
  });

  var ticking = false;
  function check() {
    ticking = false;
    for (var i = pending.length - 1; i >= 0; i--) {
      if (inView(pending[i].el, pending[i].margin)) {
        pending[i].fire();
        pending.splice(i, 1);
      }
    }
    if (!pending.length) {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    }
  }
  function onScroll() {
    if (!ticking) {
      ticking = true;
      setTimeout(check, 60);
    }
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  check(); // reveal whatever is already on screen

  startRotor();
  initTilt();
  startConsoleWhenVisible(false);

  /* ---------- last-resort safety: nothing stays invisible ---------- */
  setTimeout(function () {
    $$("[data-reveal]:not(.in)").forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.top < window.innerHeight && r.bottom > 0) el.classList.add("in");
    });
  }, 4000);
})();
