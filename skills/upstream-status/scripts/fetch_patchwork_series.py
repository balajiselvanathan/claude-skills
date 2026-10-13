#!/usr/bin/env python3
"""List patch series authored by a given author, sourced from the
patchwork.ozlabs.org REST API (project "uboot") instead of parsing raw
mail subjects out of the lore.kernel.org mirror.

Patchwork gives each submitted series a first-class, maintainer-curated
identity (series id, explicit integer `version`, grouped `patches`,
`cover_letter`) — this removes the need to regex-parse "[PATCH v2 3/4]"
style subjects or guess version numbers.

What patchwork does NOT do: link multiple versions of the same series
together — every resend is a brand new series object. So version-grouping
across resends still needs a client-side merge, same spirit as the old
mail-only approach but with much better signal: an explicit per-version
`version` integer (no regex), a per-patch/cover title normalized by
stripping the leading "[vN]"/"[N/M]" bracket tag, and (when present) the
b4 slug embedded in the Message-Id. Union-find merges series across ANY
shared signal, mirroring the rationale in the old list_my_series.py
docstring: no single signal (slug, cover title, patch-1 title) is reliable
alone across every resend.

Each patch's `sha` (needed later for git patch-id branch matching against
the lore mirror) is resolved by looking up its Message-Id in the prebuilt
mail index (build_mail_index.py) — patchwork doesn't carry a lore mirror
commit sha, only the mail body is in the lore mirror.

Output JSON shape matches the former list_my_series.py output exactly, so
match_patch_ids.py / build_report.py don't need to change their series.json
contract: a list of {series_title, latest_version, num_patches, date,
v1_date, lore_url, patches: [{sha, subject, part, message_id}]}.
"""
import argparse
import json
import re
import subprocess
import urllib.request
from collections import defaultdict

PATCHWORK_API = "https://patchwork.ozlabs.org/api/1.2"
USER_AGENT = "Mozilla/5.0 (upstream-status-skill)"

# Matches one leading bracket tag whose content is only RFC/RESEND/PATCH
# markers, a version (vN), and/or an N/M part counter, in any order/combo
# separated by commas or spaces — e.g. "[v2]", "[RFC v1]", "[2/4]",
# "[RESEND,v1]", "[RESEND,v5,0/5]", "[RFC,v1,0/3]". b4/git-send-email tools
# comma-join multiple tags into one bracket (unlike plain git send-email's
# single-purpose brackets), so the content has to be validated as a whole
# rather than matched by a single fixed pattern.
BRACKET_PREFIX_RE = re.compile(
    r"^\[\s*(?:(?:RFC|RESEND|PATCH)|v\d+|\d+/\d+)(?:[,\s]+(?:(?:RFC|RESEND|PATCH)|v\d+|\d+/\d+))*\s*\]\s*",
    re.IGNORECASE,
)
# A literal RESEND tag in a leading bracket is an explicit, author-asserted
# signal (not a coincidental title/slug collision) that this submission is
# the same series as an earlier one — trusted to merge even when patchwork
# left the version number unchanged (a true resend, as opposed to a version
# bump, keeps the same vN).
RESEND_TAG_RE = re.compile(r"^\[[^\]]*\bRESEND\b[^\]]*\]", re.IGNORECASE)
B4_SLUG_RE = re.compile(r"^\d{8}-(?:b4-)?([a-zA-Z0-9_]+)-v\d+-\d+-[0-9a-f]+@")
# plain `git send-email` Message-Id format: <timestamp>.<pid>-<N>-<email>,
# e.g. 20250531032008.3083716-2-balaji.selvanathan@oss.qualcomm.com. All
# patches/cover letter posted in the same send-email invocation share the
# <timestamp>.<pid> prefix. This catches patches patchwork's own series
# grouping drops (e.g. a patch whose subject is missing the "N/M" counter,
# which breaks patchwork's cover-letter-based association even though the
# patch was sent in the same batch) when title/slug signals don't connect
# them either.
SENDEMAIL_BATCH_RE = re.compile(r"^(\d+\.[0-9a-zA-Z]+)-\d+-")
LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def strip_brackets(title):
    title = title or ""
    while True:
        m = BRACKET_PREFIX_RE.match(title)
        if not m:
            return title.strip()
        title = title[m.end():]


def naked_msgid(msgid):
    if not msgid:
        return None
    return msgid.strip().lstrip("<").rstrip(">")


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        link_header = resp.headers.get("Link", "")
    return json.loads(body), link_header


def fetch_all_series(project, submitter_email):
    url = f"{PATCHWORK_API}/series/?project={project}&submitter={submitter_email}&page_size=250"
    out = []
    while url:
        page, link_header = http_get_json(url)
        out.extend(page)
        m = LINK_NEXT_RE.search(link_header or "")
        url = m.group(1) if m else None
    return out


def resolve_author_email(repo, author):
    if "@" in author:
        return author
    out = subprocess.run(
        ["git", "log", "--all", f"--author={author}", "--format=%ae"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    emails = sorted(set(e for e in out.splitlines() if e.strip()))
    if len(emails) == 0:
        raise SystemExit(f"No commits found for author '{author}' in {repo}")
    if len(emails) > 1:
        raise SystemExit(
            f"Author '{author}' is ambiguous — matched multiple emails: {emails}. "
            "Re-run with one of these exact emails."
        )
    return emails[0]


def batch_key(msgid):
    m = SENDEMAIL_BATCH_RE.match(naked_msgid(msgid) or "")
    return ("batch", m.group(1)) if m else None


def signal_keys(series_obj):
    """Returns (strong_keys, weak_keys). Strong keys (the exact
    git-send-email batch prefix shared by every patch posted in one
    `git send-email` invocation) are trusted to merge series regardless of
    version — they only ever collide for patches that were truly sent
    together. Weak keys (cover/patch title, b4 slug) are common,
    short strings that can coincidentally collide between two unrelated
    series that both happen to be a first-ever v1 posting (seen in
    practice: a shared b4 slug, or a shared prerequisite-patch title
    pulled into two different submissions) — so the caller only trusts a
    weak-key match to merge two series when their version numbers differ,
    i.e. when it looks like an actual resend rather than two unrelated v1s.
    """
    strong = set()
    weak = set()
    cover = series_obj.get("cover_letter")
    cover_title = strip_brackets(cover["name"]) if cover else None
    if cover_title and cover_title.lower() not in ("(no cover subject)", ""):
        weak.add(("title", cover_title.lower()))
    for p in series_obj.get("patches", []):
        weak.add(("ptitle", strip_brackets(p["name"]).lower()))
        slug_m = B4_SLUG_RE.match(naked_msgid(p.get("msgid")) or "")
        if slug_m:
            weak.add(("slug", slug_m.group(1)))
        bkey = batch_key(p.get("msgid"))
        if bkey:
            strong.add(bkey)
    if cover:
        slug_m = B4_SLUG_RE.match(naked_msgid(cover.get("msgid")) or "")
        if slug_m:
            weak.add(("slug", slug_m.group(1)))
        bkey = batch_key(cover.get("msgid"))
        if bkey:
            strong.add(bkey)
    if not strong and not weak:
        weak.add(("title", strip_brackets(series_obj["name"]).lower()))
    # A cover letter or patch explicitly tagged [RESEND...] is an
    # author-asserted claim of being the same series as an earlier posting,
    # not a coincidental collision — promote its (bracket-stripped) title to
    # a strong key so it merges with the original even when both carry the
    # same version number (a true resend, unlike a version bump, leaves
    # patchwork's `version` unchanged). Patchwork's own `series.name` field
    # is always pre-stripped of bracket tags by patchwork itself, so the tag
    # can only be observed on the raw cover-letter/patch `name` fields —
    # checking `series_obj["name"]` would never fire. Added under both the
    # "title" and "ptitle" namespaces since a single-patch, no-cover-letter
    # series' matching weak key is "ptitle".
    resend_raw_titles = [p["name"] for p in series_obj.get("patches", [])]
    if cover:
        resend_raw_titles.append(cover["name"])
    for raw_title in resend_raw_titles:
        if RESEND_TAG_RE.match(raw_title or ""):
            resend_title = strip_brackets(raw_title).lower()
            strong.add(("title", resend_title))
            strong.add(("ptitle", resend_title))
    return strong, weak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to lore mirror clone (for author email resolution)")
    ap.add_argument("--author", required=True, help="git log --author style value, or an exact email")
    ap.add_argument("--mail-index", required=True, help="JSON from build_mail_index.py")
    ap.add_argument("--project", default="uboot")
    ap.add_argument("--since", help="ISO date (YYYY-MM-DD), inclusive; filters by series' earliest patch date")
    ap.add_argument("--until", help="ISO date (YYYY-MM-DD), inclusive")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    email = resolve_author_email(args.repo, args.author)

    with open(args.mail_index) as f:
        mail_index = json.load(f)

    series_objs = fetch_all_series(args.project, email)
    if not series_objs:
        raise SystemExit(f"No patchwork series found for submitter '{email}' in project '{args.project}'")

    series_ids = [s["id"] for s in series_objs]
    thread_keys = {s["id"]: signal_keys(s) for s in series_objs}

    parent = {sid: sid for sid in series_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_id = {s["id"]: s for s in series_objs}

    # strong keys (shared git-send-email batch prefix, or a title explicitly
    # tagged [RESEND...]) merge unconditionally. They are matched against
    # every OTHER series' full key set (strong + weak), not just other
    # series' strong sets — e.g. a [RESEND,v1] series' strong title key must
    # still find the original posting's plain title, which only carries
    # that same text as a weak key.
    all_keys = {sid: strong | weak for sid, (strong, weak) in thread_keys.items()}
    strong_key_to_series = defaultdict(list)
    for sid, (strong, _weak) in thread_keys.items():
        for key in strong:
            strong_key_to_series[key].append(sid)
    for key, strong_sids in strong_key_to_series.items():
        matching_sids = [sid for sid in series_ids if key in all_keys[sid]]
        for other in matching_sids[1:]:
            union(matching_sids[0], other)

    # weak keys (title/ptitle/slug) only merge pairs with differing version
    # numbers — i.e. they look like an actual resend of the same series,
    # not two coincidentally-similar standalone v1 submissions.
    weak_key_to_series = defaultdict(list)
    for sid, (_strong, weak) in thread_keys.items():
        for key in weak:
            weak_key_to_series[key].append(sid)
    for key, sids in weak_key_to_series.items():
        for i in range(len(sids)):
            for j in range(i + 1, len(sids)):
                a, b = sids[i], sids[j]
                if by_id[a]["version"] != by_id[b]["version"]:
                    union(a, b)

    groups = defaultdict(list)
    for sid in series_ids:
        groups[find(sid)].append(by_id[sid])

    series_out = []
    for group in groups.values():
        max_version = max(s["version"] for s in group)
        latest_version = f"v{max_version}"
        # a patchwork-side split (e.g. a patch whose subject is missing its
        # N/M counter) can leave the same version number spread across
        # multiple series objects in this group — merge all of their
        # patches together rather than taking just one object's list.
        latest_objs = [s for s in group if s["version"] == max_version]

        cover = next((s.get("cover_letter") for s in latest_objs if s.get("cover_letter")), None)

        # Multiple series objects can land in latest_objs for two very
        # different reasons: (a) patchwork split one git-send-email batch
        # into separate series objects (IQ8 bug — genuinely distinct patches
        # of the same submission, linked by a shared "batch" strong key —
        # must union their patches), or (b) a literal [RESEND] of the exact
        # same version (linked only by a "title"/"ptitle" strong key from
        # RESEND_TAG_RE, with its own distinct batch prefix since it's a
        # separate git-send-email invocation) — in that case the resend's
        # patches are duplicates of the same content under new Message-Ids,
        # not additional patches, so only the most-recently-dated object's
        # patches should be kept.
        batch_keys_seen = defaultdict(set)
        for s in latest_objs:
            for key in thread_keys[s["id"]][0]:
                if key[0] == "batch":
                    batch_keys_seen[key].add(s["id"])
        shares_batch = any(len(sids) > 1 for sids in batch_keys_seen.values())

        if len(latest_objs) > 1 and not shares_batch:
            latest_objs_for_patches = [max(latest_objs, key=lambda s: s["date"])]
        else:
            latest_objs_for_patches = latest_objs

        seen_msgids = set()
        patches = []
        for s in latest_objs_for_patches:
            for p in s.get("patches", []):
                mid = naked_msgid(p.get("msgid"))
                if mid in seen_msgids:
                    continue
                seen_msgids.add(mid)
                patches.append(p)
        patches.sort(key=lambda p: p["date"])

        # patchwork's own `total` field can lag behind the patches it has
        # actually associated with the series (seen in practice: `total: 2`
        # on a series whose 3 patches are all present with correct N/3
        # counters) - trust whichever is larger, since our own patches list
        # is a strict union of everything patchwork associated with any
        # version in this merged group.
        num_patches = max([s.get("total") or 0 for s in latest_objs_for_patches] + [len(patches)])

        cover_title = strip_brackets(cover["name"]) if cover else None
        if cover_title and cover_title.lower() in ("(no cover subject)", ""):
            cover_title = None
        title_display = cover_title or (
            strip_brackets(patches[0]["name"]) if patches else strip_brackets(latest_objs[0]["name"])
        )

        link_msgid = naked_msgid(cover["msgid"]) if cover else (
            naked_msgid(patches[0]["msgid"]) if patches else None
        )
        lore_url = f"https://lore.kernel.org/u-boot/{link_msgid}/" if link_msgid else None

        v1_candidates = [s for s in group if s["version"] == 1]
        v1_date = min((s["date"] for s in v1_candidates), default=None) or min(s["date"] for s in group)

        patch_info = []
        for idx, p in enumerate(patches):
            mid = naked_msgid(p.get("msgid"))
            sha = mail_index.get(mid, {}).get("sha") if mid else None
            patch_info.append(
                {
                    "sha": sha,
                    "subject": p["name"],
                    "part": idx + 1,
                    "message_id": mid,
                    "patchwork_id": p["id"],
                }
            )

        series_out.append(
            {
                "series_title": title_display,
                "latest_version": latest_version,
                "num_patches": num_patches,
                "date": max(s["date"] for s in latest_objs),
                "v1_date": v1_date,
                "lore_url": lore_url,
                "patches": patch_info,
            }
        )

    series_out.sort(key=lambda s: s["date"])

    if args.since:
        series_out = [s for s in series_out if s["date"][:10] >= args.since]
    if args.until:
        series_out = [s for s in series_out if s["date"][:10] <= args.until]

    with open(args.out, "w") as f:
        json.dump(series_out, f, indent=2)

    print(args.out)


if __name__ == "__main__":
    main()
