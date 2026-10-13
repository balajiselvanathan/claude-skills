#!/usr/bin/env python3
"""Fetch each patch's review `state` directly from the patchwork REST API,
replacing the old mail-thread-scanning heuristic (get_review_replies.py).

Patchwork's possible patch states (project-independent, fixed enum):
  new, under-review, changes-requested, rejected, rfc, not-applicable,
  superseded, awaiting-upstream, deferred, needs-ack, accepted, queued.

This script maps that directly to the skill's status vocabulary. It does
NOT attempt to resolve "Accepted" into a specific branch (master vs next
vs qcom-next) — patchwork's `commit_ref` is a free-text field a maintainer
may or may not have filled in, with no structured branch concept, so
real branch-landing detection stays on the lore mirror + git patch-id
match (match_patch_ids.py). Here "accepted" is reported as "Accepted,
awaiting pick-up" and build_report.py's patch-id branch match takes
precedence over it when a patch-id match exists.
"""
import argparse
import json
import time
import urllib.error
import urllib.request

PATCHWORK_API = "https://patchwork.ozlabs.org/api/1.2"
USER_AGENT = "Mozilla/5.0 (upstream-status-skill)"
MAX_RETRIES = 10

STATE_TO_STATUS = {
    "accepted": "Accepted, awaiting pick-up",
    "changes-requested": "Reviewed - changes requested",
    "rejected": "Rejected",
    "rfc": "Not yet reviewed",
    "new": "Not yet reviewed",
    "under-review": "Not yet reviewed",
    "needs-ack": "Not yet reviewed",
    "deferred": "Not yet reviewed",
    "awaiting-upstream": "Not yet reviewed",
    "not-applicable": "Rejected",
    "superseded": None,  # excluded — an older version was replaced
    "queued": "Accepted, awaiting pick-up",
}


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == MAX_RETRIES:
                raise
            time.sleep(min(2 ** attempt, 20))


def fetch_patch_state(patch_id, cache):
    if patch_id in cache:
        return cache[patch_id]
    data = http_get_json(f"{PATCHWORK_API}/patches/{patch_id}/")
    cache[patch_id] = data["state"]
    return data["state"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True, help="series JSON from fetch_patchwork_series.py")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.series) as f:
        series_list = json.load(f)

    cache = {}
    results = {}
    try:
        for s in series_list:
            key = s["series_title"] + "|" + s["latest_version"]
            patch_results = []
            for p in s["patches"]:
                patch_id = p.get("patchwork_id")
                state = fetch_patch_state(patch_id, cache) if patch_id else None
                status = STATE_TO_STATUS.get(state)
                patch_results.append(
                    {
                        "subject": p["subject"],
                        "message_id": p["message_id"],
                        "patchwork_state": state,
                        "status": status,
                    }
                )
            results[key] = patch_results
    finally:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)
    print(args.out)


if __name__ == "__main__":
    main()
