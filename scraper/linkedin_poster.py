# linkedin_poster.py
#
# Posts text to a LinkedIn Company Page via the Community Management API.
#
# One-time setup:   python linkedin_poster.py auth
# Quick test post:  python linkedin_poster.py test
#
# Secrets come from environment variables (or a .env file next to this script):
#   LINKEDIN_CLIENT_ID
#   LINKEDIN_CLIENT_SECRET
#   LINKEDIN_ORG_ID          (number from linkedin.com/company/<ID>/admin)
#   LINKEDIN_REDIRECT_URI    (default http://localhost:8080/callback; must be
#                             listed under Authorized redirect URLs in your app)
#   LINKEDIN_API_VERSION     (optional, YYYYMM, default 202609)

import json
import os
import re
import sys
import time
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.parse import quote  # add to the imports at the top

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine, plain environment variables still work

TOKEN_FILE = "linkedin_token.json"
SCOPES = "w_organization_social w_organization_social_feed r_organization_social r_organization_social_feed rw_organization_admin"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
POSTS_URL = "https://api.linkedin.com/rest/posts"


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if not val:
        raise RuntimeError(f"Missing environment variable {name}")
    return val


# ---------------------------------------------------------------------------
# Token storage + refresh
# ---------------------------------------------------------------------------

def _save_token(data: dict) -> dict:
    now = int(time.time())
    existing = _load_token() or {}
    token = {
        "access_token": data["access_token"],
        "access_expires_at": now + int(data.get("expires_in", 0)),
        # LinkedIn may or may not return a new refresh token; keep the old one if not.
        "refresh_token": data.get("refresh_token") or existing.get("refresh_token"),
        "refresh_expires_at": (
            now + int(data["refresh_token_expires_in"])
            if data.get("refresh_token_expires_in")
            else existing.get("refresh_expires_at")
        ),
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(token, f, indent=2)
    return token


def _load_token() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def _refresh(token: dict) -> dict:
    if not token.get("refresh_token"):
        raise RuntimeError("No refresh token stored. Run: python linkedin_poster.py auth")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": _env("LINKEDIN_CLIENT_ID"),
            "client_secret": _env("LINKEDIN_CLIENT_SECRET"),
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LinkedIn token refresh failed ({resp.status_code}): {resp.text}. "
            "Run: python linkedin_poster.py auth"
        )
    return _save_token(resp.json())


def get_access_token() -> str:
    token = _load_token()
    if not token:
        raise RuntimeError("Not authorized yet. Run: python linkedin_poster.py auth")
    # Refresh if it expires within the next 5 days.
    if token["access_expires_at"] - time.time() < 5 * 86400:
        token = _refresh(token)
    return token["access_token"]


# ---------------------------------------------------------------------------
# One-time authorization (copy/paste flow, no local server needed)
# ---------------------------------------------------------------------------

def authorize() -> None:
    redirect = os.environ.get("LINKEDIN_REDIRECT_URI", "http://localhost:8080/callback")
    params = {
        "response_type": "code",
        "client_id": _env("LINKEDIN_CLIENT_ID"),
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": "postjob",
    }
    print("\n1) Open this URL while logged in as an ADMIN of your company page:\n")
    print(f"{AUTH_URL}?{urlencode(params)}\n")
    print("2) Click Allow. Your browser will land on a page that fails to load. That's expected.")
    print("3) Copy the FULL URL from the address bar and paste it here.\n")
    pasted = input("Pasted URL: ").strip()

    query = parse_qs(urlparse(pasted).query)
    if "code" not in query:
        raise RuntimeError(f"No 'code' found in that URL. LinkedIn said: {query.get('error_description')}")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": redirect,
            "client_id": _env("LINKEDIN_CLIENT_ID"),
            "client_secret": _env("LINKEDIN_CLIENT_SECRET"),
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    _save_token(resp.json())
    print(f"\nSaved tokens to {TOKEN_FILE}. Keep that file out of git.")


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

# Characters LinkedIn's "little text" format treats as markup. Unescaped, they
# can silently truncate a post. '#' is deliberately NOT escaped so hashtags work.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>*_~])")


def _escape(text: str) -> str:
    return _RESERVED.sub(r"\\\1", text)




def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Linkedin-Version": os.environ.get("LINKEDIN_API_VERSION", "202609"),
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def comment_on_post(post_urn: str, text: str) -> None:
    """Add a comment to a post, as the company page."""
    resp = requests.post(
        f"https://api.linkedin.com/rest/socialActions/{quote(post_urn, safe='')}/comments",
        headers=_headers(),
        json={
            "actor": f"urn:li:organization:{_env('LINKEDIN_ORG_ID')}",
            "message": {"text": text},
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn comment failed ({resp.status_code}): {resp.text}")


def post_to_linkedin(text: str, comment: str | None = None) -> str:
    """Publish a text post to the company page, then add `comment` as the first
    comment. Returns the new post URN."""
    resp = requests.post(
        POSTS_URL,
        headers=_headers(),
        json={
            "author": f"urn:li:organization:{_env('LINKEDIN_ORG_ID')}",
            "commentary": _escape(text),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn post failed ({resp.status_code}): {resp.text}")

    post_urn = resp.headers.get("x-restli-id", "")

    # A failed comment must NOT raise: the post is already live, and raising
    # would make the caller queue it again and create a duplicate.
    if comment and post_urn:
        try:
            time.sleep(2)  # give LinkedIn a moment to register the post
            comment_on_post(post_urn, comment)
        except Exception as err:  # noqa: BLE001
            print(f"   ⚠️ Post is live but the link comment failed: {err}")
            print(f"      Add this manually under {post_urn}: {comment}")

    return post_urn

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "auth":
        authorize()
    elif cmd == "test":
        print("Posted:", post_to_linkedin("Test post from my job automation (will delete). #RemoteJobs"))
    else:
        print("Usage: python linkedin_poster.py [auth|test]")