import os
import re
import json
import time
import html as html_lib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from linkedin_poster import post_to_linkedin

# ============================================================================
# CONFIG (secrets come from environment variables, not the source file)
# ============================================================================
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENROUTER_API_KEYS = [k.strip() for k in os.environ.get("OPENROUTER_API_KEYS", "").split(",") if k.strip()]
OPENROUTER_MODEL = "openrouter/free"

SITE_BASE_URL = "https://www.globalremotehq.com/jobs"

# --- UTM tracking (so Google Analytics can attribute LinkedIn post traffic) ---
UTM_SOURCE = "linkedin"
UTM_MEDIUM = "social"
UTM_CAMPAIGN = "job_posts"

# --- Schedule / limits ---
# The workflow cron decides WHEN this runs (12:00, 12:30, 13:00 UTC = 1:00pm,
# 1:30pm, 2:00pm Lagos). The script has no time-of-day checks, so a late GitHub
# start never causes a skipped batch.
SCHEDULE_TZ = ZoneInfo("Africa/Lagos")  # defines the "day" for the daily cap (midnight Lagos)
DAILY_LIMIT = 50            # 5 runs x 10 jobs
PER_RUN_LIMIT = 10          # max posts per run
MAX_FAILURES_PER_RUN = 5    # stop the run if this many jobs fail to post
SLEEP_BETWEEN_POSTS_SECONDS = 5
MAX_HASHTAGS = 12           # hashtags per LinkedIn post
MAX_POST_CHARS = 2900       # LinkedIn's limit is 3000 characters

JOB_TYPE_LABELS = {"fullTime": "Full-Time", "partTime": "Part-Time",
                   "contract": "Contract", "freelance": "Freelance"}

# Generic hashtags added after the job-specific ones (company + role tags).
BASE_HASHTAGS = ["#RemoteJobs", "#USARemoteJobs", "#CanadaRemoteJobs", "#UKRemoteJobs",
                 "#WorkFromHome", "#Hiring", "#NowHiring", "#RemoteWork"]


def build_tracked_url(slug: str) -> str:
    """Job URL with UTM parameters; utm_content = job slug shows which job drove the click."""
    params = {
        "utm_source": UTM_SOURCE,
        "utm_medium": UTM_MEDIUM,
        "utm_campaign": UTM_CAMPAIGN,
        "utm_content": slug,
    }
    return f"{SITE_BASE_URL}/{slug}?{urlencode(params)}"


# ============================================================================
# Supabase
# ============================================================================
def supabase_request(path: str, method: str = "get", payload=None):
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Prefer": "return=representation",
    }
    resp = requests.request(method.upper(), f"{SUPABASE_URL}/rest/v1/{path}",
                            headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Supabase error ({resp.status_code}) on {path}: {resp.text}")
    return resp.json() if resp.text else None


def fetch_pending_jobs(limit: int) -> list[dict]:
    # Newest first: each run takes the latest unposted jobs.
    return supabase_request(
        f"jobs_pending_linkedin?select=*&order=created_at.desc&limit={limit}", "get"
    ) or []


def mark_posted(job_id: str) -> None:
    supabase_request("linkedin_posts", "post", {"job_id": job_id})


def posts_today() -> int:
    now_local = datetime.now(SCHEDULE_TZ)
    start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    ts = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = supabase_request(f"linkedin_posts?select=id&posted_at=gte.{ts}", "get") or []
    return len(rows)


# ============================================================================
# HTML -> text / list helpers
# ============================================================================
def html_to_text(html: str | None) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html or "", flags=re.I)
    text = re.sub(r"</(p|li|div|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def li_items(html: str | None) -> list[str]:
    items = re.findall(r"<li[^>]*>([\s\S]*?)</li>", html or "", flags=re.I)
    out = [re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", i))).strip() for i in items]
    return [i for i in out if i]


def _clean_bullet(text: str, max_len: int = 110) -> str:
    text = re.sub(r"^[-•*✅✔⭐\s]+", "", str(text).strip())
    text = re.sub(r"\s+", " ", text).rstrip(".")
    if len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _clean_list(items, limit: int) -> list[str]:
    out = []
    for it in items or []:
        c = _clean_bullet(it)
        if c:
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _hashtag(raw: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", raw or "")
    return "#" + "".join(w[:1].upper() + w[1:] for w in words) if words else ""


def _first_sentence(text: str, max_len: int = 220) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:max_len].strip()


# ============================================================================
# OpenRouter (key rotation)
# ============================================================================
class AllKeysExhausted(Exception):
    pass


_key_index = 0
_dead_until: dict[int, float] = {}


def _next_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def call_openrouter(payload: dict) -> dict:
    global _key_index
    n = len(OPENROUTER_API_KEYS)
    if n == 0:
        raise AllKeysExhausted("No OpenRouter keys configured.")
    for _ in range(n):
        idx = _key_index % n
        if _dead_until.get(idx, 0) > time.time():
            _key_index += 1
            continue
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEYS[idx]}"},
            json=payload, timeout=60,
        )
        if resp.status_code == 429:
            t = resp.text.lower()
            daily = "per-day" in t or "per day" in t or "daily" in t
            _dead_until[idx] = _next_utc_midnight() if daily else time.time() + 60
            _key_index += 1
            continue
        if resp.status_code in (401, 402, 403):
            _dead_until[idx] = _next_utc_midnight()
            _key_index += 1
            continue
        try:
            body = resp.json()
        except ValueError:
            body = {"error": {"message": resp.text[:300]}}
        if not isinstance(body, dict):
            body = {"error": {"message": str(body)[:300]}}
        body["_status"] = resp.status_code
        return body
    raise AllKeysExhausted("All OpenRouter keys are exhausted for now.")


LINKEDIN_SYSTEM_PROMPT = """You write short LinkedIn bullets for a job post.
Return ONLY one valid JSON object, no prose, no markdown fences:
{
  "summary": string (ONE sentence, max 220 chars, format: "<Company> is hiring a <Title> to <what the role does>."),
  "doItems": array of 4-6 short bullets (max 90 chars each, start with a verb, no trailing period),
  "lookingFor": array of 4-5 short bullets (max 90 chars each, no trailing period),
  "whyJoin": array of 2-3 short bullets (max 70 chars each) based ONLY on benefits/mission stated in the text, or [] if none,
  "hashtags": array of 4-5 role/industry hashtags in CamelCase, e.g. "#FullStackDeveloper", "#SoftwareEngineer", "#Python", "#WebDevelopment"
}
All text must be plain text with no emojis, no markdown, and no invented facts."""


def generate_linkedin_bullets(job: dict) -> dict:
    """Returns the model's bullets, or {} on any failure (we fall back to DB content)."""
    user_prompt = (
        f"Company: {job.get('company_name') or ''}\nTitle: {job['title']}\n\n"
        f"DESCRIPTION:\n{html_to_text(job.get('description_html'))[:6000]}\n\n"
        f"REQUIREMENTS:\n{html_to_text(job.get('requirements_html'))[:2000]}\n\n"
        f"BENEFITS:\n{html_to_text(job.get('benefits_html'))[:2000]}"
    )
    try:
        body = call_openrouter({
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "system", "content": LINKEDIN_SYSTEM_PROMPT},
                         {"role": "user", "content": user_prompt}],
            "temperature": 0.2,
        })
        if body.get("_status", 200) >= 400:
            return {}
        raw = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group(0)) if m else {}
        return data if isinstance(data, dict) else {}
    except AllKeysExhausted:
        print("   ⚠️ OpenRouter keys exhausted, using fallback bullets from the DB.")
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ Bullet generation failed ({e}), using fallback.")
        return {}


# ============================================================================
# Post builder
# ============================================================================
def build_hashtags(company: str, li: dict) -> list[str]:
    """Job-specific tags (company + role) first, then generic ones, de-duplicated and capped."""
    role_tags = [_hashtag(t) for t in (li.get("hashtags") or [])[:5]]
    tags = [_hashtag(company)] + role_tags + BASE_HASHTAGS

    seen, hashtags = set(), []
    for t in tags:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            hashtags.append(t)
    return hashtags[:MAX_HASHTAGS]


def build_linkedin_post(job: dict, li: dict) -> str:
    title = job["title"]
    company = job.get("company_name") or ""

    summary = (_clean_bullet(li.get("summary") or "", 240)
               or _clean_bullet(job.get("summary") or "", 240)
               or _first_sentence(html_to_text(job.get("description_html"))))
    do_items = _clean_list(li.get("doItems"), 6)
    looking = _clean_list(li.get("lookingFor"), 5) or _clean_list(li_items(job.get("requirements_html")), 5)
    why = _clean_list(li.get("whyJoin"), 3) or _clean_list(li_items(job.get("benefits_html")), 3)

    location = "Remote (United States)" if job.get("worldwide_only") else "Remote(United States)"
    job_type_label = JOB_TYPE_LABELS.get(job.get("job_type"), "Full-Time")
    url = build_tracked_url(job["slug"])

    hashtag_line = " ".join(build_hashtags(company, li))

    header = f"📝 {title}\n📍 {location}\n💼 {job_type_label}"
    if summary:
        header += f"\n{summary}"

    apply_block = f"📩 Apply here:\n{url}"

    # Optional sections, listed from most to least important. If the post is too
    # long, the least important ones are dropped so the apply link and hashtags
    # are never cut off.
    optional = []
    if do_items:
        optional.append("What you'll do\n" + "\n".join(f"✅ {i}" for i in do_items))
    if looking:
        optional.append("What they're looking for\n" + "\n".join(f"✔ {i}" for i in looking))
    if why:
        optional.append("Why join?\n" + "\n".join(f"⭐ {i}" for i in why))

    def assemble(sections: list[str]) -> str:
        return "\n\n".join([header, *sections, apply_block, hashtag_line])

    while optional and len(assemble(optional)) > MAX_POST_CHARS:
        optional.pop()  # drop the least important remaining section

    return assemble(optional)[:MAX_POST_CHARS]


# ============================================================================
# Error classification
# ============================================================================
def is_fatal_linkedin_error(err: Exception) -> bool:
    """Auth, permission, rate-limit and token problems affect every job, so stop the run."""
    msg = str(err)
    m = re.search(r"\((\d{3})\)", msg)
    if m and int(m.group(1)) in (401, 403, 429):
        return True
    return any(s in msg for s in ("Not authorized", "token refresh failed", "No refresh token"))


# ============================================================================
# Main
# ============================================================================
def main() -> None:
    force = os.environ.get("FORCE_RUN") == "1"   # for manual testing only

    done_today = posts_today()
    remaining = DAILY_LIMIT - done_today
    if not force and remaining <= 0:
        print(f"Daily limit reached ({done_today}/{DAILY_LIMIT}), skipping.")
        return

    batch_size = PER_RUN_LIMIT if force else min(PER_RUN_LIMIT, remaining)
    # Fetch extra candidates so a skipped job can be replaced and the run still reaches batch_size.
    jobs = fetch_pending_jobs(batch_size * 3)
    print(f"Posted today: {done_today}/{DAILY_LIMIT}. Found {len(jobs)} candidate job(s), target {batch_size}.")

    posted = 0
    failed = 0
    for job in jobs:
        if posted >= batch_size:
            break

        print(f"\n===== {job['title']} @ {job.get('company_name')} ({job['slug']}) =====")
        li = generate_linkedin_bullets(job)
        post_text = build_linkedin_post(job, li)

        try:
            post_to_linkedin(post_text)
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"   ⚠️ LinkedIn post failed: {err}")
            if is_fatal_linkedin_error(err):
                print("   Auth, permission or rate-limit problem, stopping this run.")
                print("   Check linkedin_token.json or re-run: python linkedin_poster.py auth")
                break
            if failed >= MAX_FAILURES_PER_RUN:
                print(f"   Too many failures ({failed}), stopping this run.")
                break
            continue  # skip this job (it stays pending) and try the next one

        try:
            mark_posted(job["id"])
        except Exception as err:  # noqa: BLE001
            print(f"   🚨 Posted, but failed to record in linkedin_posts: {err}")
            print(f"   Run: insert into linkedin_posts (job_id) values ('{job['id']}');")
            break  # keep this stop: continuing could create duplicate posts

        posted += 1
        print("   📣 Posted to LinkedIn")
        time.sleep(SLEEP_BETWEEN_POSTS_SECONDS)

    print(f"\n✅ Done. Posted {posted}, skipped {failed} this run, {done_today + posted}/{DAILY_LIMIT} today.")


if __name__ == "__main__":
    main()