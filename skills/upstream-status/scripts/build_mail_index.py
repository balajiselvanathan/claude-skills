#!/usr/bin/env python3
"""One-pass scan of every mail body blob (path 'm') in a public-inbox git
mirror, building an index of Message-Id -> lore mirror commit sha plus
References/In-Reply-To and tag-trailer data. Used by
fetch_patchwork_series.py to resolve a patchwork patch's Message-Id into
the sha needed for git patch-id branch matching (match_patch_ids.py) -
patchwork doesn't carry a lore mirror commit sha itself - and by
check_review_replies.py to cross-check patchwork's `state` field against
actual reply activity in the mailing list archive (patchwork's state is
maintainer-curated and sometimes never gets flipped even when a reviewer
has already replied with comments).

Output JSON: { message_id: { sha, refs: [msgids...], tags: [...] } }
"""
import argparse
import json
import re
import subprocess
import tempfile

MSGID_HDR_RE = re.compile(r"^Message-I[Dd]:\s*<([^>]+)>", re.IGNORECASE)
REFS_HDR_RE = re.compile(r"^(?:References|In-Reply-To):\s*(.+)$", re.IGNORECASE)
REF_MSGID_RE = re.compile(r"<([^>]+)>")
TAG_RE = re.compile(
    r"^(Reviewed-by|Acked-by|Tested-by|Reviewed-and-tested-by):\s*(.+)$"
)


def iter_blobs(repo):
    rev_list = subprocess.Popen(
        ["git", "rev-list", "--all", "--objects"], cwd=repo, stdout=subprocess.PIPE, text=True
    )
    blob_shas = []
    for line in rev_list.stdout:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "m":
            blob_shas.append(parts[0])
    rev_list.wait()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".shas", delete=False) as tf:
        tf.write("\n".join(blob_shas) + "\n")
        shas_path = tf.name

    try:
        with open(shas_path, "rb") as stdin_f:
            batch = subprocess.Popen(
                ["git", "cat-file", "--batch"],
                cwd=repo,
                stdin=stdin_f,
                stdout=subprocess.PIPE,
            )
            out = batch.stdout
            for _ in blob_shas:
                header = out.readline()
                if not header:
                    break
                parts = header.split()
                if len(parts) != 3:
                    continue
                sha, _type, size = parts
                sha = sha.decode()
                size = int(size)
                content = out.read(size).decode("utf-8", errors="replace")
                out.read(1)  # trailing newline after blob content
                yield sha, content
            batch.wait()
    finally:
        import os

        os.unlink(shas_path)


def parse_headers(content):
    content = content.replace("\r\n", "\n")
    lines = content.split("\n")
    header_lines = []
    for line in lines:
        if line == "":
            break
        header_lines.append(line)
    # unfold headers (continuation lines start with whitespace)
    unfolded = []
    for line in header_lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += " " + line.strip()
        else:
            unfolded.append(line)

    msgid = None
    refs = []
    for line in unfolded:
        m = MSGID_HDR_RE.match(line)
        if m:
            msgid = m.group(1)
            continue
        m = REFS_HDR_RE.match(line)
        if m:
            refs.extend(REF_MSGID_RE.findall(m.group(1)))
            continue

    body = "\n".join(lines[len(header_lines):])
    tags = []
    for line in body.split("\n"):
        tm = TAG_RE.match(line.strip())
        if tm:
            tags.append(f"{tm.group(1)}: {tm.group(2).strip()}")

    return msgid, refs, tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    index = {}
    for sha, content in iter_blobs(args.repo):
        msgid, refs, tags = parse_headers(content)
        if not msgid:
            continue
        index[msgid] = {"sha": sha, "refs": refs, "tags": tags}

    with open(args.out, "w") as f:
        json.dump(index, f)
    print(args.out)


if __name__ == "__main__":
    main()
