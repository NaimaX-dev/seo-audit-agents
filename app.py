"""
app.py - Flask web application for seo-audit-agents.

This file only wires the web layer together; the audit pipeline itself is the
untouched agents/ package (see services/audit_runner.py, which calls it).

Run:
    python app.py                       # http://127.0.0.1:5000
    FLASK_DEBUG=1 python app.py         # auto-reload while developing

The CLI (`python main.py`) still works exactly as before and is unaffected by
anything in this file.
"""

import logging
import os
import secrets
from pathlib import Path

from flask import Flask, render_template, send_from_directory

from config import ConfigError, load_config
from routes.audit import bp as audit_bp
from routes.dashboard import bp as dashboard_bp
from routes.downloads import bp as downloads_bp
from routes.leads import bp as leads_bp
from services.audit_runner import AuditRunner
from services.job_store import JobStore
from utils.logging_setup import setup_logging

PROJECT_ROOT = Path(__file__).resolve().parent


def create_app() -> Flask:
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    setup_logging(cfg.logs_dir, cfg.log_level)
    log = logging.getLogger("app")

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024   # web form only, no uploads
    # flash() messages (form errors, "audits started") live in a signed cookie and need a
    # secret key; without one every flash() raised a 500. No login exists, so a random
    # per-start key is enough. Set SEO_SECRET_KEY to keep messages across restarts.
    app.secret_key = os.environ.get("SEO_SECRET_KEY") or secrets.token_hex(32)
    app.config["SEO_CONFIG"] = cfg

    jobs_dir = cfg.reports_dir / "_jobs"
    store = JobStore(jobs_dir)
    recovered = store.mark_interrupted()
    if recovered:
        log.warning("Marked %d job(s) as failed after a restart", recovered)

    app.config["JOB_STORE"] = store
    app.config["AUDIT_RUNNER"] = AuditRunner(store)

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(downloads_bp)
    app.register_blueprint(leads_bp)

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(_exc):
        return render_template(
            "errors/generic.html",
            title="Request too large",
            message="The form submission was larger than allowed.",
        ), 413

    @app.errorhandler(500)
    def server_error(exc):
        log.exception("Unhandled server error")
        return render_template(
            "errors/generic.html",
            title="Something went wrong",
            message="An unexpected error occurred. Check logs/seo_audit.log for details.",
        ), 500

    log.info("Flask app ready (engine default: %s)",
              "agent4" if cfg.enable_agent4 else "agent3")
    return app


app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes"}
    app.run(host="127.0.0.1", port=5000, debug=debug, use_reloader=debug)
