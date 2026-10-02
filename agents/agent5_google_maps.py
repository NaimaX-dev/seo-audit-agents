"""
agents/agent5_google_maps.py - Agent 5: Google Maps Lead Discovery (Playwright scraper).

Responsibility
--------------
Turn "<keyword> + <location>" (e.g. Dentists + Lahore) into a clean list of
businesses that HAVE a website, so the existing pipeline can audit them:

    keyword + location
      -> launch a browser (Playwright / Chromium) and open Google Maps
      -> type "<keyword> in <location>" into the Maps search box
      -> scroll the results list until enough businesses are loaded
      -> open each business and read: name, address, phone, website
      -> clean + de-duplicate + drop businesses without a (real) website
      -> Lead list  (-> saved to data/leads.csv by the web UI, -> Agent 2 audits them)

No Google API, API key, Google Cloud account or billing is used. The only
requirement is a browser: ``pip install playwright`` and ``playwright install chromium``.

Keeping the scraper working
---------------------------
Google Maps has no public markup contract and changes its HTML from time to
time. Every selector the scraper depends on is a named constant in the
"SELECTORS" block below, so if Google changes the page only that block needs
editing. Identifiers like ``data-item-id="address"`` are used on purpose because
they do not depend on the page language or on generated class names.

Cleaning rules
--------------
* a business without a website is dropped
* website URLs are normalised with Agent 1's own rules (scheme added, host
  lower-cased, fragment removed), Google redirect wrappers are unwrapped and
  tracking parameters (utm_*, gclid, ...) are stripped
* social-media / link-in-bio / messaging URLs (facebook.com, instagram.com,
  wa.me, ...) are not websites that can be SEO-audited, so they are dropped
* duplicate businesses (same Google place id, or same name + address) are dropped
* duplicate websites (same host + path, ignoring ``www.`` and a trailing slash)
  are dropped, which also collapses chains that share one site

CSV export columns: business_name, address, phone, website

Run it on its own (saves to data/leads.csv like the web page does):
    python -m agents.agent5_google_maps "Dentists" "Lahore" --max 10
"""

import csv
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from agents.agent1_reader import FileReaderAgent
from config import Config

MAPS_URL = "https://www.google.com/maps"
MAX_RESULTS_LIMIT = 60          # practical cap per search (each business is opened one by one)
MAX_LISTINGS = 150              # never scroll the results list past this many cards
MAX_SCROLL_ROUNDS = 60
SCROLL_PAUSE_MS = 1200
STALE_ROUNDS_BEFORE_STOP = 4    # stop scrolling after this many rounds with no new cards
MAX_CONSECUTIVE_READ_FAILURES = 6

CSV_COLUMNS = ["business_name", "address", "phone", "website"]

# ------------------------------------------------------------------ SELECTORS
# Everything Google-Maps-specific lives here. Edit this block if Google changes its page.
SEL_SEARCH_BOX = 'input#searchboxinput, input[name="q"]'
SEL_FEED = 'div[role="feed"]'                              # the scrolling results list
SEL_LISTING_LINKS = 'div[role="feed"] a[href*="/maps/place/"]'   # one link per business card
SEL_PLACE_TITLE = "h1.DUwDvf, h1.fontHeadlineLarge"        # business name on its detail panel
SEL_ADDRESS = 'button[data-item-id="address"]'
SEL_PHONE = 'button[data-item-id^="phone"]'
SEL_WEBSITE = 'a[data-item-id="authority"]'
SEL_ITEM_TEXT = ".Io6YTe"                                  # visible text inside an info button
RE_END_OF_LIST = re.compile(r"reached the end of the list", re.I)
RE_NO_RESULTS = re.compile(r"can't find|no results found", re.I)
RE_CONSENT_BUTTON = re.compile(r"^(accept all|reject all|i agree|accept)$", re.I)
# -----------------------------------------------------------------------------

# Hosts that are profiles / link pages / messengers, not auditable websites.
NON_WEBSITE_HOSTS = {
    "facebook.com", "fb.com", "fb.me", "m.facebook.com", "instagram.com", "linkedin.com",
    "twitter.com", "x.com", "tiktok.com", "youtube.com", "youtu.be", "pinterest.com",
    "snapchat.com", "wa.me", "api.whatsapp.com", "whatsapp.com", "t.me", "telegram.me",
    "linktr.ee", "g.page", "goo.gl", "maps.app.goo.gl", "google.com",
}
_TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "igshid", "mc_cid", "mc_eid", "_ga", "ref", "y_source"}
_PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
_JUNK_CHARS_RE = re.compile(r"[\ue000-\uf8ff\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

# One browser at a time: concurrent scrapes would look like a bot and fight for the UI.
_SCRAPE_LOCK = threading.Lock()


class GoogleMapsError(RuntimeError):
    """Raised with a user-facing message when lead discovery cannot run."""


@dataclass
class Lead:
    """One business discovered on Google Maps."""

    business_name: str
    address: str
    phone: str
    website: str          # cleaned, absolute http(s) URL
    place_id: str = ""

    def as_row(self) -> dict[str, str]:
        return {c: getattr(self, c) for c in CSV_COLUMNS}


@dataclass
class DiscoveryResult:
    """What ``discover()`` returns: the leads plus counters for the UI."""

    keyword: str
    location: str
    leads: list[Lead] = field(default_factory=list)
    businesses_found: int = 0         # unique businesses opened and read
    business_keys: list[str] = field(default_factory=list)   # their identities (lifetime totals)
    skipped_no_website: int = 0
    skipped_not_a_website: int = 0    # social / link-in-bio / invalid URL
    skipped_duplicates: int = 0       # duplicate business or duplicate website
    skipped_unreadable: int = 0       # businesses whose page did not load / could not be read


@dataclass
class _Listing:
    """A business card found in the results list (before its details are read)."""

    key: str
    name: str
    href: str


# --------------------------------------------------------------------------
# Pure helpers (module level so they are easy to test)
# --------------------------------------------------------------------------
def website_key(url: str) -> str:
    """Identity of a website for de-duplication: host + path, no www, no trailing slash."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return f"{host}{parsed.path.rstrip('/')}"


def unwrap_google_redirect(raw: str) -> str:
    """Maps sometimes wraps outgoing links as google.com/url?q=<real url>; return the real one."""
    parsed = urlparse(raw.strip())
    if (parsed.hostname or "").lower().endswith("google.com") and parsed.path in ("/url", "/aclk"):
        qs = parse_qs(parsed.query)
        for key in ("q", "url", "adurl"):
            if qs.get(key):
                return unquote(qs[key][0])
    return raw


def clean_website_url(raw: object) -> str | None:
    """Return a clean absolute http(s) URL or None if ``raw`` is empty/unusable."""
    value = str(raw or "").strip().strip("\"'<>")
    if not value:
        return None
    value = unwrap_google_redirect(value)
    url = FileReaderAgent._normalize_url(value)     # same rules as the CLI / web form
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        return None
    query = urlencode([
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ])
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", query, ""))


def is_auditable_website(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return not any(host == bad or host.endswith("." + bad) for bad in NON_WEBSITE_HOSTS)


def clean_text(value: object) -> str:
    """Collapse whitespace and drop the invisible / icon-font characters Maps puts in labels."""
    return " ".join(_JUNK_CHARS_RE.sub(" ", str(value or "")).split())


def place_key(href: str) -> str:
    """Stable identity of a Maps place URL: its 0x..:0x.. feature id, else its name slug."""
    match = _PLACE_ID_RE.search(href or "")
    if match:
        return match.group(1).lower()
    path = unquote(urlparse(href or "").path)
    slug = path.split("/maps/place/", 1)[-1].split("/", 1)[0]
    return slug.lower()


def scraper_status() -> tuple[bool, str]:
    """(ready, message) - whether the Playwright package is installed (browser is checked on launch)."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False, ("Playwright is not installed. Run  pip install playwright  and then  "
                       "playwright install chromium  and restart the app.")
    return True, ""


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
class GoogleMapsLeadAgent:
    name = "Agent5-GoogleMaps"
    MAPS_URL = MAPS_URL             # overridable (the tests point it at a local mock page)

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.log = logging.getLogger(self.name)

    # ------------------------------------------------------------------ public
    def preflight(self) -> None:
        ready, message = scraper_status()
        if not ready:
            raise GoogleMapsError(message)

    def discover(self, keyword: str, location: str, max_results: int = 25) -> DiscoveryResult:
        """Find up to ``max_results`` businesses WITH a website for keyword + location."""
        keyword, location = " ".join(keyword.split()), " ".join(location.split())
        if not keyword or not location:
            raise GoogleMapsError("Both a business keyword and a location are required.")
        if not (1 <= max_results <= MAX_RESULTS_LIMIT):
            raise GoogleMapsError(f"Max results must be between 1 and {MAX_RESULTS_LIMIT}.")
        self.preflight()

        if not _SCRAPE_LOCK.acquire(blocking=False):
            raise GoogleMapsError("Another Lead Discovery search is already running. "
                                  "Wait for it to finish, then try again.")
        try:
            return self._scrape(keyword, location, max_results)
        finally:
            _SCRAPE_LOCK.release()

    @staticmethod
    def export_csv(leads: list[Lead], path: Path) -> Path:
        """Write ``leads`` to ``path`` with columns business_name,address,phone,website."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for lead in leads:
                writer.writerow(lead.as_row())
        return path

    # ---------------------------------------------------------------- scraping
    def _scrape(self, keyword: str, location: str, max_results: int) -> DiscoveryResult:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright

        query = f"{keyword} in {location}"
        timeout_ms = int(self.cfg.maps_timeout_seconds * 1000)
        want_cards = min(max(max_results * 3, 15), MAX_LISTINGS)
        result = DiscoveryResult(keyword=keyword, location=location)
        self.log.info("Searching Google Maps in a browser: %r (want %d leads with a website)", query, max_results)

        try:
            with sync_playwright() as pw:
                browser = self._launch(pw, PlaywrightError)
                try:
                    context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
                    context.set_default_timeout(timeout_ms)
                    page = context.new_page()
                    listings = self._search(page, query, want_cards, PlaywrightTimeout)
                    self.log.info("Collected %d business card(s) from the results list", len(listings))
                    self._read_listings(context, listings, result, max_results, PlaywrightTimeout, PlaywrightError)
                finally:
                    browser.close()
        except GoogleMapsError:
            raise
        except PlaywrightTimeout as exc:
            raise GoogleMapsError(
                "Google Maps did not respond in time. The page may have changed, loaded slowly, or "
                "blocked the automated browser. Try again, or raise 'maps_timeout_seconds' in config.json."
            ) from exc
        except PlaywrightError as exc:
            raise GoogleMapsError(f"The browser stopped while scraping Google Maps: {self._short(exc)}") from exc

        result.leads = result.leads[:max_results]
        self.log.info(
            "Agent 5 found %d lead(s) with a website (%d businesses read; %d without website, "
            "%d not a real website, %d duplicates, %d unreadable)",
            len(result.leads), result.businesses_found, result.skipped_no_website,
            result.skipped_not_a_website, result.skipped_duplicates, result.skipped_unreadable,
        )
        return result

    def _launch(self, pw, playwright_error):
        options: dict = {"headless": bool(self.cfg.maps_headless)}
        channel = (self.cfg.maps_browser_channel or "").strip()
        if channel:
            options["channel"] = channel
        try:
            return pw.chromium.launch(**options)
        except playwright_error as exc:
            text = str(exc)
            if "Executable doesn't exist" in text or "playwright install" in text:
                raise GoogleMapsError(
                    "The browser for Agent 5 is not installed. Run  playwright install chromium  "
                    "(or set \"maps_browser_channel\": \"chrome\" in config.json to use Google Chrome)."
                ) from exc
            if "X server" in text or "DISPLAY" in text:
                raise GoogleMapsError("No display is available for a visible browser. "
                                      "Set \"maps_headless\": true in config.json.") from exc
            raise GoogleMapsError(f"Could not start the browser: {self._short(exc)}") from exc

    # -- step 1: open Maps, search, scroll the results ------------------------
    def _search(self, page, query: str, want_cards: int, timeout_error) -> list[_Listing]:
        page.goto(f"{self.MAPS_URL}?hl=en", wait_until="domcontentloaded")
        self._dismiss_consent(page)
        self._raise_if_blocked(page)

        box = page.locator(SEL_SEARCH_BOX).first
        try:
            box.wait_for(state="visible")
        except timeout_error:
            self._raise_if_blocked(page)
            raise
        box.click()
        box.fill(query)
        box.press("Enter")

        try:
            page.wait_for_selector(f"{SEL_FEED}, {SEL_PLACE_TITLE}")
        except timeout_error:
            self._raise_if_blocked(page)
            if RE_NO_RESULTS.search(page.inner_text("body") or ""):
                return []
            raise

        if page.locator(SEL_FEED).count() == 0:
            # Maps jumped straight to a single business instead of a list.
            return [_Listing(place_key(page.url), "", page.url)]
        return self._scroll_results(page, want_cards)

    def _scroll_results(self, page, want_cards: int) -> list[_Listing]:
        feed = page.locator(SEL_FEED).first
        found: dict[str, _Listing] = {}
        stale = 0
        for round_no in range(MAX_SCROLL_ROUNDS):
            items = page.eval_on_selector_all(
                SEL_LISTING_LINKS,
                "els => els.map(e => ({href: e.href, name: e.getAttribute('aria-label') || ''}))",
            )
            before = len(found)
            for item in items:
                key = place_key(item["href"])
                if key and key not in found:
                    found[key] = _Listing(key, clean_text(item["name"]), item["href"])
            self.log.debug("Scroll round %d: %d card(s)", round_no, len(found))
            if len(found) >= want_cards:
                break
            if page.get_by_text(RE_END_OF_LIST).count() > 0:
                break
            stale = stale + 1 if len(found) == before else 0
            if stale >= STALE_ROUNDS_BEFORE_STOP:
                break
            feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
            page.wait_for_timeout(SCROLL_PAUSE_MS)
        return list(found.values())[:want_cards]

    # -- step 2: open every business and read its details ---------------------
    def _read_listings(self, context, listings: list[_Listing], result: DiscoveryResult,
                       max_results: int, timeout_error, playwright_error) -> None:
        if not listings:
            return
        detail = context.new_page()
        # Photos, video and fonts are not needed to read text; skipping them makes each business load faster.
        detail.route("**/*", lambda route: route.abort()
                     if route.request.resource_type in {"image", "media", "font"} else route.continue_())

        seen_places: set[str] = set()
        seen_websites: set[str] = set()
        failures = 0
        for index, listing in enumerate(listings, start=1):
            if len(result.leads) >= max_results:
                break
            place = None
            for attempt in (1, 2):
                try:
                    place = self._read_place(detail, listing, timeout_error)
                    break
                except timeout_error:
                    self._raise_if_blocked(detail)
                    self.log.warning("Business %d/%d timed out (attempt %d): %s",
                                     index, len(listings), attempt, listing.name or listing.key)
                except playwright_error as exc:
                    self.log.warning("Business %d/%d failed: %s", index, len(listings), self._short(exc))
                    break
            if place is None:
                result.skipped_unreadable += 1
                failures += 1
                if failures >= MAX_CONSECUTIVE_READ_FAILURES:
                    if result.leads:
                        self.log.warning("Too many unreadable businesses in a row - stopping early.")
                        break
                    raise GoogleMapsError(
                        "Google Maps results loaded, but business details could not be read. Google may have "
                        "changed its page layout or is blocking the browser. Try again later, or set "
                        "\"maps_headless\": false in config.json."
                    )
                continue
            failures = 0
            self._consume(place, result, seen_places, seen_websites)
            self.log.info("Read %d/%d: %s -> %s", index, len(listings), place["name"] or "?",
                          place["website"] or "no website")

    def _read_place(self, page, listing: _Listing, timeout_error) -> dict:
        page.goto(listing.href, wait_until="domcontentloaded")
        page.wait_for_selector(SEL_PLACE_TITLE)
        try:
            # Address is on almost every business; once it is there the rest of the panel is too.
            page.wait_for_selector(SEL_ADDRESS, timeout=min(6000, self.cfg.maps_timeout_seconds * 1000))
        except timeout_error:
            pass
        page.wait_for_timeout(250)
        self._raise_if_blocked(page)

        name = clean_text(page.locator(SEL_PLACE_TITLE).first.inner_text()) or listing.name
        website = ""
        site_link = page.locator(SEL_WEBSITE).first
        if site_link.count():
            website = (site_link.get_attribute("href") or "").strip()
        return {
            "id": listing.key,
            "name": name,
            "address": self._item_text(page, SEL_ADDRESS),
            "phone": self._item_text(page, SEL_PHONE),
            "website": website,
        }

    @staticmethod
    def _item_text(page, selector: str) -> str:
        """Visible text of an info row ('Address', 'Phone'), whatever language Maps is showing."""
        item = page.locator(selector).first
        if item.count() == 0:
            return ""
        inner = item.locator(SEL_ITEM_TEXT).first
        text = inner.inner_text() if inner.count() else ""
        if not clean_text(text):
            label = item.get_attribute("aria-label") or ""
            text = label.split(":", 1)[1] if ":" in label else label
        if not clean_text(text):
            text = item.inner_text()
        return clean_text(text)

    # -- helpers ---------------------------------------------------------------
    def _dismiss_consent(self, page) -> None:
        """Click through Google's cookie/consent page when it shows up (common in the EU)."""
        try:
            on_consent_page = "consent." in urlparse(page.url).netloc
            button = page.get_by_role("button", name=RE_CONSENT_BUTTON).first
            if on_consent_page or button.is_visible():
                self.log.info("Dismissing the Google consent screen")
                button.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded")
        except Exception:  # noqa: BLE001 - no consent screen, or an unfamiliar one: carry on
            self.log.debug("No consent screen handled", exc_info=True)

    @staticmethod
    def _raise_if_blocked(page) -> None:
        url = page.url or ""
        text = ""
        try:
            text = page.inner_text("body")[:3000].lower()
        except Exception:  # noqa: BLE001
            pass
        if "/sorry/" in url or "unusual traffic" in text or "not a robot" in text:
            raise GoogleMapsError(
                "Google is asking for a CAPTCHA (it detected automated traffic). Wait a while before "
                "searching again, search fewer results, or run with \"maps_headless\": false."
            )

    def _consume(self, place: dict, result: DiscoveryResult,
                 seen_places: set[str], seen_websites: set[str]) -> None:
        name, address, phone = clean_text(place["name"]), clean_text(place["address"]), clean_text(place["phone"])
        if not name:
            result.skipped_unreadable += 1
            return

        business_key = place["id"] or f"{name.lower()}|{address.lower()}"
        name_addr_key = f"{name.lower()}|{address.lower()}"
        if business_key in seen_places or name_addr_key in seen_places:
            result.skipped_duplicates += 1
            return
        seen_places.update({business_key, name_addr_key})
        result.businesses_found += 1
        result.business_keys.append(business_key)

        raw_site = place["website"]
        if not str(raw_site or "").strip():
            result.skipped_no_website += 1
            return
        website = clean_website_url(raw_site)
        if website is None or not is_auditable_website(website):
            result.skipped_not_a_website += 1
            return
        key = website_key(website)
        if key in seen_websites:
            result.skipped_duplicates += 1
            return
        seen_websites.add(key)
        result.leads.append(Lead(name, address, phone, website, place["id"]))

    @staticmethod
    def _short(exc: Exception) -> str:
        return " ".join(str(exc).split())[:200]


# --------------------------------------------------------------------------
# Stand-alone use:  python -m agents.agent5_google_maps "Dentists" "Lahore" --max 10
# --------------------------------------------------------------------------
def _main() -> int:
    import argparse

    from config import load_config
    from services.lead_store import LeadStore
    from utils.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="Agent 5: find businesses with websites on Google Maps.")
    parser.add_argument("keyword")
    parser.add_argument("location")
    parser.add_argument("--max", type=int, default=25, dest="max_results")
    parser.add_argument("--headless", action="store_true", help="hide the browser window")
    args = parser.parse_args()

    cfg = load_config(overrides={"maps_headless": True} if args.headless else None)
    setup_logging(cfg.logs_dir, cfg.log_level)
    try:
        result = GoogleMapsLeadAgent(cfg).discover(args.keyword, args.location, args.max_results)
    except GoogleMapsError as exc:
        print(f"Error: {exc}")
        return 1
    store = LeadStore(cfg.leads_csv)
    _stamp, new, updated = store.add_discovery(result)
    GoogleMapsLeadAgent.export_csv(result.leads, store.last_export_path)
    print(f"{len(result.leads)} lead(s) with a website saved to {cfg.leads_csv} ({new} new, {updated} already saved)")
    for lead in result.leads:
        print(f"  {lead.business_name} | {lead.phone} | {lead.website}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
