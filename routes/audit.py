"""routes/audit.py - start an audit, show progress, and show the results page."""

import logging

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)

from services.url_validation import InvalidURLError, validate_url

bp = Blueprint("audit", __name__)
log = logging.getLogger("routes.audit")

MAX_PAGES_MIN, MAX_PAGES_MAX = 1, 1000


@bp.route("/audit/new", methods=["GET", "POST"])
def new_audit():
    cfg = current_app.config["SEO_CONFIG"]
    if request.method == "GET":
        return render_template("audit_form.html", cfg=cfg)

    store = current_app.config["JOB_STORE"]
    runner = current_app.config["AUDIT_RUNNER"]

    try:
        url = validate_url(request.form.get("url"))
    except InvalidURLError as exc:
        flash(str(exc), "danger")
        return render_template("audit_form.html", cfg=cfg, form=request.form), 400

    engine_requested = request.form.get("engine") or None
    if engine_requested not in (None, "agent3", "agent4"):
        flash("Unknown analysis engine selected.", "danger")
        return render_template("audit_form.html", cfg=cfg, form=request.form), 400
    if engine_requested == "agent3" and not cfg.enable_agent3:
        flash("AI (Ollama) analysis is disabled in this deployment's config.json.", "danger")
        return render_template("audit_form.html", cfg=cfg, form=request.form), 400
    if engine_requested == "agent4" and not cfg.enable_agent4:
        flash("Rule-based analysis is disabled in this deployment's config.json.", "danger")
        return render_template("audit_form.html", cfg=cfg, form=request.form), 400

    max_pages = None
    raw_max_pages = (request.form.get("max_pages") or "").strip()
    if raw_max_pages:
        try:
            max_pages = int(raw_max_pages)
        except ValueError:
            flash("Max pages must be a whole number.", "danger")
            return render_template("audit_form.html", cfg=cfg, form=request.form), 400
        if not (MAX_PAGES_MIN <= max_pages <= MAX_PAGES_MAX):
            flash(f"Max pages must be between {MAX_PAGES_MIN} and {MAX_PAGES_MAX}.", "danger")
            return render_template("audit_form.html", cfg=cfg, form=request.form), 400

    existing = store.find_active_for_url(url)
    if existing:
        flash(f"{url} is already being audited.", "info")
        return redirect(url_for("audit.progress", job_id=existing["id"]))

    job = store.create(url, engine_requested, max_pages)
    runner.submit(job["id"])
    log.info("Queued audit job %s for %s", job["id"], url)
    return redirect(url_for("audit.progress", job_id=job["id"]))


@bp.route("/audit/<job_id>")
def progress(job_id: str):
    store = current_app.config["JOB_STORE"]
    job = store.get(job_id)
    if job is None:
        return render_template("errors/404.html", message="That audit could not be found."), 404
    if job["status"] == "completed":
        return redirect(url_for("audit.results", job_id=job_id))
    return render_template("audit_progress.html", job=job)


@bp.route("/audit/<job_id>/status.json")
def status_json(job_id: str):
    store = current_app.config["JOB_STORE"]
    job = store.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    job["log"] = job["log"][-40:]   # the poller only needs the recent tail
    return jsonify(job)


@bp.route("/audit/<job_id>/results")
def results(job_id: str):
    store = current_app.config["JOB_STORE"]
    job = store.get(job_id)
    if job is None:
        return render_template("errors/404.html", message="That audit could not be found."), 404
    if job["status"] == "failed":
        return render_template("audit_failed.html", job=job)
    if job["status"] != "completed":
        return redirect(url_for("audit.progress", job_id=job_id))
    return render_template("results.html", job=job, result=job["result"])
