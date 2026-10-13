#!/usr/bin/env python3
"""Combine fetch_patchwork_series.py + match_patch_ids.py +
fetch_patchwork_state.py output into the final per-series status report,
applying the master > next > qcom-next precedence rule (if a patch landed
in master, ignore next/qcom-next; if in next, ignore qcom-next).

A series' status is derived from its patches' individual statuses:
  - Tier 1 (authoritative): if ANY patch matched a branch via git patch-id
    (match_patch_ids.py), that's definitive proof of landing — report the
    lowest tier reached by any patch in the series (conservative: the
    series isn't fully landed at a higher tier until every patch is
    there).
  - Tier 2 (fallback): if no patch in the series matched any branch, fall
    back to patchwork's own per-patch `state` (fetch_patchwork_state.py).
    Patchwork's "accepted" doesn't distinguish which branch, only that a
    maintainer accepted it — if patch-id matching couldn't confirm a
    branch despite that, report "Accepted, awaiting pick-up" rather than
    contradicting patchwork. Any patch reported "Rejected" by patchwork
    makes the whole series "Rejected" (same conservative logic as
    changes-requested below). Otherwise, any patch in "Reviewed - changes
    requested" makes the whole series that status, regardless of other
    patches' states (an unresolved comment thread means the series isn't
    ready to merge). Otherwise "Not yet reviewed", UNLESS
    check_review_replies.py found a plain-prose (non-tag) reply on the
    thread for one of those patches — reviewers routinely leave review
    comments on the list without ever flipping patchwork's state field
    (it sits at `new`/`under-review` indefinitely), so a real review
    reply overrides a stale/never-updated patchwork state and upgrades
    the patch to "Reviewed - changes requested" too. This cross-check
    only fires when patchwork hasn't already reported something more
    specific (accepted/rejected/changes-requested) for that patch.

Patchwork state is authoritative for review signal (maintainer-curated,
not inferred from raw mail-thread text) EXCEPT for the reply-based
override above, which exists only because that curation is sometimes
never done. It is never authoritative for which destination branch a
patch landed in — patchwork has no structured concept of custodian
branches, so that detection always comes from Tier 1.
"""
import argparse
import json
from collections import Counter
from datetime import datetime, timezone

TIER_ORDER = ["master", "next", "qcom-next"]
TIER_LABEL = {
    "master": "Merged in u-boot master",
    "next": "Merged in u-boot next",
    "qcom-next": "Merged in qcom-next",
}

STATUS_DISPLAY_ORDER = [
    "Merged in u-boot master",
    "Merged in u-boot next",
    "Merged in qcom-next",
    "Accepted, awaiting pick-up",
    "Reviewed - changes requested",
    "Not yet reviewed",
    "Rejected",
]


def sorted_status_counts(counter):
    ordered = [(s, counter[s]) for s in STATUS_DISPLAY_ORDER if counter.get(s)]
    leftover = [(s, c) for s, c in counter.items() if s not in STATUS_DISPLAY_ORDER]
    return ordered + leftover


def best_tier(matches):
    for tier in TIER_ORDER:
        if tier in matches:
            return tier
    return None


def format_days_old(days):
    months, rem_days = divmod(days, 30)
    if months and rem_days:
        return f"{months} months {rem_days} days"
    if months:
        return f"{months} months"
    return f"{rem_days} days"


def series_status(patch_matches, patchwork_info, review_replies):
    tiers_hit = [best_tier(p["matches"]) for p in patch_matches]
    tiers_hit = [t for t in tiers_hit if t]

    if tiers_hit:
        # conservative: report the *lowest* tier reached by any patch,
        # since the series isn't fully landed at a higher tier until
        # every patch in it is.
        worst = max(tiers_hit, key=lambda t: TIER_ORDER.index(t))
        return TIER_LABEL[worst]

    if not patchwork_info:
        return "Not yet reviewed"

    # a series where every patch's patchwork state is "superseded" is
    # itself an older/replaced revision - even if our title-based
    # resend-grouping (fetch_patchwork_series.py) didn't connect it to
    # whatever later-titled resend replaced it. Exclude it entirely
    # rather than defaulting it to "Not yet reviewed", which would be
    # misleading (it HAS been acted on, just not under this series key).
    if all(p.get("patchwork_state") == "superseded" for p in patchwork_info):
        return None

    statuses = [p["status"] for p in patchwork_info if p.get("status")]

    if "Rejected" in statuses:
        return "Rejected"
    if "Reviewed - changes requested" in statuses:
        return "Reviewed - changes requested"
    if "Accepted, awaiting pick-up" in statuses:
        return "Accepted, awaiting pick-up"

    # patchwork's state field is sometimes never updated even after a
    # reviewer has replied with comments on the list - cross-check
    # against actual reply activity before defaulting to "not reviewed".
    if any(p.get("prose_replies") for p in review_replies):
        return "Reviewed - changes requested"

    return "Not yet reviewed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True)
    ap.add_argument("--patch-id-matches", required=True)
    ap.add_argument("--patchwork-state", required=True)
    ap.add_argument("--review-replies", required=True, help="JSON from check_review_replies.py")
    ap.add_argument("--out")
    ap.add_argument("--md-out", help="write the report as a markdown table to this path (e.g. ./upstream-status-report.md)")
    ap.add_argument("--author", default="", help="only used for the markdown report header")
    ap.add_argument("--since", default="", help="only used for the markdown report header")
    ap.add_argument("--until", default="", help="only used for the markdown report header")
    args = ap.parse_args()

    with open(args.series) as f:
        series_list = json.load(f)
    with open(args.patch_id_matches) as f:
        matches = json.load(f)
    with open(args.patchwork_state) as f:
        patchwork_states = json.load(f)
    with open(args.review_replies) as f:
        review_replies = json.load(f)

    rows = []
    now = datetime.now(timezone.utc)
    for s in series_list:
        key = s["series_title"] + "|" + s["latest_version"]
        patch_matches = matches.get(key, [])
        patchwork_info = patchwork_states.get(key)
        replies_info = review_replies.get(key, [])
        status = series_status(patch_matches, patchwork_info, replies_info)
        if status is None:
            continue  # fully-superseded series under this title - excluded
        latest_dt = datetime.fromisoformat(s["date"])
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
        days_old = (now - latest_dt).days
        rows.append(
            {
                "series_title": s["series_title"],
                "version": s["latest_version"],
                "num_patches": s["num_patches"],
                "status": status,
                "days_old": days_old,
                "days_old_display": format_days_old(days_old),
                "lore_url": s.get("lore_url"),
            }
        )

    if args.out:
        with open(args.out, "w") as f:
            json.dump(rows, f, indent=2)

    print("Patch Series\tVersion\tNumber of Patches\tStatus\tDays Old")
    for r in rows:
        print(f"{r['series_title']}\t{r['version']}\t{r['num_patches']}\t{r['status']}\t{r['days_old_display']}")
        if r["lore_url"]:
            print(f"  {r['lore_url']}")

    series_counts = Counter(r["status"] for r in rows)
    patch_counts = Counter()
    for r in rows:
        patch_counts[r["status"]] += r["num_patches"]

    print("\nSummary by series:")
    for status, count in sorted_status_counts(series_counts):
        print(f"  {status}: {count}")
    print(f"  Total series: {sum(series_counts.values())}")

    print("\nSummary by patch:")
    for status, count in sorted_status_counts(patch_counts):
        print(f"  {status}: {count}")
    print(f"  Total patches: {sum(patch_counts.values())}")

    if args.md_out:
        period = "All time"
        if args.since or args.until:
            period = f"{args.since or '...'} to {args.until or '...'}"
        lines = [
            "# u-boot Upstream Patch Series Status",
            "",
            f"Author: {args.author or 'unspecified'} | Period: {period}",
            "",
            "| Patch Series | Version | Number of Patches | Status | Days Old |",
            "|---|---|---|---|---|",
        ]
        for r in rows:
            title = r["series_title"].replace("|", "\\|")
            if r["lore_url"]:
                title = f"{title}<br>[{r['lore_url']}]({r['lore_url']})"
            lines.append(f"| {title} | {r['version']} | {r['num_patches']} | {r['status']} | {r['days_old_display']} |")
        lines.append("")
        lines.append("## Summary by series")
        lines.append("")
        lines.append("| Status | Series |")
        lines.append("|---|---|")
        for status, count in sorted_status_counts(series_counts):
            lines.append(f"| {status} | {count} |")
        lines.append(f"| **Total** | **{sum(series_counts.values())}** |")
        lines.append("")
        lines.append("## Summary by patch")
        lines.append("")
        lines.append("| Status | Patches |")
        lines.append("|---|---|")
        for status, count in sorted_status_counts(patch_counts):
            lines.append(f"| {status} | {count} |")
        lines.append(f"| **Total** | **{sum(patch_counts.values())}** |")
        lines.append("")
        with open(args.md_out, "w") as f:
            f.write("\n".join(lines))
        print(f"\nMarkdown report written to {args.md_out}")


if __name__ == "__main__":
    main()
