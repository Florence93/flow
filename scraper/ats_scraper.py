#!/usr/bin/env python3
"""
ATS monitor -> Supabase (GlobalRemoteHQ)

Finds company career boards on Greenhouse, Lever, Ashby, Workable, SmartRecruiters and
Recruitee by itself (free Common Crawl index, no API keys, no paid tools), then polls
each company's free public job-board API and keeps your Supabase `companies` and `jobs`
tables in sync:

  * first run   -> imports every existing remote job on every discovered board
  * every cycle -> inserts newly posted jobs, updates edited ones, closes removed ones

Only jobs posted by the hiring company itself are kept. Job boards, aggregators,
staffing and recruiting agencies are filtered out (see BLOCKED_NAMES / AGENCY_TEXT_RE).

US-only mode (on by default, set US_ONLY=false to turn off):
  * location names a non-US country/region/city (or worldwide)  -> skipped
  * location names the US (country, state, "US-based" ...)        -> kept
  * location is just "Remote" / empty / unrecognised              -> treated as US, but the
    job description is checked and the job is skipped if it restricts the role to another
    region (e.g. "must be located in Canada") or says it is not open to US candidates

Usage
  python ats_scraper.py --discover-only      # just build the board list and exit
  python ats_scraper.py --dry-run --once     # preview, writes nothing
  python ats_scraper.py                      # run forever
  python ats_scraper.py --once               # single pass (cron / GitHub Actions)
Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import nh3
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("ats")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "15"))
REMOTE_ONLY = os.environ.get("REMOTE_ONLY", "true").lower() != "false"
USER_AGENT = "GlobalRemoteHQ-JobMonitor/1.0 (+https://globalremotehq.com)"

# --- US-only filter ---------------------------------------------------------- #
US_ONLY = os.environ.get("US_ONLY", "true").lower() != "false"
# keep jobs that list the US together with other countries, e.g. "Remote - US, Canada"
US_ALLOW_MULTI = os.environ.get("US_ALLOW_MULTI", "false").lower() == "true"
US_STRICT = os.environ.get("US_STRICT", "true").lower() != "false"

BOARDS_CACHE = os.environ.get("BOARDS_CACHE", "discovered_boards.json")
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS", "7"))        # re-discover boards this often
CC_COLLECTIONS = int(os.environ.get("CC_COLLECTIONS", "3"))    # recent Common Crawl snapshots to scan
CC_MAX_PAGES = int(os.environ.get("CC_MAX_PAGES", "400"))      # index pages per domain per snapshot
EXCLUDE_AGENCIES = os.environ.get("EXCLUDE_AGENCIES", "true").lower() != "false"
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "7"))  # only import jobs posted this recently (0 = no limit)
IDLE_RECHECK_SECONDS = 6 * 3600  # boards with no remote jobs are re-checked this often

# --------------------------------------------------------------------------- #
# Only real hiring companies: block job boards, aggregators, staffing/recruiting firms
# --------------------------------------------------------------------------- #
BLOCKED_NAMES = [
    r"staffing", r"recruit(?:ing|ment|ers?)", r"headhunt\w*", r"executive search",
    r"bjakcareer", r"mindrift",
    r"talent (?:marketplace|network|solutions|partners|acquisition)", r"manpower",
    r"randstad", r"adecco", r"robert half", r"kforce", r"teksystems", r"insight global",
    r"hays", r"michael page", r"kelly services", r"jobot", r"toptal", r"crossover",
    r"andela", r"braintrust", r"upwork", r"fiverr", r"freelancer",
    r"jobgether", r"jobright", r"himalayas", r"flexjobs", r"remote ?ok", r"we work remotely",
    r"remote ?co", r"remotive", r"working nomads", r"jobspresso", r"powertofly",
    r"dynamite jobs", r"ziprecruiter", r"indeed", r"glassdoor", r"linkedin", r"wellfound",
    r"angellist", r"jooble", r"adzuna", r"simplyhired", r"careerbuilder", r"snagajob",
    r"lensa", r"jobs? ?(?:board|portal|aggregator|marketplace)", r"job ?search",
]
BLOCKED_COMPANY_RE = re.compile(r"\b(?:" + "|".join(BLOCKED_NAMES) + r")\b", re.I)
# phrases that show an agency is posting on behalf of someone else
AGENCY_TEXT_RE = re.compile(
    r"on behalf of (?:our|a|an|one of our) client\b"
    r"|\bour client (?:is|has|seeks|is seeking|is looking|needs)\b"
    r"|\bwe are (?:a|an) (?:\w+ ){0,2}(?:staffing|recruiting|recruitment|executive search|headhunting)\b"
    r"|\bthis (?:role|position|job) is (?:being )?(?:posted|advertised) (?:by|on behalf)\b",
    re.I,
)


def is_blocked_company(*names) -> bool:
    return any(BLOCKED_COMPANY_RE.search(re.sub(r"[-_.]+", " ", n)) for n in names if n)


def is_recent(j: "Job") -> bool:
    if MAX_AGE_DAYS <= 0:
        return True
    iso = to_iso(j.posted_at)
    if not iso:  # no usable posting date -> skip rather than guess
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    return age.total_seconds() <= MAX_AGE_DAYS * 86400

def looks_like_agency(j: "Job") -> bool:
    text = re.sub(r"<[^>]+>", " ", html.unescape(j.description_html or ""))
    return bool(AGENCY_TEXT_RE.search(text))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s or "job"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_iso(v) -> Optional[str]:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        else:
            s = str(v).replace("Z", "+00:00").replace(" UTC", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return min(dt, datetime.now(timezone.utc)).isoformat()
    except Exception:
        return None


def fmt_range(cur, lo, hi, interval=None) -> Optional[str]:
    lo, hi = lo or 0, hi or 0
    if not lo and not hi:
        return None
    amount = f"{lo:,.0f} - {hi:,.0f}" if lo and hi else f"{(lo or hi):,.0f}"
    out = f"{cur or ''} {amount}".strip()
    return f"{out} / {interval}" if interval else out


ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "h1", "h2", "h3", "h4",
    "h5", "h6", "a", "blockquote", "hr", "span", "div", "code", "pre", "table", "thead",
    "tbody", "tr", "td", "th",
}


def clean_html(s: Optional[str]) -> Optional[str]:
    """Third-party HTML gets rendered on your site, so always sanitise it."""
    if not s or not s.strip():
        return None
    out = nh3.clean(
        s,
        tags=ALLOWED_TAGS,
        attributes={"a": {"href"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    ).strip()
    return out or None


REMOTE_RE = re.compile(
    r"\b(remote|anywhere|work from home|wfh|worldwide|telecommut\w*|hybrid)\b", re.I
)
ONSITE_RE = re.compile(r"\b(on-?site|in[- ]office)\b", re.I)


def is_remote_text(*texts) -> bool:
    t = " ".join(x for x in texts if x)
    return bool(REMOTE_RE.search(t)) and not ONSITE_RE.search(t)


# --- eligibility (edit these to match how your site stores countries/states) --
COUNTRY_PATTERNS = [
    (name, re.compile(rf"(?<!\w)(?:{pat})(?!\w)"))
    for name, pat in [
        ("United States", r"(?i:United States|USA|U\.S\.A?\.?)|US"),
        ("Canada", r"(?i:Canada)"),
        ("United Kingdom", r"(?i:United Kingdom|Great Britain|England|Scotland)|UK"),
        ("Germany", r"(?i:Germany|Deutschland)"),
        ("Australia", r"(?i:Australia)"),
        ("Netherlands", r"(?i:Netherlands)"),
        ("Ireland", r"(?i:Ireland)"),
        ("France", r"(?i:France)"),
        ("Spain", r"(?i:Spain)"),
        ("Portugal", r"(?i:Portugal)"),
        ("Poland", r"(?i:Poland)"),
        ("Brazil", r"(?i:Brazil)"),
        ("Mexico", r"(?i:Mexico)"),
    ]
]
US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
STATE_PATTERNS = [(s, re.compile(rf"\b{s}\b", re.I)) for s in US_STATES]
WORLDWIDE_RE = re.compile(r"\b(worldwide|world-wide|anywhere|global(ly)?|any location)\b", re.I)


def parse_eligibility(location: str):
    """-> (eligible_countries | None, eligible_states | None, worldwide_only)"""
    if not location:
        return None, None, False
    countries = [n for n, rx in COUNTRY_PATTERNS if rx.search(location)]
    states = [n for n, rx in STATE_PATTERNS if rx.search(location)]
    if states and "United States" not in countries:
        countries.insert(0, "United States")
    worldwide = bool(WORLDWIDE_RE.search(location)) and not countries
    return (countries or None), (states or None), worldwide


# --------------------------------------------------------------------------- #
# US-only filter
#   1) location_verdict(): looks only at the location string  -> us / non_us / unknown
#   2) passes_us_description(): looks at the job description (needs the full text, so it
#      runs after the detail fetch) and decides jobs whose location was only "Remote"
# --------------------------------------------------------------------------- #
US_STATE_ABBR = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|"
    "NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
# "Austin, TX" / "US-CA" / "US CA"  (comma or US prefix required, so "IN"/"OR"/"ME" in
# normal words don't count)
STATE_ABBR_RE = re.compile(rf"(?:,\s*|\bUS[-\s]|\bU\.S\.[-\s])(?:{US_STATE_ABBR})(?![A-Za-z])")
US_LOC_RE = re.compile(
    r"(?<![\w.])(?:united states(?: of america)?|usa|u\.s\.a?\.?|us[- ]only|us[- ]based|"
    r"north america)(?!\w)",
    re.I,
)
US_BARE_RE = re.compile(r"(?<![\w.])US(?![\w.])")  # upper-case only, never the word "us"

NON_US_NAMES = [
    # countries
    r"canada", r"united kingdom", r"uk", r"great britain", r"england", r"scotland", r"wales",
    r"northern ireland", r"ireland", r"germany", r"deutschland", r"france", r"spain",
    r"portugal", r"italy", r"netherlands", r"belgium", r"switzerland", r"austria", r"sweden",
    r"norway", r"denmark", r"finland", r"poland", r"romania", r"bulgaria", r"czech(?:ia| republic)?",
    r"ukraine", r"turkey", r"israel", r"uae", r"united arab emirates", r"saudi arabia", r"egypt",
    r"nigeria", r"kenya", r"south africa", r"ghana", r"india", r"pakistan", r"bangladesh",
    r"philippines", r"indonesia", r"malaysia", r"singapore", r"vietnam", r"thailand", r"japan",
    r"china", r"korea", r"taiwan", r"hong kong", r"australia", r"new zealand", r"brazil",
    r"argentina", r"colombia", r"chile", r"peru", r"(?<!new )mexico", r"costa rica",
    # provinces / regions
    r"british columbia", r"ontario", r"quebec", r"alberta", r"emea", r"apac", r"latam",
    r"latin america", r"europe(?:an union)?", r"eu", r"asia", r"africa", r"middle east",
    # unambiguous big cities
    r"london", r"berlin", r"toronto", r"vancouver", r"amsterdam", r"bangalore", r"bengaluru",
    r"mumbai", r"lagos", r"nairobi", r"sydney", r"tokyo", r"lisbon", r"madrid", r"barcelona",
    r"warsaw", r"manila", r"sao paulo", r"mexico city",
]
NON_US_ALT = "|".join(NON_US_NAMES)
NON_US_RE = re.compile(rf"(?<!\w)(?:{NON_US_ALT})(?!\w)", re.I)


def location_verdict(location: str) -> str:
    """'us' | 'non_us' | 'unknown'. Unknown (e.g. just "Remote") is treated as US later."""
    loc = (location or "").strip()
    if not loc:
        return "unknown"
    countries, states, _ = parse_eligibility(loc)
    has_us = bool(
        (countries and "United States" in countries)
        or states
        or US_LOC_RE.search(loc)
        or US_BARE_RE.search(loc)
        or STATE_ABBR_RE.search(loc)
    )
    has_other = bool(NON_US_RE.search(loc))
    if has_us:
        return "us" if (not has_other or US_ALLOW_MULTI) else "non_us"
    if has_other:
        return "non_us"
    if WORLDWIDE_RE.search(loc):  # "Remote - Worldwide" is not US-only
        return "non_us"
    return "unknown"

US_NAMES = {"us", "usa", "united states", "united states of america"}

def job_verdict(j: "Job") -> str:
    c = (j.country or "").strip().lower()
    if c:
        return "us" if c in US_NAMES else "non_us"
    return location_verdict(j.location)

# --- description checks -------------------------------------------------------- #
# US token: the words "United States", or upper-case US / USA / U.S. (never the word "us")
_US = r"(?<![\w.])(?:united states(?: of america)?|(?-i:USA|U\.S\.A?\.?|US))(?!\w)"

# description positively says the role is for the US
DESC_US_RE = re.compile(
    r"\bmust (?:be|reside|live|be located|be based|be residing)(?: \w+){0,3} (?:in|within) the " + _US
    + r"|\b(?:located|based|residing|reside|live) (?:in|within) the " + _US
    + r"|(?<![\w.])(?:US|U\.S\.)[- ]based\b"
    + r"|\b(?:authori[sz]ed|eligible|legally authori[sz]ed) to work in the " + _US
    + r"|\bwithin the " + _US
    + r"|\bremote\b[^.]{0,60}" + _US
    + r"|" + _US + r"[- ](?:remote|only|residents?|citizens?)\b"
    + r"|\b401\(?k\)?",
    re.I,
)

# description restricts the role to another region
DESC_NON_US_RE = re.compile(
    r"\b(?:must|should|need to) (?:be|reside|live|be located|be based|be residing)(?: \w+){0,3} "
    rf"(?:in|within) (?:the )?(?:{NON_US_ALT})(?!\w)"
    rf"|\b(?:open|available|restricted|limited)(?: only)? to (?:candidates|applicants|residents)"
    rf"(?: \w+){{0,3}} (?:in|from|based in|located in) (?:the )?(?:{NON_US_ALT})(?!\w)",
    re.I,
)

# description explicitly closes the role to US candidates
DESC_NOT_US_RE = re.compile(
    r"\bnot (?:available|open|eligible|accepting)(?: \w+){0,6} (?:in|from|located in) (?:the )?" + _US,
    re.I,
)


def plain_text(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def has_us_evidence(j: "Job", text: str) -> bool:
    return bool(DESC_US_RE.search(text)) or (j.salary or "").upper().startswith(("USD", "$"))

def passes_us_description(j: "Job") -> bool:
    text = f"{plain_text(j.description_html)} {plain_text(j.requirements_html)}"
    if DESC_NOT_US_RE.search(text):
        return False
    v = job_verdict(j)
    if v == "us":
        return True
    if v == "non_us":
        return False
    if DESC_NON_US_RE.search(text) and not DESC_US_RE.search(text):
        return False
    return has_us_evidence(j, text) if US_STRICT else True


def map_job_type(raw: str, title: str) -> str:
    s = f"{raw or ''} {title or ''}".lower()
    if "freelanc" in s:
        return "freelance"
    if re.search(r"contract|temporary|\btemp\b|fixed[- ]term|consultant", s):
        return "contract"
    if re.search(r"part[- ]?time", s):
        return "partTime"
    return "fullTime"


# --- categories: first match wins; missing categories are created automatically --
CATEGORY_RULES = [
    ("security", "Security", r"security|infosec|appsec|pentest|penetration|threat"),
    ("customer-success", "Customer Success",
     r"customer (success|support|experience|service)|support (specialist|agent|engineer|representative)|technical support|onboarding specialist|implementation"),
    ("sales", "Sales",
     r"\bsales\b|account executive|\bsdr\b|\bbdr\b|business development|account manager|partnerships?"),
    ("data", "Data",
     r"data (scien|analy|engineer)|analytics|machine learning|\bml\b|\bai\b|business intelligence|\bbi\b|statistic"),
    ("it-and-support", "IT & Support",
     r"\bit (support|admin|manager|specialist)|sysadmin|system administrator|network admin|helpdesk|desktop support"),
    ("engineering", "Engineering",
     r"engineer|developer|devops|\bsre\b|software|backend|frontend|full[- ]?stack|architect|\bqa\b|sdet|programmer"),
    ("design", "Design", r"design|\bux\b|\bui\b|creative director|illustrat"),
    ("product", "Product", r"product (manager|owner|lead|director|operations)|\bpm\b|head of product|chief product"),
    ("project-management", "Project Management",
     r"project manager|program manager|scrum|delivery manager|project coordinator"),
    ("marketing", "Marketing",
     r"marketing|growth|\bseo\b|demand gen|\bbrand|social media|community|lifecycle|paid media"),
    ("content-and-writing", "Content & Writing",
     r"writer|copywrit|content|editor|journalist|communications"),
    ("hr-and-recruiting", "HR & Recruiting",
     r"recruit|talent|people (ops|partner|operations)|\bhr\b|human resources|payroll|sourcer"),
    ("finance", "Finance",
     r"financ|account(ant|ing)|controller|fp&a|billing|bookkeep|treasury|\btax\b|auditor|procurement"),
    ("legal", "Legal", r"\blegal\b|counsel|attorney|paralegal|compliance|contracts? manager"),
    ("healthcare", "Healthcare",
     r"nurse|clinician|clinical|physician|therapist|pharmacist|dental|patient care|\brn\b"),
    ("education", "Education", r"teacher|tutor|instructor|curriculum|professor|trainer|learning"),
    ("research", "Research", r"research|scientist"),
    ("operations", "Operations",
     r"operations|\bops\b|supply chain|logistics|administrat|executive assistant|office manager|coordinator|chief of staff"),
]
CATEGORY_RULES = [(s, n, re.compile(p, re.I)) for s, n, p in CATEGORY_RULES]


def classify(title: str, department: str = "") -> tuple[str, str]:
    for text in (title, department):
        for slug, name, rx in CATEGORY_RULES:
            if text and rx.search(text):
                return slug, name
    return "general", "General"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    ats: str
    board: str
    external_id: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    description_html: str = ""
    requirements_html: str = ""
    posted_at: Optional[str] = None
    job_type: str = ""
    salary: Optional[str] = None
    department: str = ""
    tags: list = field(default_factory=list)
    country: str = ""
    version: str = ""  # changes whenever the posting is edited
    detail: Optional[Callable[["Job"], Awaitable[None]]] = None  # lazy full-content fetch


@dataclass
class Source:
    ats: str
    board: str
    name: str = ""
    website: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.board}"


def ver(*parts) -> str:
    return hashlib.sha1("|".join(str(p or "") for p in parts).encode()).hexdigest()[:12]


def job_slug(j: Job, company_slug: str) -> str:
    h = hashlib.sha1(f"{j.ats}:{j.board}:{j.external_id}".encode()).hexdigest()[:8]
    return f"{slugify(j.title)[:60].strip('-')}-{company_slug[:30].strip('-')}-{h}"


MANAGED_RE = re.compile(r"-[0-9a-f]{8}$")


def is_managed(row: dict) -> bool:
    """Rows this tool created: hash-suffixed slug and no employer owner."""
    return row.get("owner_id") is None and bool(MANAGED_RE.search(row.get("slug", "")))


# --------------------------------------------------------------------------- #
# ATS adapters: each returns (company_name_hint | None, [Job])
# --------------------------------------------------------------------------- #
async def get_json(client: httpx.AsyncClient, url: str, params=None, tries: int = 3):
    for i in range(tries):
        try:
            r = await client.get(url, params=params)
            if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
                await asyncio.sleep(2 ** i + random.random())
                continue
            r.raise_for_status()
            return r.json()
        except httpx.TransportError:
            if i == tries - 1:
                raise
            await asyncio.sleep(2 ** i)


# Greenhouse ---------------------------------------------------------------- #
def _gh_detail(client, board, jid):
    async def run(job: Job):
        d = await get_json(
            client,
            f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}",
            {"pay_transparency": "true"},
        )
        job.description_html = html.unescape(d.get("content") or "")
        deps = [x["name"] for x in d.get("departments") or [] if x.get("name")]
        job.department, job.tags = ", ".join(deps), deps[:3]
        pr = d.get("pay_input_ranges") or []
        if pr:
            p = pr[0]
            job.salary = fmt_range(
                p.get("currency_type"), (p.get("min_cents") or 0) / 100, (p.get("max_cents") or 0) / 100
            )
    return run


_GH_NAMES: dict = {}


async def gh_board_name(client, board) -> Optional[str]:
    """Real company name. Raises on failure so we never fall back to a different name later."""
    if board not in _GH_NAMES:
        d = await get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board}")
        _GH_NAMES[board] = (d or {}).get("name") or ""
    return _GH_NAMES[board] or None


async def fetch_greenhouse(client, board):
    data = await get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        job = Job(
            ats="greenhouse", board=board, external_id=str(j["id"]), title=j.get("title", ""),
            url=j.get("absolute_url", ""), location=loc,
            remote=is_remote_text(loc),
            posted_at=j.get("first_published"),
            version=str(j.get("updated_at", "")),
        )
        job.detail = _gh_detail(client, board, j["id"])
        out.append(job)
    name = await gh_board_name(client, board) if any(j.remote for j in out) else None
    return name, out


# Lever --------------------------------------------------------------------- #
async def fetch_lever(client, board):
    data = await get_json(client, f"https://api.lever.co/v0/postings/{board}", {"mode": "json"})
    out = []
    for j in data:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or ([cat["location"]] if cat.get("location") else [])
        loc = " / ".join(locs)
        wt = (j.get("workplaceType") or "").lower()
        remote = wt in ("remote", "hybrid") or (wt != "on-site" and is_remote_text(loc, j.get("text")))
        parts = [j.get("description") or ""]
        for lst in j.get("lists") or []:
            parts.append(f"<h3>{html.escape(lst.get('text', ''))}</h3><ul>{lst.get('content', '')}</ul>")
        parts.append(j.get("additional") or "")
        desc = "".join(parts)
        sr = j.get("salaryRange") or {}
        salary = fmt_range(sr.get("currency"), sr.get("min"), sr.get("max"),
                           "year" if "year" in (sr.get("interval") or "") else None)
        dept = cat.get("department") or cat.get("team") or ""
        out.append(Job(
            ats="lever", board=board, external_id=j["id"], title=j.get("text", ""),
            url=j.get("hostedUrl", ""), location=loc, remote=remote, description_html=desc,
            posted_at=j.get("createdAt"), job_type=cat.get("commitment", ""), salary=salary,
            department=dept, tags=[dept] if dept else [], version=ver(desc, salary),
            country=j.get("country") or "",
        ))
    return None, out


# Ashby --------------------------------------------------------------------- #
async def fetch_ashby(client, board):
    data = await get_json(
        client, f"https://api.ashbyhq.com/posting-api/job-board/{board}",
        {"includeCompensation": "true"},
    )
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [
            (s.get("location") if isinstance(s, dict) else str(s)) for s in j.get("secondaryLocations") or []
        ]
        loc = " / ".join(x for x in locs if x)
        wt = (j.get("workplaceType") or "").lower()
        remote = wt in ("remote", "hybrid") or (wt != "onsite" and (j.get("isRemote") or is_remote_text(loc, j.get("title"))))
        comp = j.get("compensation") or {}
        salary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
        desc = j.get("descriptionHtml") or ""
        dept = j.get("department") or j.get("team") or ""
        out.append(Job(
            ats="ashby", board=board, external_id=j["id"], title=j.get("title", ""),
            url=j.get("applyUrl") or j.get("jobUrl", ""), location=loc, remote=bool(remote),
            description_html=desc, posted_at=j.get("publishedAt"),
            job_type=j.get("employmentType", ""), salary=salary,
            department=dept, tags=[dept] if dept else [], version=ver(desc, salary),
            country=((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "",
        ))
    return None, out


# Workable ------------------------------------------------------------------ #
async def fetch_workable(client, board):
    data = await get_json(
        client, f"https://apply.workable.com/api/v1/widget/accounts/{board}", {"details": "true"}
    )
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        remote = bool(j.get("telecommuting")) or is_remote_text(loc, j.get("title"))
        desc = j.get("description") or ""
        dept = j.get("department") or ""
        out.append(Job(
            ats="workable", board=board, external_id=str(j.get("shortcode") or j.get("id")),
            title=j.get("title", ""),
            url=j.get("url") or j.get("shortlink") or j.get("application_url", ""),
            location=loc, remote=remote, description_html=desc,
            posted_at=j.get("published_on") or j.get("created_at"),
            job_type=j.get("employment_type", ""), department=dept,
            tags=[dept] if dept else [], version=ver(desc),
            country=j.get("country") or "",
        ))
    return data.get("name"), out


# SmartRecruiters ------------------------------------------------------------ #
def _sr_detail(client, board, pid):
    async def run(job: Job):
        d = await get_json(client, f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{pid}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        parts = []
        for k in ("jobDescription", "qualifications", "additionalInformation"):
            s = secs.get(k) or {}
            if s.get("text"):
                parts.append(f"<h3>{html.escape(s.get('title') or '')}</h3>{s['text']}")
        job.description_html = "".join(parts)
        job.url = d.get("postingUrl") or job.url
    return run


async def fetch_smartrecruiters(client, board):
    out, offset, cname = [], 0, None
    while True:
        data = await get_json(
            client, f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
            {"limit": 100, "offset": offset},
        )
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            loc_txt = ", ".join(x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x)
            dept = (j.get("department") or {}).get("label", "")
            job = Job(
                ats="smartrecruiters", board=board, external_id=j["id"], title=j.get("name", ""),
                url=f"https://jobs.smartrecruiters.com/{board}/{j['id']}", location=loc_txt,
                remote=bool(loc.get("remote")) or is_remote_text(loc_txt, j.get("name")),
                posted_at=j.get("releasedDate"),
                job_type=(j.get("typeOfEmployment") or {}).get("label", ""),
                department=dept, tags=[dept] if dept else [],
                country=loc.get("country") or "",
            )
            job.detail = _sr_detail(client, board, j["id"])
            cname = cname or (j.get("company") or {}).get("name")
            out.append(job)
        offset += 100
        if not items or offset >= data.get("totalFound", 0):
            break
    return cname, out


# Recruitee ----------------------------------------------------------------- #
async def fetch_recruitee(client, board):
    data = await get_json(client, f"https://{board}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []):
        if j.get("status") not in (None, "published"):
            continue
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        remote = bool(j.get("remote")) or is_remote_text(j.get("location"), loc, j.get("title"))
        dept = j.get("department") or ""
        desc, req = j.get("description") or "", j.get("requirements") or ""
        out.append(Job(
            ats="recruitee", board=board, external_id=str(j["id"]), title=j.get("title", ""),
            url=j.get("careers_url", ""), location=j.get("location") or loc, remote=remote,
            description_html=desc, requirements_html=req, posted_at=j.get("published_at"),
            job_type=j.get("employment_type_code", ""), department=dept,
            tags=[dept] if dept else [], version=ver(desc, req),
            country=j.get("country_code") or "",
        ))
    return data.get("company", {}).get("name") if isinstance(data.get("company"), dict) else None, out


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
}


def get_sources(boards: dict, only: Optional[str] = None) -> list[Source]:
    out = []
    for ats, tokens in boards.items():
        if ats not in ADAPTERS or (only and ats != only):
            continue
        out += [Source(ats, tok) for tok in tokens if not is_blocked_company(tok)]
    random.shuffle(out)  # spread load across ATS hosts
    return out


# --------------------------------------------------------------------------- #
# Supabase (PostgREST)
# --------------------------------------------------------------------------- #
class Supa:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client
        self.base = f"{SUPABASE_URL}/rest/v1"
        self.h = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

    async def select(self, table: str, params: dict) -> list[dict]:
        out, offset = [], 0
        while True:
            r = await self.c.get(
                f"{self.base}/{table}",
                headers={**self.h, "Range-Unit": "items", "Range": f"{offset}-{offset + 999}"},
                params=params,
            )
            if r.status_code >= 300:
                raise RuntimeError(f"select {table} failed {r.status_code}: {r.text[:300]}")
            rows = r.json()
            out += rows
            if len(rows) < 1000:
                return out
            offset += 1000

    async def insert(self, table, rows, on_conflict=None, ignore_dupes=False, ret=False):
        params, prefer = {}, ["return=representation" if ret else "return=minimal"]
        if on_conflict:
            params["on_conflict"] = on_conflict
            prefer.append("resolution=ignore-duplicates" if ignore_dupes else "resolution=merge-duplicates")
        r = await self.c.post(
            f"{self.base}/{table}", headers={**self.h, "Prefer": ",".join(prefer)}, params=params, json=rows
        )
        if r.status_code >= 300:
            raise RuntimeError(f"insert {table} failed {r.status_code}: {r.text[:300]}")
        return r.json() if ret else None

    async def patch(self, table, params, body):
        r = await self.c.patch(
            f"{self.base}/{table}", headers={**self.h, "Prefer": "return=minimal"}, params=params, json=body
        )
        if r.status_code >= 300:
            raise RuntimeError(f"patch {table} failed {r.status_code}: {r.text[:300]}")


# --------------------------------------------------------------------------- #
# Sync engine
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, http: httpx.AsyncClient, dry: bool):
        self.http, self.db, self.dry = http, Supa(http), dry
        self.companies: dict[str, str] = {}
        self.categories: dict[str, str] = {}
        self.hashes: dict[str, str] = {}  # job slug -> last seen content version
        self.known: dict[str, dict] = {}  # company_id -> {slug: row} (loaded once, kept in memory)
        self.rejected: set = set()        # job slugs skipped as agency / non-US posts
        self.fails: Counter = Counter()
        self.retry_at: dict[str, float] = {}
        self.cat_lock = asyncio.Lock()
        self.detail_sem = asyncio.Semaphore(8)

    async def preload(self):
        for r in await self.db.select("companies", {"select": "id,slug"}):
            self.companies[r["slug"]] = r["id"]
        for r in await self.db.select("categories", {"select": "id,slug"}):
            self.categories[r["slug"]] = r["id"]
        log.info("Loaded %d companies, %d categories", len(self.companies), len(self.categories))

    async def ensure_company(self, name: str, slug: str, src: Source) -> str:
        if slug in self.companies:
            return self.companies[slug]
        if self.dry:
            log.info("[dry] would create company %s", name)
            return f"dry-{slug}"
        domain = re.sub(r"^https?://", "", src.website).split("/")[0] if src.website else ""
        row = {
            "name": name, "slug": slug, "website": src.website or None,
            "logo_url": f"https://www.google.com/s2/favicons?domain={domain}&sz=128" if domain else None,
        }
        res = await self.db.insert("companies", [row], on_conflict="slug", ignore_dupes=True, ret=True)
        if res:
            cid = res[0]["id"]
        else:
            cid = (await self.db.select("companies", {"select": "id", "slug": f"eq.{slug}"}))[0]["id"]
        self.companies[slug] = cid
        log.info("Created company %s", name)
        return cid

    async def category_id(self, job: Job) -> Optional[str]:
        slug, name = classify(job.title, job.department)
        if slug in self.categories:
            return self.categories[slug]
        async with self.cat_lock:
            if slug in self.categories:
                return self.categories[slug]
            if not self.dry:
                try:
                    res = await self.db.insert(
                        "categories", [{"name": name, "slug": slug}],
                        on_conflict="slug", ignore_dupes=True, ret=True,
                    )
                    if not res:
                        res = await self.db.select("categories", {"select": "id", "slug": f"eq.{slug}"})
                    self.categories[slug] = res[0]["id"]
                    log.info("Created category %s", name)
                    return self.categories[slug]
                except Exception as e:
                    log.warning("Could not create category %s (%s); using General", name, e)
            return self.categories.get("general")

    async def build_row(self, slug: str, j: Job, company_id: str) -> dict:
        countries, states, worldwide = parse_eligibility(j.location)
        if US_ONLY:
            # a bare "Remote" job that passed the US filter is a US job
            worldwide = False
            countries = countries or []
            if "United States" not in countries:
                countries.insert(0, "United States")
        return {
            "slug": slug,
            "title": j.title.strip()[:250],
            "company_id": company_id,
            "category_id": await self.category_id(j),
            "job_type": map_job_type(j.job_type, j.title),
            "salary_range": j.salary,
            "tags": [t for t in j.tags if t][:3],
            "description_html": clean_html(j.description_html),
            "requirements_html": clean_html(j.requirements_html),
            "benefits_html": None,
            "application_url": j.url,
            "worldwide_only": worldwide,
            "status": "published",
            "posted_at": to_iso(j.posted_at) or now_iso(),
            "eligible_countries": countries or None,
            "eligible_states": states,
        }

    async def _detail(self, j: Job):
        if not j.detail:
            return
        async with self.detail_sem:
            try:
                await j.detail(j)
            except Exception as e:
                log.warning("detail fetch failed for %s/%s: %s", j.board, j.external_id, e)

    async def sync_source(self, src: Source) -> Counter:
        stats: Counter = Counter()
        hint, jobs = await ADAPTERS[src.ats](self.http, src.board)
        fetched = len(jobs)  # what the board really returned, before any filtering
        name = src.name or hint or re.sub(r"[-_.]+", " ", src.board).title()
        if is_blocked_company(name, src.board):
            return stats
        cslug = slugify(name)
        if REMOTE_ONLY:
            jobs = [j for j in jobs if j.remote]
        if US_ONLY:
            # location clearly outside the US -> drop now (before any detail fetch).
            # "Remote" / empty / unrecognised locations stay and get the description check.
            jobs = [j for j in jobs if job_verdict(j) != "non_us"]
        live = {job_slug(j, cslug): j for j in jobs if j.title and j.url}
        live_hash = {s: ver(j.title, j.url, j.version) for s, j in live.items()}
        stats["live"] = len(live)

        if cslug not in self.companies and not any(is_recent(j) for j in live.values()):
            return stats
        company_id = await self.ensure_company(name, cslug, src)

        if company_id not in self.known:
            rows = []
            if not company_id.startswith("dry-"):
                rows = await self.db.select(
                    "jobs", {"select": "slug,status,title,application_url,owner_id", "company_id": f"eq.{company_id}"}
                )
            self.known[company_id] = {r["slug"]: r for r in rows}
        existing = self.known[company_id]

        # ---- new jobs
        existing_titles = {(r["title"] or "").strip().lower() for r in existing.values()}
        seen_new, new = set(), []
        for s, j in live.items():
            t = j.title.strip()[:250].lower()
            if s in existing or s in self.rejected or not is_recent(j):
                continue
            if t in existing_titles or t in seen_new:
                continue
            seen_new.add(t)
            new.append((s, j))
        await asyncio.gather(*(self._detail(j) for _, j in new))
        good = []
        for s, j in new:
            if US_ONLY and not passes_us_description(j):
                self.rejected.add(s)
                stats["skipped_non_us"] += 1
                continue
            if EXCLUDE_AGENCIES and looks_like_agency(j):
                self.rejected.add(s)
                stats["skipped_agency"] += 1
                continue
            good.append((s, j))
        new_rows = [await self.build_row(s, j, company_id) for s, j in good]
        if new_rows:
            if self.dry:
                for r in new_rows:
                    log.info("[dry] NEW  %s | %s", name, r["title"])
            else:
                for i in range(0, len(new_rows), 50):
                    await self.db.insert("jobs", new_rows[i:i + 50], on_conflict="slug", ignore_dupes=True)
                for r in new_rows:
                    existing[r["slug"]] = {"slug": r["slug"], "status": "published", "title": r["title"],
                                           "application_url": r["application_url"], "owner_id": None}
            stats["new"] = len(new_rows)

        # ---- edited / reopened jobs
        for s, j in live.items():
            r = existing.get(s)
            if not r:
                continue
            prev = self.hashes.get(s)
            changed = (prev is not None and prev != live_hash[s]) or r["title"] != j.title.strip()[:250] \
                or (j.ats != "smartrecruiters" and r["application_url"] != j.url)
            reopen = r["status"] == "closed" and is_managed(r) and is_recent(j)
            if not (changed or reopen):
                continue
            await self._detail(j)
            if US_ONLY and not passes_us_description(j):
                continue
            patch = {"title": j.title.strip()[:250], "application_url": j.url}
            if j.salary:
                patch["salary_range"] = j.salary
            if clean_html(j.description_html):
                patch["description_html"] = clean_html(j.description_html)
            if clean_html(j.requirements_html):
                patch["requirements_html"] = clean_html(j.requirements_html)
            if reopen:
                patch["status"] = "published"
            if self.dry:
                log.info("[dry] UPDATE %s | %s", name, j.title)
            else:
                await self.db.patch("jobs", {"slug": f"eq.{s}"}, patch)
                r.update(title=patch["title"], application_url=j.url, status=patch.get("status", r["status"]))
            stats["updated"] += 1

        # ---- removed jobs -> closed (never when the board returned nothing: could be an API glitch).
        # Jobs filtered out as non-US are not in `live`, so previously imported non-US jobs get
        # closed here too.
        gone = [s for s, r in existing.items() if s not in live and r["status"] == "published" and is_managed(r)]
        if gone and fetched:
            if self.dry:
                log.info("[dry] would close %d jobs at %s", len(gone), name)
            else:
                for i in range(0, len(gone), 50):
                    chunk = gone[i:i + 50]
                    await self.db.patch("jobs", {"slug": f"in.({','.join(chunk)})"}, {"status": "closed"})
                for s in gone:
                    existing[s]["status"] = "closed"
            stats["closed"] = len(gone)
        elif gone:
            log.warning("%s returned 0 jobs but %d are published; skipping close", name, len(gone))

        self.hashes.update(live_hash)
        return stats

    async def run_cycle(self, sources: list[Source]):
        sem = asyncio.Semaphore(CONCURRENCY)
        totals: Counter = Counter()
        done = 0

        async def one(src: Source):
            nonlocal done
            if self.retry_at.get(src.key, 0) > time.time():
                return
            async with sem:
                try:
                    st = await self.sync_source(src)
                    self.fails[src.key] = 0
                    if st.pop("live", 0) == 0:
                        self.retry_at[src.key] = time.time() + IDLE_RECHECK_SECONDS
                    totals.update(st)
                    if st:
                        log.info("%-34s %s", src.key, dict(st))
                except Exception as e:
                    self.fails[src.key] += 1
                    not_found = isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404
                    delay = 86400 if not_found else min(3600, 60 * 2 ** self.fails[src.key])
                    self.retry_at[src.key] = time.time() + delay
                    if not not_found:
                        log.warning("%s failed (%s); retry in %ds", src.key, e, delay)
                finally:
                    done += 1
                    if done % 1000 == 0:
                        log.info("progress %d/%d boards", done, len(sources))

        await asyncio.gather(*(one(s) for s in sources))
        log.info("Cycle done: %d boards | %s", len(sources), dict(totals) or "no changes")


# --------------------------------------------------------------------------- #
# Board discovery: Common Crawl's free public URL index (no API key, no cost).
# It lists every career-page URL the crawler has seen on each ATS domain; the first
# path segment is the company's board slug.
# --------------------------------------------------------------------------- #
CC_TARGETS = {
    "greenhouse": ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*"],
    "lever": ["jobs.lever.co/*"],
    "ashby": ["jobs.ashbyhq.com/*"],
    "workable": ["apply.workable.com/*"],
    "smartrecruiters": ["jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*"],
    "recruitee": ["*.recruitee.com"],
}
IGNORED_TOKENS = {
    "embed", "j", "api", "www", "app", "jobs", "careers", "oneclick-ui", "company", "static",
    "assets", "favicon.ico", "robots.txt", "sitemap.xml", "login", "sign_in", "support", "help",
}
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def board_token(ats: str, url: str) -> Optional[str]:
    u = urlparse(url)
    host = u.netloc.lower().split(":")[0]
    if ats == "recruitee":
        tok = host[: -len(".recruitee.com")] if host.endswith(".recruitee.com") else None
    else:
        parts = [p for p in u.path.split("/") if p]
        if ats == "greenhouse" and parts[:1] == ["embed"]:
            q = parse_qs(u.query).get("for")
            tok = q[0] if q else None
        else:
            tok = parts[0] if parts else None
    if not tok:
        return None
    tok = unquote(tok).strip()
    if not TOKEN_RE.match(tok) or tok.lower() in IGNORED_TOKENS:
        return None
    return tok


async def cc_get(client: httpx.AsyncClient, url: str, params: dict, sem: asyncio.Semaphore) -> str:
    for i in range(6):
        async with sem:
            try:
                r = await client.get(url, params=params, timeout=120)
                if r.status_code == 200:
                    return r.text
                if r.status_code == 404:
                    return ""
            except httpx.TransportError:
                pass
        await asyncio.sleep(2 ** i + random.random())  # Common Crawl asks clients to back off on 503
    raise RuntimeError(f"Common Crawl request kept failing: {url}")


async def cc_scan(client, api: str, pattern: str, ats: str, found: dict, sem) -> None:
    base = {"url": pattern, "output": "json", "fl": "url,status"}
    first = (await cc_get(client, api, {**base, "showNumPages": "true"}, sem)).strip().splitlines()
    pages = min(int(json.loads(first[0]).get("pages", 1)), CC_MAX_PAGES) if first else 0

    async def page(n: int):
        text = await cc_get(client, api, {**base, "page": n}, sem)
        for line in text.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if str(d.get("status")) != "200":
                continue
            tok = board_token(ats, d.get("url", ""))
            if tok:
                found.setdefault(tok.lower(), tok)

    await asyncio.gather(*(page(n) for n in range(pages)))


async def discover_boards(http: httpx.AsyncClient) -> dict:
    info = (await http.get("https://index.commoncrawl.org/collinfo.json", timeout=60)).json()
    apis = [c["cdx-api"] for c in info[1:CC_COLLECTIONS + 1]]   # skip the newest, often unstable
    found: dict = {ats: {} for ats in CC_TARGETS}
    sem = asyncio.Semaphore(1)
    for api in apis:
        for ats, patterns in CC_TARGETS.items():
            for pat in patterns:
                try:
                    await cc_scan(http, api, pat, ats, found[ats], sem)
                except Exception as e:
                    log.warning("discovery %s %s failed: %s", api.rsplit("/", 1)[-1], pat, e)
            log.info("%s: %d boards so far", ats, len(found[ats]))
    return {ats: sorted(v.values(), key=str.lower) for ats, v in found.items()}

ATS_HOSTS = {
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
    "workable": ("apply.workable.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"),
}


async def boards_from_db(http: httpx.AsyncClient) -> dict:
    """Every ATS board already referenced by an application_url in your jobs table."""
    db = Supa(http)
    found = {ats: {} for ats in ADAPTERS}
    for ats, hosts in ATS_HOSTS.items():
        for host in hosts:
            rows = await db.select(
                "jobs", {"select": "application_url", "application_url": f"ilike.*{host}*"}
            )
            for r in rows:
                tok = board_token(ats, r.get("application_url") or "")
                if tok:
                    found[ats].setdefault(tok.lower(), tok)
    return {a: sorted(v.values(), key=str.lower) for a, v in found.items()}

def load_boards() -> Optional[dict]:
    try:
        with open(BOARDS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


SKIP_CC = os.environ.get("SKIP_CC", "false").lower() == "true"


async def get_boards(http: httpx.AsyncClient, force: bool = False) -> tuple[dict, float]:
    cached = load_boards()
    if cached and not force and time.time() - cached["discovered_at"] < REFRESH_DAYS * 86400:
        return cached["boards"], cached["discovered_at"]

    boards = {ats: [] for ats in ADAPTERS}
    if SKIP_CC:
        log.info("SKIP_CC set: not using Common Crawl")
    else:
        log.info("Discovering company boards via Common Crawl...")
        try:
            boards = await discover_boards(http)
        except Exception as e:
            log.warning("Common Crawl discovery failed: %s", e)

    try:
        with open("seed_boards.json", encoding="utf-8") as f:
            seed = json.load(f)
    except Exception:
        seed = {}
    try:
        db_boards = await boards_from_db(http) if SUPABASE_URL and SUPABASE_KEY else {}
    except Exception as e:
        log.warning("DB snowball failed: %s", e)
        db_boards = {}
    old = cached["boards"] if cached else {}

    for ats in ADAPTERS:
        merged = {
            t.lower(): t
            for t in boards.get(ats, []) + seed.get(ats, []) + db_boards.get(ats, []) + old.get(ats, [])
        }
        boards[ats] = sorted(merged.values(), key=str.lower)

    if not any(boards.values()):
        raise SystemExit("No boards found from any source")
    with open(BOARDS_CACHE, "w", encoding="utf-8") as f:
        json.dump({"discovered_at": time.time(), "boards": boards}, f)
    log.info("Boards: %s (saved to %s)", {k: len(v) for k, v in boards.items()}, BOARDS_CACHE)
    return boards, time.time()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
async def monitor(args):
    if not SUPABASE_URL or not SUPABASE_KEY:
        if not args.discover_only:
            raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
    limits = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, limits=limits, headers={"User-Agent": USER_AGENT}
    ) as http:
        boards, discovered_at = await get_boards(http, args.refresh_boards)
        if args.discover_only:
            return
        eng = Engine(http, args.dry_run)
        await eng.preload()
        sources = get_sources(boards, args.only)
        while True:
            started = time.time()
            log.info("Polling %d boards%s%s", len(sources), " (dry run)" if args.dry_run else "",
                     " (US only)" if US_ONLY else "")
            await eng.run_cycle(sources)
            if args.once:
                return
            if time.time() - discovered_at > REFRESH_DAYS * 86400:
                boards, discovered_at = await get_boards(http, True)
                sources = get_sources(boards, args.only)
            await asyncio.sleep(max(5, args.interval - (time.time() - started)) + random.uniform(0, 5))


def main():
    p = argparse.ArgumentParser(description="ATS -> Supabase job monitor")
    p.add_argument("--interval", type=int, default=120, help="seconds between cycles")
    p.add_argument("--once", action="store_true", help="single pass then exit")
    p.add_argument("--dry-run", action="store_true", help="log what would change, write nothing")
    p.add_argument("--only", choices=list(ADAPTERS), help="only poll one ATS")
    p.add_argument("--discover-only", action="store_true", help="build the board list and exit")
    p.add_argument("--refresh-boards", action="store_true", help="force re-discovery of boards")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(monitor(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()