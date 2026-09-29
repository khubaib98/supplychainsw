/*
 * Scanner: keyboard-wedge scanners (they "type" the code + Enter), manual
 * entry, or the phone camera. The camera uses the browser's BarcodeDetector
 * when available and falls back to jsQR (served locally, loaded on demand) –
 * important in China, where many Android phones lack Google Play services.
 */
(function () {
  "use strict";
  var root = document.getElementById("scanner");
  if (!root) { return; }
  var d = root.dataset;
  var csrf = document.querySelector('meta[name="csrf-token"]').getAttribute("content");
  var input = document.getElementById("scan-code");
  var log = document.getElementById("scan-log");
  var empty = document.getElementById("scan-empty");
  var targetBox = document.getElementById("scan-target");
  var target = null;

  function mode() {
    var checked = root.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "lookup";
  }

  function showModePanels() {
    var current = mode();
    root.querySelectorAll("[data-show-mode]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-show-mode") !== current;
    });
    renderTarget();
    input.focus();
  }

  function renderTarget() {
    if (!targetBox) { return; }
    targetBox.textContent = "";
    if (target) {
      targetBox.classList.add("set");
      var strong = document.createElement("strong");
      strong.textContent = d.msgTarget + ": ";
      targetBox.appendChild(strong);
      var code = document.createElement("span");
      code.className = "code";
      code.textContent = target.label;
      targetBox.appendChild(code);
    } else {
      targetBox.classList.remove("set");
      targetBox.textContent = d.msgTargetNone;
    }
  }

  function addLog(ok, message, info) {
    if (empty) { empty.hidden = true; }
    var li = document.createElement("li");
    li.className = ok ? "ok" : "fail";
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", ok ? "#i-check" : "#i-x");
    svg.appendChild(use);
    li.appendChild(svg);
    var body = document.createElement("div");
    var text = document.createElement("div");
    text.textContent = message;
    body.appendChild(text);
    if (info && info.where) {
      var where = document.createElement("div");
      where.className = "where";
      where.textContent = info.where;
      body.appendChild(where);
    }
    if (info && info.url) {
      var link = document.createElement("a");
      link.href = info.url;
      link.textContent = d.msgOpen + " →";
      body.appendChild(link);
    }
    li.appendChild(body);
    log.insertBefore(li, log.firstChild);
    if (!ok && navigator.vibrate) { navigator.vibrate([80, 60, 80]); }
  }

  function submit(raw) {
    var code = (raw || "").trim();
    if (!code) { return; }
    var current = mode();
    var payload = { code: code, mode: current, consignment: d.consignment || "" };
    if (current === "move") {
      if (!target) {
        payload.mode = "target";
      } else {
        payload.target = target.code;
        payload.reason = document.getElementById("scan-reason").value;
        payload.place = document.getElementById("scan-place-move").value;
      }
    } else if (current === "receive") {
      payload.condition = document.getElementById("scan-condition").value;
      payload.include_contents = document.getElementById("scan-contents").checked;
      payload.place = document.getElementById("scan-place-receive").value;
    }
    fetch(d.api, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, "Accept": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (payload.mode === "target" && data.ok) {
          target = { code: data.code, label: data.label };
          renderTarget();
        }
        addLog(data.ok, data.message, data);
      })
      .catch(function () { addLog(false, d.msgNetwork, null); });
  }

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      submit(input.value);
      input.value = "";
    }
  });

  root.querySelectorAll('input[name="mode"]').forEach(function (radio) {
    if (radio.value === d.initialMode) { radio.checked = true; }
    radio.addEventListener("change", showModePanels);
  });
  var clear = document.getElementById("scan-clear-target");
  if (clear) { clear.addEventListener("click", function () { target = null; renderTarget(); input.focus(); }); }
  showModePanels();

  // ---- camera ------------------------------------------------------------
  var button = document.getElementById("scan-camera");
  var view = document.getElementById("scan-camera-view");
  var video = document.getElementById("scan-video");
  var canvas = document.getElementById("scan-canvas");
  var context = canvas.getContext("2d", { willReadFrequently: true });
  var stream = null;
  var detector = null;
  var running = false;
  var lastCode = "";
  var lastTime = 0;

  function loadJsQR() {
    return new Promise(function (resolve, reject) {
      if (window.jsQR) { resolve(); return; }
      var script = document.createElement("script");
      script.src = d.jsqr;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  function found(code) {
    var now = Date.now();
    if (code === lastCode && now - lastTime < 3000) { return; }
    lastCode = code;
    lastTime = now;
    submit(code);
  }

  function tick() {
    if (!running) { return; }
    var next = function () { window.setTimeout(function () { window.requestAnimationFrame(tick); }, 160); };
    if (video.readyState < 2) { next(); return; }
    if (detector) {
      detector.detect(video).then(function (codes) {
        if (codes.length) { found(codes[0].rawValue); }
        next();
      }).catch(function () { detector = null; next(); });
      return;
    }
    if (window.jsQR) {
      var scale = Math.min(1, 640 / (video.videoWidth || 640));
      var width = Math.round(video.videoWidth * scale);
      var height = Math.round(video.videoHeight * scale);
      canvas.width = width;
      canvas.height = height;
      context.drawImage(video, 0, 0, width, height);
      var image = context.getImageData(0, 0, width, height);
      var result = window.jsQR(image.data, width, height, { inversionAttempts: "dontInvert" });
      if (result && result.data) { found(result.data); }
    }
    next();
  }

  function stopCamera() {
    running = false;
    if (stream) { stream.getTracks().forEach(function (track) { track.stop(); }); }
    stream = null;
    view.classList.remove("on");
  }

  function startCamera() {
    if (!window.isSecureContext) { addLog(false, d.msgInsecure, null); return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { addLog(false, d.msgCameraError, null); return; }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then(function (media) {
        stream = media;
        video.srcObject = media;
        view.classList.add("on");
        return video.play();
      })
      .then(function () {
        if ("BarcodeDetector" in window) {
          return window.BarcodeDetector.getSupportedFormats().then(function (formats) {
            if (formats.indexOf("qr_code") !== -1) { detector = new window.BarcodeDetector({ formats: ["qr_code"] }); }
          }).catch(function () { detector = null; });
        }
      })
      .then(function () { return detector ? null : loadJsQR(); })
      .then(function () { running = true; tick(); })
      .catch(function () { stopCamera(); addLog(false, d.msgCameraError, null); });
  }

  button.addEventListener("click", function () {
    if (running || stream) { stopCamera(); } else { startCamera(); }
  });
  window.addEventListener("pagehide", stopCamera);
})();
