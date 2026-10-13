#!/usr/bin/env python3
"""For each patch message-id in a series JSON (from fetch_patchwork_series.py),
find replies via the prebuilt mail index (build_mail_index.py) - any message
whose References/In-Reply-To list contains the target message-id.

This exists because patchwork's per-patch `state` field
(fetch_patchwork_state.py) is maintainer/reviewer-curated and sometimes never
gets updated even after a reviewer has already replied with comments on the
list - a patch can sit at `state: new` indefinitely despite genuine review
activity. build_report.py uses this script's output to upgrade such patches
to "Reviewed - changes requested" rather than trusting patchwork's `new`/
`under-review` state at face value. A tag-only reply (Reviewed-by/Acked-by/
Tested-by, no prose) is NOT review-pending signal on its own - it's treated
the same as "no reply" here, since build_report.py already treats
patchwork's `accepted` as the stronger, more specific signal; a plain prose
reply (no tag) is what indicates an unresolved review comment.

Output JSON: { "<series_title>|<version>": [ {subject, message_id,
prose_replies, reply_tags}, ... ] }
"""
import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True, help="series JSON from fetch_patchwork_series.py")
    ap.add_argument("--mail-index", required=True, help="JSON from build_mail_index.py")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.series) as f:
        series_list = json.load(f)
    with open(args.mail_index) as f:
        mail_index = json.load(f)

    # msgid -> list of (reply_msgid, reply_entry) referencing it
    replies_by_target = {}
    for msgid, entry in mail_index.items():
        for ref in entry.get("refs", []):
            replies_by_target.setdefault(ref, []).append((msgid, entry))

    results = {}
    for s in series_list:
        key = s["series_title"] + "|" + s["latest_version"]
        patch_results = []
        for p in s["patches"]:
            mid = p.get("message_id")
            reply_tags = []
            prose_replies = 0
            for reply_mid, reply_entry in replies_by_target.get(mid, []) if mid else []:
                if reply_entry.get("tags"):
                    reply_tags.extend(reply_entry["tags"])
                else:
                    prose_replies += 1
            patch_results.append(
                {
                    "subject": p["subject"],
                    "message_id": mid,
                    "prose_replies": prose_replies,
                    "reply_tags": reply_tags,
                }
            )
        results[key] = patch_results

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(args.out)


if __name__ == "__main__":
    main()
