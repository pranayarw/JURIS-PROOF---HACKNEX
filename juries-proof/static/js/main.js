/* JURIES PROOF - UI interactions */
(function () {
  "use strict";

  // Theme toggle (persisted)
  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var html = document.documentElement;
      var next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
      html.setAttribute("data-theme", next);
      try { localStorage.setItem("jp-theme", next); } catch (e) {}
    });
  }

  // Dropzone highlight
  var dz = document.querySelector(".dropzone");
  if (dz) {
    var form = dz.closest("form");
    ["dragenter", "dragover"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("drag"); });
    });
    dz.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length && form) {
        var input = form.querySelector('input[type=file]');
        if (input) { input.files = e.dataTransfer.files; form.submit(); }
      }
    });
  }

  // Document processing status polling
  var statusCells = document.querySelectorAll("[data-status-url]");
  if (statusCells.length) {
    var poll = function () {
      var pending = false;
      statusCells.forEach(function (cell) {
        var st = cell.getAttribute("data-current");
        if (st === "ready" || st === "failed") return;
        pending = true;
        fetch(cell.getAttribute("data-status-url"))
          .then(function (r) { return r.json(); })
          .then(function (j) {
            cell.setAttribute("data-current", j.status);
            var badge = cell.querySelector(".badge");
            if (!badge) return;
            badge.textContent = j.status.toUpperCase();
            badge.className = "badge badge-" + j.status;
            var err = cell.parentElement.querySelector(".proc-error");
            if (err) err.textContent = j.error || "";
            if (j.status === "ready" || j.status === "failed") setTimeout(function(){ location.reload(); }, 900);
          }).catch(function () {});
      });
      if (pending) setTimeout(poll, 1500);
    };
    poll();
  }

  // Simple SVG evidence graph
  var svg = document.getElementById("evidence-graph");
  if (svg) {
    var caseId = svg.getAttribute("data-case-id");
    fetch("/api/evidence/graph/" + caseId)
      .then(function (r) { return r.json(); })
      .then(function (data) { renderGraph(svg, data); })
      .catch(function () {
        svg.outerHTML = "<p class='meta'>No evidence graph available yet.</p>";
      });
  }

  function renderGraph(svg, data) {
    var nodes = data.nodes || [], links = data.links || [];
    if (!nodes.length) {
      svg.outerHTML = "<p class='meta'>No claims yet — ask evidence questions or run a review to build the graph.</p>";
      return;
    }
    var W = 900, H = Math.max(420, nodes.length * 34);
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("width", "100%");
    var ns = "http://www.w3.org/2000/svg";
    var levels = { case: 0, claim: 1, document: 2, page: 3 };
    var pos = {}, cols = {};
    nodes.forEach(function (n) {
      var lv = levels[n.kind] !== undefined ? levels[n.kind] : 1;
      cols[lv] = (cols[lv] || 0) + 1;
    });
    var rowIdx = {};
    nodes.forEach(function (n) {
      var lv = levels[n.kind] !== undefined ? levels[n.kind] : 1;
      rowIdx[lv] = (rowIdx[lv] || 0) + 1;
      pos[n.id] = {
        x: 60 + lv * 250,
        y: (H / (cols[lv] + 1)) * rowIdx[lv] - 10
      };
    });
    links.forEach(function (l) {
      var a = pos[l.source], b = pos[l.target];
      if (!a || !b) return;
      var line = document.createElementNS(ns, "line");
      line.setAttribute("x1", a.x + 150); line.setAttribute("y1", a.y + 10);
      line.setAttribute("x2", b.x); line.setAttribute("y2", b.y + 10);
      line.setAttribute("stroke", "var(--border)");
      line.setAttribute("stroke-width", "1.2");
      svg.appendChild(line);
    });
    nodes.forEach(function (n) {
      var p = pos[n.id];
      var g = document.createElementNS(ns, "g");
      var rect = document.createElementNS(ns, "rect");
      rect.setAttribute("x", p.x); rect.setAttribute("y", p.y);
      rect.setAttribute("rx", 5);
      rect.setAttribute("width", 150); rect.setAttribute("height", 22);
      var kindClass = { case: "g-case", claim: "g-claim", document: "g-doc", page: "g-page" }[n.kind] || "g-claim";
      rect.setAttribute("class", kindClass);
      var text = document.createElementNS(ns, "text");
      text.setAttribute("x", p.x + 6); text.setAttribute("y", p.y + 15);
      text.setAttribute("font-size", "10");
      text.textContent = n.label;
      var title = document.createElementNS(ns, "title");
      title.textContent = (n.kind.toUpperCase()) + ": " + n.label + (n.status ? " — " + n.status : "");
      g.appendChild(rect); g.appendChild(text); g.appendChild(title);
      svg.appendChild(g);
    });
  }
})();
