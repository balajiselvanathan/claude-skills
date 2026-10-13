---
description: "Report how many u-boot patch series I've submitted upstream in a given time period, and their status: merged in u-boot master, merged in u-boot next, merged in the Qualcomm qcom-next custodian branch, accepted and awaiting pick-up, changes requested, or not yet reviewed. One row per series (latest version only), following a master > next > qcom-next precedence (a patch merged in master is never also reported against next/qcom-next). Defaults to the user's own submissions but accepts any author name/email. Trigger phrases: uboot series status, my patch series status, how many patches did I submit upstream, uboot upstream submission report, <name>'s uboot patch status."
argument-hint: "[author name/email] [since YYYY-MM-DD] [until YYYY-MM-DD]"
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
---

# upstream-status

Report the status of a u-boot developer's patch series submitted to the upstream mailing list (defaulting to the user themself), over a given time period, against three destination tiers (precedence high to low): `u-boot master` > `u-boot next` > Qualcomm's `qcom-next` custodian branch. Output is one row per series (latest version only), in the format:

```
Patch Series    Version    Number of Patches    Status    Days Old
```

"Days Old" is the time elapsed since the latest version of the series was posted (not v1), formatted as "X months Y days" (months = days // 30). Below each series title, the report also links to the series' lore.kernel.org thread (cover letter if there is one, else the first patch of the latest version).

This skill only reads public git mirrors/clones — it never posts, emails, or pushes anything.

## Background the model should know before running this

- Series grouping and review status come from the **patchwork REST API** (`https://patchwork.ozlabs.org/api/1.2`, project `uboot`), not from parsing raw mail subjects. Patchwork gives each submission a maintainer-curated `series` object (explicit integer `version`, grouped `patches`, optional `cover_letter`) and a per-patch `state` enum (`new`, `under-review`, `changes-requested`, `rejected`, `accepted`, `superseded`, etc.) — this is more reliable than inferring either from mail text.
  - The API is reachable with a plain `curl`/`urllib` request as long as a browser-like `User-Agent` header is set (e.g. `Mozilla/5.0 (upstream-status-skill)`) — patchwork's Anubis bot-protection only guards the HTML frontend, not `/api/...` endpoints. No cookies, auth, or special headers needed for read-only GETs.
  - Patchwork does **not** link multiple versions of the same series together — every resend is a separate `series` object with no back-reference to earlier versions. `fetch_patchwork_series.py` merges resends into one logical series client-side via union-find across two signal tiers: a **strong** signal (the exact `<timestamp>.<pid>` batch prefix shared by every patch/cover-letter posted in one `git send-email` invocation, or the b4 slug for b4-style Message-Ids) that merges unconditionally, and a **weak** signal (cover-letter title, per-patch title, bracket-tag stripped) that only merges two series when their `version` numbers differ — i.e. it looks like an actual resend, not two coincidentally-similar standalone v1 submissions. The weak-signal version-gate exists because a bare title/slug match on two *same-version* series has been observed to produce false merges (e.g. two unrelated v1 series that both happened to post a patch titled identically, or whose b4 slugs happened to collide) — gating weak signals on a version difference preserves genuine resend-merging while rejecting those collisions. The strong batch-prefix signal was added specifically because patchwork's own series grouping can itself under-group a submission: if one patch in a `git send-email` batch is missing its `N/M` subject counter (e.g. `[v5] doc: ...` instead of `[v5,2/6] doc: ...`), patchwork puts it in its own single-patch series object even though its own API data (`series.total` vs `len(series.patches)`) shows it's part of a larger series — title/slug alone can't reconnect it since the two series' titles/patch titles may share nothing in common, but the shared `<timestamp>.<pid>` Message-Id prefix always does.
  - Patchwork's `submitter` filter needs an exact email, not a free-text name — a bare "firstname" substring returns zero results. Resolve the user-given author (name or email) to an exact email via `git log --author=<value> --format=%ae` against the lore mirror first (same ambiguity/zero-match handling as before: ask via `AskUserQuestion` rather than guessing).
- The lore.kernel.org mirror at `/local/mnt/workspace/bselvana/lore_kernel/u-boot-epoch1` (an epoch clone of `https://lore.kernel.org/u-boot/1`, bypassing the HTML frontend's Anubis protection the same way) is now used for exactly one thing: resolving a patch's Message-Id into its lore-mirror commit sha, which `match_patch_ids.py` needs to run `git patch-id --stable` on the patch's raw email body (`git show <sha>:m`). `build_mail_index.py` indexes Message-Id -> sha for this lookup only.
- Destination branches live in two upstream clones (not source.denx.de — that's a read-only GitLab *mirror*; the canonical repos are on `git.u-boot-project.org`):
  - `/local/mnt/workspace/bselvana/.cache/uboot-series-status-mainline` — clone of `https://git.u-boot-project.org/u-boot/u-boot`, branches `master`, `next`.
  - `/local/mnt/workspace/bselvana/.cache/uboot-series-status-qcom-next` — clone of `https://git.u-boot-project.org/u-boot/custodians/u-boot-snapdragon`, branch `qcom-next`.
  If either clone doesn't exist yet, create it (`git clone <url> <path>`); this is a one-time cost, ~600MB each. On every run, fetch the specific branches needed (`git fetch origin master next` / `git fetch origin qcom-next`) rather than a full `git clone` — the clone should be reused across runs, not recreated.
- Matching a submitted patch against a destination branch (**Tier 1, authoritative**): `git patch-id --stable` computed on the raw email body (from the lore mirror, via the sha resolved above) is compared against patch-ids of every commit reachable from the destination branch tip. `git patch-id` tolerates leading email/header noise and finds the embedded diff itself, so no manual diff extraction is needed — feed the whole `git show <sha>:m` output straight into it. This is checked first and takes precedence over patchwork's own `state`, because patchwork's `accepted` doesn't say *which* branch (master vs next vs qcom-next) a patch landed in — that's an upstream-git-tree concept patchwork's schema has no field for (its `commit_ref` field is a free-text, maintainer-typed value, unreliable and not branch-structured).
- **Tier 2 (fallback, when no patch-id match at any destination branch)**: use patchwork's per-patch `state` directly (`fetch_patchwork_state.py`). `accepted`/`queued` -> "Accepted, awaiting pick-up" (patchwork says a maintainer took it, but Tier 1 couldn't confirm which branch yet — report it as pending pick-up rather than contradicting patchwork). `changes-requested` -> "Reviewed - changes requested"; an unresolved comment thread on even one patch means the series isn't ready to merge, regardless of how many other patches in it were separately accepted. `rejected`/`not-applicable` -> "Rejected". `superseded` is excluded entirely (an older version was replaced — consistent with "latest version only"). Everything else (`new`, `under-review`, `rfc`, `deferred`, `awaiting-upstream`, `needs-ack`, or no patchwork data at all) -> "Not yet reviewed", **unless** `check_review_replies.py` finds an actual plain-prose reply on that patch's thread in the lore mirror — reviewers often leave review comments without ever flipping patchwork's state field (patchwork relies on a maintainer/reviewer action that's easy to forget), so a real reply overrides the stale `new`/`under-review` state and the patch is still reported as "Reviewed - changes requested". A tag-only reply (bare `Reviewed-by:`/`Acked-by:`/`Tested-by:`, no prose) does NOT trigger this override — only a reply with actual review prose counts, since a tag alone isn't a request for changes.
- The user's explicit precedence rule: if a patch landed in `master`, ignore `next`/`qcom-next` for it; if in `next`, ignore `qcom-next`. Applied at the *series* level conservatively: a series is only reported at a given tier once every one of its patches has reached at least that tier — if patches within one series matched inconsistently across tiers, report the lowest tier reached by any patch in it (do not report a series as "merged in master" if only some of its patches actually made it that far).

## Parse skill arguments

Determine the **author** to report on: default to "Balaji Selvanathan" (the user) unless the user names someone else (a colleague's name or email) — in that case use whatever they gave verbatim as the `--author` value below. `git log --author=<value>` does a substring match against the commit author field (derived from the email `From:` header in the lore mirror), so a bare first/last name or an email address both work; if a name is ambiguous (e.g. matches multiple people) or returns zero commits, say so and ask which person/email they meant via `AskUserQuestion` rather than silently reporting an empty table.

Accept optional `since`/`until` dates (`YYYY-MM-DD`). If the user's invocation is ambiguous about the time period (e.g. they said "this quarter" or gave nothing at all), ask via `AskUserQuestion` rather than guessing — this materially changes which series are in scope. If no period is ultimately wanted (report *all* series ever posted by that author), that's a valid answer too.

## Step 1 — Ensure the mail index is fresh

```bash
cd /local/mnt/workspace/bselvana/lore_kernel/u-boot-epoch1 && git pull --quiet
```

Then rebuild the mail index (this is a full index of the whole mailing list, not just the user's messages — needed so `fetch_patchwork_series.py` can resolve any patch's Message-Id into its lore-mirror commit sha for Step 4's patch-id matching):

```bash
python3 ~/.claude/skills/upstream-status/scripts/build_mail_index.py \
  --repo /local/mnt/workspace/bselvana/lore_kernel/u-boot-epoch1 \
  --out /tmp/upstream-status/mail_index.json
```

Takes under two minutes for the full ~100k-message archive. Reuse this file across runs within the same session rather than rebuilding it per invocation — only rebuild if the lore mirror was just pulled and might have new mail.

## Step 2 — Fetch the target author's series from patchwork, grouped and filtered

```bash
python3 ~/.claude/skills/upstream-status/scripts/fetch_patchwork_series.py \
  --repo /local/mnt/workspace/bselvana/lore_kernel/u-boot-epoch1 \
  --author "<resolved author name/email>" \
  --mail-index /tmp/upstream-status/mail_index.json \
  [--since <DATE>] [--until <DATE>] \
  --out /tmp/upstream-status/series.json
```

Use the author resolved in "Parse skill arguments" above (default: `"Balaji Selvanathan"`). If running for multiple different authors in the same session, use a distinct `--out` path per author (e.g. suffix with a slug of the name) so results don't overwrite each other.

Produces one entry per logical series (already collapsed to latest-version-only, per union-find grouping), each with `series_title`, `latest_version`, `num_patches`, `date` (earliest date among the latest version's entries), `v1_date` (date the series' v1 was first posted, falling back to the earliest date across all versions if no entry was literally tagged v1), `lore_url` (the lore.kernel.org permalink for the cover letter, or the first patch of the latest version if there's no cover letter), and a `patches` list (`sha`, `subject`, `part`, `message_id`).

## Step 3 — Ensure destination clones exist and are up to date

```bash
[ -d /local/mnt/workspace/bselvana/.cache/uboot-series-status-mainline ] || \
  git clone https://git.u-boot-project.org/u-boot/u-boot /local/mnt/workspace/bselvana/.cache/uboot-series-status-mainline
git -C /local/mnt/workspace/bselvana/.cache/uboot-series-status-mainline fetch origin master next --quiet

[ -d /local/mnt/workspace/bselvana/.cache/uboot-series-status-qcom-next ] || \
  git clone https://git.u-boot-project.org/u-boot/custodians/u-boot-snapdragon /local/mnt/workspace/bselvana/.cache/uboot-series-status-qcom-next
git -C /local/mnt/workspace/bselvana/.cache/uboot-series-status-qcom-next fetch origin qcom-next --quiet
```

## Step 4 — Patch-id match against all three branches

```bash
python3 ~/.claude/skills/upstream-status/scripts/match_patch_ids.py \
  --src-repo /local/mnt/workspace/bselvana/lore_kernel/u-boot-epoch1 \
  --series /tmp/upstream-status/series.json \
  --dest master /local/mnt/workspace/bselvana/.cache/uboot-series-status-mainline:origin/master \
  --dest next /local/mnt/workspace/bselvana/.cache/uboot-series-status-mainline:origin/next \
  --dest qcom-next /local/mnt/workspace/bselvana/.cache/uboot-series-status-qcom-next:origin/qcom-next \
  --out /tmp/upstream-status/patch_id_matches.json
```

This indexes patch-ids for all commits reachable from each of the three branches (can take a few minutes the first time; git's own object cache makes reruns faster) and reports, per patch, which branch(es) it matches.

## Step 5 — Fetch patchwork review state (fallback signal)

```bash
python3 ~/.claude/skills/upstream-status/scripts/fetch_patchwork_state.py \
  --series /tmp/upstream-status/series.json \
  --out /tmp/upstream-status/patchwork_state.json
```

## Step 5b — Cross-check against actual mailing-list reply activity

```bash
python3 ~/.claude/skills/upstream-status/scripts/check_review_replies.py \
  --series /tmp/upstream-status/series.json \
  --mail-index /tmp/upstream-status/mail_index.json \
  --out /tmp/upstream-status/review_replies.json
```

Patchwork's `state` field (Step 5) is maintainer/reviewer-curated and is routinely never updated even after a reviewer has already replied with review comments on the list — this step catches that gap by finding real reply messages (via References/In-Reply-To in the mail index) so `build_report.py` can upgrade a patch stuck at patchwork `new`/`under-review` to "Reviewed - changes requested" when a genuine prose reply exists.

## Step 6 — Build and print the final report

```bash
python3 ~/.claude/skills/upstream-status/scripts/build_report.py \
  --series /tmp/upstream-status/series.json \
  --patch-id-matches /tmp/upstream-status/patch_id_matches.json \
  --patchwork-state /tmp/upstream-status/patchwork_state.json \
  --review-replies /tmp/upstream-status/review_replies.json \
  --out /tmp/upstream-status/final_report.json \
  --md-out ./upstream-status-report.md \
  --author "<resolved author name/email>" \
  [--since <DATE>] [--until <DATE>]
```

This prints the tab-separated report directly (`Patch Series\tVersion\tNumber of Patches\tStatus\tDays Old`), followed by a series-wise and patch-wise status breakdown (count of series/patches per status, e.g. "Merged in u-boot master: 17", "Reviewed - changes requested: 68 patches"). It writes the structured JSON, and writes a markdown version to `./upstream-status-report.md` (i.e. in the current working directory the skill was invoked from — not `/tmp`) containing the same table plus both summary breakdowns, so the user has a persistent, shareable file. If a report already exists at that path from a prior run, it is overwritten. Present the printed table AND both summaries to the user, sorted by whatever order best answers their question (chronological is a reasonable default, matching the script's output order — earliest-submitted series first), and mention the markdown file's path.

## Guardrails

- Never push, email, or otherwise mutate any remote — every operation here is `clone`/`fetch`/`pull`/read-only local git plumbing plus read-only GETs against the patchwork API.
- Never invent a merge/review status not backed by an actual patch-id match or an actual patchwork `state` value — do not guess a status patchwork doesn't report.
- If `fetch_patchwork_series.py`'s resend-merging ever looks wrong for a specific series (over- or under-merged), don't hand-patch the report — fix the union-find signal set in the script itself, since the same bug will recur on the next run.
