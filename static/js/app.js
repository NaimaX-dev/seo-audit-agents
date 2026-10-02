// static/js/app.js - small progressive-enhancement helpers, no framework.
(function () {
  "use strict";

  // ------------------------------------------------------------- theme toggle
  const themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const root = document.documentElement;
      const next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      try { localStorage.setItem("seo-theme", next); } catch (e) { /* storage blocked */ }
    });
  }

  // Bootstrap tooltips, if any are present.
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
    new bootstrap.Tooltip(el);
  });

  // ------------------------------------------------------ clickable table rows
  document.addEventListener("click", (evt) => {
    const row = evt.target.closest("tr[data-href]");
    if (!row) return;
    if (evt.target.closest("a, button, input, select, textarea, label")) return;
    window.location.href = row.dataset.href;
  });

  // -------------------------------------------------------- quick-fill chips
  document.querySelectorAll("[data-fill]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = document.querySelector(btn.dataset.fill);
      if (!target) return;
      target.value = btn.dataset.value;
      target.focus();
    });
  });

  // ------------------------------------------------------------- audit forms
  // Client-side URL sanity check (the server still validates) and a loading state.
  document.querySelectorAll("form[data-audit-form]").forEach((form) => {
    const input = form.querySelector("[data-audit-url]");
    const submitBtn = form.querySelector("[data-audit-submit]");
    const error = form.querySelector("[data-audit-error]");
    const idleHtml = submitBtn ? submitBtn.innerHTML : "";

    form.addEventListener("submit", (evt) => {
      const value = ((input && input.value) || "").trim();
      if (!value) {
        evt.preventDefault();
        if (input) { input.classList.add("is-invalid"); input.focus(); }
        if (error) error.hidden = false;
        return;
      }
      if (input) input.classList.remove("is-invalid");
      if (error) error.hidden = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML =
          '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>Starting audit...';
      }
    });

    if (input) {
      input.addEventListener("input", () => {
        input.classList.remove("is-invalid");
        if (error) error.hidden = true;
      });
    }

    // Coming back with the browser's Back button must not leave the button stuck.
    window.addEventListener("pageshow", (e) => {
      if (e.persisted && submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = idleHtml;
      }
    });
  });

  // ------------------------------------------------------------ history filter
  const historyBody = document.getElementById("history-body");
  if (historyBody) {
    const rows = Array.from(historyBody.querySelectorAll("tr"));
    const search = document.getElementById("history-search");
    const tabs = document.querySelectorAll(".filter-tab");
    const emptyBox = document.getElementById("history-empty");
    let filter = "all";

    function applyFilter() {
      const q = ((search && search.value) || "").trim().toLowerCase();
      let visible = 0;
      rows.forEach((row) => {
        const okStatus = filter === "all" || row.dataset.status === filter;
        const okText = !q || (row.dataset.search || "").includes(q);
        const show = okStatus && okText;
        row.hidden = !show;
        if (show) visible += 1;
      });
      if (emptyBox) emptyBox.hidden = visible !== 0;
    }

    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        tabs.forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        filter = tab.dataset.filter;
        applyFilter();
      });
    });
    if (search) search.addEventListener("input", applyFilter);
  }

  // ------------------------------------------------------- live audit progress
  const progressRoot = document.getElementById("progress-root");
  if (progressRoot) {
    const statusUrl = progressRoot.dataset.statusUrl;
    const resultsUrl = progressRoot.dataset.resultsUrl;
    const failedUrl = progressRoot.dataset.failedUrl;

    const ring = document.getElementById("progress-ring");
    const ringWrap = document.getElementById("progress-ring-wrap");
    const percent = document.getElementById("progress-percent");
    const message = document.getElementById("progress-message");
    const statusPill = document.getElementById("progress-status");
    const statusText = document.getElementById("progress-status-text");
    const elapsedEl = document.getElementById("progress-elapsed");
    const stageItems = document.querySelectorAll(".stage-item");
    const logConsole = document.getElementById("log-console");
    const warningsBox = document.getElementById("progress-warnings");

    const STAGE_ORDER = ["queued", "reading", "preflight", "crawling", "analyzing", "reporting"];
    const CIRC = 2 * Math.PI * 54;
    let startedAt = Date.parse(progressRoot.dataset.started || "") || Date.now();

    function renderProgress(value) {
      const p = Math.max(0, Math.min(100, Number(value) || 0));
      if (ring) ring.style.strokeDashoffset = String(CIRC * (1 - p / 100));
      if (percent) percent.textContent = String(p);
      if (ringWrap) ringWrap.setAttribute("aria-valuenow", String(p));
    }

    function renderStages(currentStage) {
      const idx = STAGE_ORDER.indexOf(currentStage);
      stageItems.forEach((item) => {
        const stageIdx = STAGE_ORDER.indexOf(item.dataset.stage);
        item.classList.remove("done", "active");
        if (stageIdx < idx) item.classList.add("done");
        else if (stageIdx === idx) item.classList.add("active");
      });
    }

    function renderStatus(status) {
      if (!statusPill) return;
      statusPill.className = "status-pill status-" + status;
      if (statusText) statusText.textContent = status === "running" ? "Running" : "Pending";
    }

    function renderLog(lines) {
      if (!logConsole || !lines || !lines.length) return;
      const stick = logConsole.scrollHeight - logConsole.scrollTop - logConsole.clientHeight < 48;
      logConsole.textContent = lines.join("\n");
      if (stick) logConsole.scrollTop = logConsole.scrollHeight;
    }

    function escapeHtml(str) {
      const div = document.createElement("div");
      div.textContent = str;
      return div.innerHTML;
    }

    function renderWarnings(warnings) {
      if (!warningsBox) return;
      if (!warnings || !warnings.length) {
        warningsBox.innerHTML = "";
        return;
      }
      warningsBox.innerHTML = warnings
        .map(
          (w) =>
            '<div class="alert alert-warning" role="alert"><i class="bi bi-exclamation-triangle-fill"></i>' +
            '<div class="alert-body">' + escapeHtml(w) + "</div></div>"
        )
        .join("");
    }

    function tickElapsed() {
      if (!elapsedEl) return;
      const secs = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      const m = Math.floor(secs / 60);
      const s = String(secs % 60).padStart(2, "0");
      elapsedEl.textContent = m + ":" + s;
    }
    tickElapsed();
    setInterval(tickElapsed, 1000);

    // Make sure the log starts scrolled to the newest line.
    if (logConsole) logConsole.scrollTop = logConsole.scrollHeight;

    async function poll() {
      try {
        const res = await fetch(statusUrl, { cache: "no-store" });
        if (!res.ok) throw new Error("status request failed");
        const job = await res.json();

        if (job.started_at) {
          const t = Date.parse(job.started_at);
          if (!Number.isNaN(t)) startedAt = t;
        }
        renderProgress(job.progress);
        if (message) message.textContent = job.message || "";
        renderStatus(job.status);
        renderStages(job.stage);
        renderLog(job.log);
        renderWarnings(job.warnings);

        if (job.status === "completed") {
          window.location.href = resultsUrl;
          return;
        }
        if (job.status === "failed") {
          window.location.href = failedUrl;
          return;
        }
      } catch (err) {
        // transient network hiccup - keep polling
        console.warn("progress poll failed", err);
      }
      setTimeout(poll, 1500);
    }

    poll();
  }
})();
