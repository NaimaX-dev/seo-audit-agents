"""routes/leads.py - Lead Discovery page (Agent 5) and the hand-off to the audit pipeline.

    GET  /leads                  the page: search form + lead table
    POST /leads/discover         "Find Businesses"  -> Agent 5 (Playwright browser scraper of
                                 Google Maps; takes a minute or two) -> data/leads.csv
    POST /leads/audit            "Run SEO Audit"    -> one normal audit job per selected website
    GET  /leads/status.json      audit status of every lead (the page polls this)
    GET  /leads/download/<kind>  latest.csv (last discovery) | all.csv (data/leads.csv)

Audits started here are ordinary jobs created through the same JobStore /
AuditRunner as the manual form, so Agent 2 -> 3/4 -> PDF and the History page
work unchanged. The only difference is that the job carries the business's
details (job["lead"]) so the report and results page can show them.
"""

import logging

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, send_file, url_for,
)

from agents.agent5_google_maps import (
    MAX_RESULTS_LIMIT, GoogleMapsError, GoogleMapsLeadAgent, scraper_status,
)
from services.lead_store import (
    ACTIVE_STATUSES, STATUS_NOT_AUDITED, STATUS_QUEUED, LeadStore,
)

bp = Blueprint("leads", __name__)
log = logging.getLogger("routes.leads")

MAX_FIELD_LENGTH = 80


def _store() -> LeadStore:
    return LeadStore(current_app.config["SEO_CONFIG"].leads_csv)


def _render(form=None, view="latest", status=200):
    cfg = current_app.config["SEO_CONFIG"]
    store = _store()
    leads = store.all() if view == "all" else store.latest_batch()
    runs = store.discoveries()
    last = runs[-1] if runs else None
    # Pre-fill the form with the last search so "Find again" is one click.
    if form is None and last:
        form = {"keyword": last["keyword"], "location": last["location"]}
    scraper_ok, scraper_message = scraper_status()
    return render_template(
        "leads.html", cfg=cfg, form=form, leads=leads, view=view, last=last,
        total_saved=len(store.all()), scraper_ok=scraper_ok, scraper_message=scraper_message,
        max_results_limit=MAX_RESULTS_LIMIT, active=sum(1 for r in leads if r["audit_status"] in ACTIVE_STATUSES),
    ), status


@bp.route("/leads")
def index():
    view = "all" if request.args.get("view") == "all" else "latest"
    body, status = _render(view=view)
    return body, status


@bp.route("/leads/discover", methods=["POST"])
def discover():
    cfg = current_app.config["SEO_CONFIG"]
    keyword = " ".join((request.form.get("keyword") or "").split())
    location = " ".join((request.form.get("location") or "").split())

    def fail(message: str):
        flash(message, "danger")
        body, status = _render(form=request.form, status=400)
        return body, status

    if not keyword:
        return fail("Please enter a business keyword, for example Dentists.")
    if not location:
        return fail("Please enter a location, for example Lahore.")
    if len(keyword) > MAX_FIELD_LENGTH or len(location) > MAX_FIELD_LENGTH:
        return fail(f"Keyword and location must be at most {MAX_FIELD_LENGTH} characters.")
    try:
        max_results = int((request.form.get("max_results") or "25").strip())
    except ValueError:
        return fail("Max results must be a whole number.")
    if not (1 <= max_results <= MAX_RESULTS_LIMIT):
        return fail(f"Max results must be between 1 and {MAX_RESULTS_LIMIT}.")

    agent = GoogleMapsLeadAgent(cfg)
    try:
        result = agent.discover(keyword, location, max_results)
    except GoogleMapsError as exc:
        log.warning("Lead discovery failed: %s", exc)
        return fail(str(exc))
    except Exception:  # noqa: BLE001 - never show a stack trace for a failed search
        log.exception("Unexpected error during lead discovery")
        return fail("Lead discovery failed unexpectedly. Check logs/seo_audit.log for details.")

    store = _store()
    _stamp, new, updated = store.add_discovery(result)
    try:
        agent.export_csv(result.leads, store.last_export_path)
    except OSError:
        log.warning("Could not write %s", store.last_export_path, exc_info=True)

    if not result.leads:
        flash(f"Google Maps found {result.businesses_found} business(es) for \"{keyword}\" in {location}, "
              "but none had a website that can be audited. Try a broader keyword or location.", "warning")
    else:
        parts = [f"Found {len(result.leads)} business(es) with a website for \"{keyword}\" in {location}"
                 f" ({new} new, {updated} already saved)."]
        skipped = []
        if result.skipped_no_website:
            skipped.append(f"{result.skipped_no_website} without a website")
        if result.skipped_not_a_website:
            skipped.append(f"{result.skipped_not_a_website} with only a social/link page")
        if result.skipped_duplicates:
            skipped.append(f"{result.skipped_duplicates} duplicate(s)")
        if result.skipped_unreadable:
            skipped.append(f"{result.skipped_unreadable} that could not be read")
        if skipped:
            parts.append("Skipped: " + ", ".join(skipped) + ".")
        flash(" ".join(parts), "success")
    return redirect(url_for("leads.index"))


@bp.route("/leads/audit", methods=["POST"])
def run_audit():
    store = _store()
    jobs = current_app.config["JOB_STORE"]
    runner = current_app.config["AUDIT_RUNNER"]

    selected = request.form.getlist("lead_id")
    if not selected:
        flash("Select at least one business to audit.", "warning")
        return redirect(request.referrer or url_for("leads.index"))

    queued = already = 0
    first_job = None
    for lid in dict.fromkeys(selected):            # keep order, ignore repeats
        lead = store.get(lid)
        if lead is None:
            continue
        existing = jobs.find_active_for_url(lead["website"])
        if existing:
            already += 1
            first_job = first_job or existing
            continue
        snapshot = {k: lead[k] for k in ("id", "business_name", "address", "phone", "website")}
        job = jobs.create(lead["website"], None, None, lead=snapshot)   # default engine / page limit
        store.set_status(lid, STATUS_QUEUED, job_id=job["id"])
        runner.submit(job["id"])
        queued += 1
        first_job = first_job or job
        log.info("Queued lead audit job %s for %s", job["id"], lead["website"])

    if queued:
        msg = f"Started SEO audits for {queued} website(s)."
        if already:
            msg += f" {already} more were already being audited."
        flash(msg + " Audits run one after another; this page updates as they finish.", "success")
    elif already:
        flash("Those websites are already being audited.", "info")
    else:
        flash("None of the selected businesses could be found. Run the search again.", "warning")
    return redirect(request.referrer or url_for("leads.index"))


@bp.route("/leads/status.json")
def status_json():
    return jsonify({
        r["id"]: {"status": r["audit_status"], "job_id": r["job_id"], "audit_date": r["audit_date"]}
        for r in _store().all()
    })


@bp.route("/leads/download/<kind>")
def download(kind: str):
    store = _store()
    if kind == "latest":
        path, name = store.last_export_path, "lead-discovery-latest.csv"
    elif kind == "all":
        path, name = store.path, "leads.csv"
    else:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="text/csv", as_attachment=True, download_name=name)
