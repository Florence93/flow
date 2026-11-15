import re, httpx
from collections import Counter

REMOTE_RE = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed|worldwide|telecommut\w*)\b", re.I)
HYBRID_RE = re.compile(r"\b(hybrid|on-?site|in[- ]office)\b", re.I)

def is_remote_text(*texts):
    t = " ".join(x for x in texts if x)
    return bool(REMOTE_RE.search(t)) and not HYBRID_RE.search(t)

d = httpx.get("https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs", timeout=30).json()
passed = [j for j in d["jobs"] if is_remote_text(j["location"]["name"], j["title"])]
print(len(passed), "of", len(d["jobs"]), "pass the remote filter\n")
print(Counter(j["location"]["name"] for j in passed).most_common(), "\n")
for j in passed[:25]:
    print(repr(j["location"]["name"]), "|", j["title"])