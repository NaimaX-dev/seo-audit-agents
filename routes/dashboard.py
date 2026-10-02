"""routes/dashboard.py - home dashboard and audit history."""

from flask import Blueprint, current_app, render_template

from services.health import system_status
from services.lead_store import LeadStore

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def home():
    store = current_app.config["JOB_STORE"]
    cfg = current_app.config["SEO_CONFIG"]
    all_jobs = store.list()
    jobs = all_jobs[:8]
    status = system_status(cfg)
    completed = [j for j in all_jobs if j["status"] == "completed"]
    # Read-only counts over the existing job list, purely for the dashboard's
    # summary cards - no change to how jobs are created, run, or stored.
    stats = {
        "total_audits": len(all_jobs),
        "completed": len(completed),
        "running": sum(1 for j in all_jobs if j["status"] == "running"),
        "queued": sum(1 for j in all_jobs if j["status"] == "queued"),
        "failed": sum(1 for j in all_jobs if j["status"] == "failed"),
        "active": store.count_active(),
        "total_issues": sum(j["result"]["analysis"]["total_issues"] for j in completed),
        "last_issue_count": completed[0]["result"]["analysis"]["total_issues"] if completed else None,
    }
    # Lead Discovery (Agent 5) cards. A problem reading the lead files must never
    # break the dashboard, so fall back to zeros.
    try:
        lead_stats = LeadStore(cfg.leads_csv).summary(store)
    except Exception:  # noqa: BLE001
        lead_stats = {"total_leads": 0, "with_website": 0, "audited": 0,
                      "potential_clients": 0, "latest": None, "runs": 0}
    return render_template("dashboard.html", jobs=jobs, status=status, stats=stats, cfg=cfg,
                           lead_stats=lead_stats)


@bp.route("/history")
def history():
    store = current_app.config["JOB_STORE"]
    jobs = store.list()
    return render_template("history.html", jobs=jobs)
