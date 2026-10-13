#!/usr/bin/env python3
"""Compute git patch-id for a list of lore-mirror blob SHAs (one per
patch, from build_mail_index.py), and check each against the patch-id set
of commits reachable from given branches in one or more destination
repos. Reports, per source patch, which destination branch(es) (if any)
contain a matching patch-id.
"""
import argparse
import json
import subprocess


def patch_ids_for_range(repo, rev_range):
    """rev_range e.g. 'origin/main' meaning all commits reachable from it."""
    log = subprocess.run(
        ["git", "log", rev_range, "--format=%H"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    ids = {}
    diff_tree = subprocess.Popen(
        ["git", "diff-tree", "-p", "--stdin"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        errors="replace",
    )

    patch_id_proc = subprocess.Popen(
        ["git", "patch-id", "--stable"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        errors="replace",
    )

    import threading

    def feed():
        for sha in log:
            diff_tree.stdin.write(sha + "\n")
        diff_tree.stdin.close()

    def relay():
        for line in diff_tree.stdout:
            patch_id_proc.stdin.write(line)
        patch_id_proc.stdin.close()

    t1 = threading.Thread(target=feed)
    t2 = threading.Thread(target=relay)
    t1.start()
    t2.start()

    for line in patch_id_proc.stdout:
        parts = line.split()
        if len(parts) == 2:
            pid, sha = parts
            ids[pid] = sha

    t1.join()
    t2.join()
    diff_tree.wait()
    patch_id_proc.wait()
    return ids


def patch_id_for_commit(lore_repo, blob_sha):
    """The lore mirror stores each email verbatim as a blob at path 'm'
    (not a real code commit) - the actual unified diff is embedded in the
    email body. build_mail_index.py indexes the blob sha directly (not a
    commit sha), so read it with `git cat-file -p` rather than `git show
    <commit>:m`. git patch-id tolerates leading email/header text and
    finds the diff itself, so no manual diff extraction is needed."""
    email = subprocess.run(
        ["git", "cat-file", "-p", blob_sha], cwd=lore_repo, capture_output=True,
        text=True, errors="replace", check=True,
    ).stdout
    out = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=email,
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    ).stdout
    parts = out.split()
    return parts[0] if parts else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-repo", required=True)
    ap.add_argument("--series", required=True, help="series JSON from list_my_series.py")
    ap.add_argument(
        "--dest",
        nargs=2,
        action="append",
        metavar=("LABEL", "REPO:REV"),
        required=True,
        help="e.g. --dest master /path/to/mainline:origin/master",
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.series) as f:
        series_list = json.load(f)

    dest_index = {}
    for label, repo_rev in args.dest:
        repo, rev = repo_rev.split(":", 1)
        print(f"Indexing patch-ids for {label} ({repo}:{rev})...")
        dest_index[label] = patch_ids_for_range(repo, rev)

    results = {}
    for s in series_list:
        key = s["series_title"] + "|" + s["latest_version"]
        patch_results = []
        for p in s["patches"]:
            pid = patch_id_for_commit(args.src_repo, p["sha"]) if p.get("sha") else None
            match = {}
            for label, idmap in dest_index.items():
                if pid and pid in idmap:
                    match[label] = idmap[pid]
            patch_results.append(
                {"subject": p["subject"], "patch_id": pid, "matches": match}
            )
        results[key] = patch_results

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(args.out)


if __name__ == "__main__":
    main()
