"""routes/downloads.py - serve a completed job's report files."""

import logging

from flask import Blueprint, abort, current_app, send_from_directory

bp = Blueprint("downloads", __name__)
log = logging.getLogger("routes.downloads")

FILES = {
    "issues": ("issues.csv", "text/csv"),
    "report": ("report.pdf", "application/pdf"),
    "summary": ("run_summary.csv", "text/csv"),
}


@bp.route("/audit/<job_id>/download/<kind>")
def download(job_id: str, kind: str):
    if kind not in FILES:
        abort(404)
    store = current_app.config["JOB_STORE"]
    job = store.get(job_id)
    if job is None or job["status"] != "completed":
        abort(404)

    filename, mimetype = FILES[kind]
    job_dir = store.job_dir(job_id)
    if not (job_dir / filename).is_file():
        log.warning("[job %s] Missing deliverable %s", job_id, filename)
        abort(404)

    slug = job.get("slug") or "site"
    download_name = f"{slug}-{filename}"
    return send_from_directory(job_dir, filename, mimetype=mimetype,
                               as_attachment=True, download_name=download_name)
