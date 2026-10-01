/* Small progressive enhancements. Every page works without JavaScript. */
(function () {
  "use strict";

  // Language menu and other selects that should submit on change.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });

  // Confirmation for irreversible-looking actions (archiving, waiving…).
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) { event.preventDefault(); }
    });
  });

  // Show / hide the original text under a machine translation.
  document.querySelectorAll("[data-toggle-original]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-toggle-original"));
      if (target) {
        var shown = target.classList.toggle("show");
        button.setAttribute("aria-expanded", shown ? "true" : "false");
      }
    });
  });

  // A tab row wider than the screen scrolls sideways: keep the current tab in view.
  document.querySelectorAll(".tabs").forEach(function (tabs) {
    var active = tabs.querySelector(".active");
    if (!active || tabs.scrollWidth <= tabs.clientWidth) { return; }
    var row = tabs.getBoundingClientRect(), tab = active.getBoundingClientRect();
    if (tab.left < row.left || tab.right > row.right) {
      tabs.scrollLeft += tab.left < row.left ? tab.left - row.left - 16 : tab.right - row.right + 16;
    }
  });

  document.querySelectorAll("[data-reload]").forEach(function (button) {
    button.addEventListener("click", function () { window.location.reload(); });
  });

  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () { window.print(); });
  });

  // Select the whole ready-to-send message with one click, ready to copy.
  document.querySelectorAll("textarea[data-select-all]").forEach(function (area) {
    area.addEventListener("focus", function () { area.select(); });
  });

  // Desktop edition: after "Save and restart", wait until the new portal process answers.
  var waiting = document.querySelector("[data-wait-restart]");
  if (waiting) {
    var next = waiting.getAttribute("data-wait-restart");
    var oldBoot = waiting.getAttribute("data-boot");
    var check = function () {
      fetch(waiting.getAttribute("data-health-url"), { cache: "no-store" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) { if (data && data.boot !== oldBoot) { window.location.assign(next); } })
        .catch(function () { /* still restarting */ });
    };
    window.setInterval(check, 1500);
  }

  // Bring a highlighted unit/item into view (packing page ?hl=…).
  var highlighted = document.querySelector(".hl");
  if (highlighted && highlighted.scrollIntoView) {
    highlighted.scrollIntoView({ block: "center" });
  }

  // Near-real-time: ask the server every 30 s whether someone else changed
  // something visible on this page; if so, offer a refresh (never auto-reload
  // while someone may be typing).
  var body = document.body;
  var since = body.getAttribute("data-watch-since");
  var url = body.getAttribute("data-changes-url");
  if (since !== null && url) {
    var consignment = body.getAttribute("data-watch-consignment") || "";
    var banner = document.getElementById("update-banner");
    var poll = function () {
      if (document.visibilityState !== "visible") { return; }
      var query = "?since=" + encodeURIComponent(since) + (consignment ? "&consignment=" + encodeURIComponent(consignment) : "");
      fetch(url + query, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data) { return; }
          if (data.count > 0 && banner) { banner.classList.add("show"); }
          var badge = document.querySelector("[data-unread]");
          if (badge && typeof data.unread === "number") { badge.textContent = String(data.unread); }
        })
        .catch(function () { /* offline for a moment: try again next tick */ });
    };
    window.setInterval(poll, 30000);
    document.addEventListener("visibilitychange", poll);
  }
})();
