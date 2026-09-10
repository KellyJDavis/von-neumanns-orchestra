"""Rebuild the vendored miniF2F corpus from upstream.

Run manually, never from a test or from CI:

    uv run python -m lean_agent_eval.suites.vendor_minif2f

The corpus is **vendored, not fetched**, for three reasons that all point the same way.

1. Spec §7.5 asks that the benchmark's own sealed goal bundle be held read-only "so proving a
   weakened restatement is structurally impossible rather than something to detect". A checked-in,
   digest-pinned file is that property; a download resolved at test time is the opposite, since
   whatever upstream serves that day becomes the benchmark.
2. Spec's Phase 2 exit criterion is a suite that "runs on every PR forever". A network fetch on
   every PR makes the gate as reliable as GitHub's raw-content CDN, and turns an upstream force-push
   into a silent change in what is being measured.
3. Phase 1 gate 1 is *still blocked* because the Lean Workbook corpus targets a toolchain this
   repo does not pin. Recording the upstream toolchain alongside the statements is what makes that
   kind of mismatch visible here rather than mysterious.

Upstream is MIT-licensed (Copyright (c) Meta Platforms, Inc. and affiliates); `LICENSE.miniF2F`
beside the generated corpus carries the notice the licence requires, and `provenance` in the file
itself records the exact commit these statements came from.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

REPO = "yangky11/miniF2F-lean4"

#: Pinned by commit, never by branch: `main` is a moving target, and a benchmark that moves is not
#: a benchmark. Re-pin deliberately (and re-run the suite) rather than tracking upstream silently.
COMMIT = "5746b7d6c47855ce1294bed87329618ff7f1bc31"

DATA_DIR = Path(__file__).parent / "data"
CORPUS_PATH = DATA_DIR / "minif2f.json"
LICENSE_PATH = DATA_DIR / "LICENSE.miniF2F"

#: Every one of the 488 upstream files has exactly this header -- verified, not assumed, by
#: `_parse` rejecting anything that does not match. Hoisting it out of the per-problem records
#: keeps the corpus honest about what is shared and what is the problem's own text.
EXPECTED_IMPORT = "import Mathlib"
EXPECTED_OPENS = "open BigOperators Real Nat Topology Rat"
EXPECTED_SET_OPTION = "set_option maxHeartbeats 0"

_THEOREM = re.compile(r"^theorem\s", re.MULTILINE)


class VendorError(RuntimeError):
    """Upstream no longer has the shape this vendorer verified. Never silently worked around --
    a corpus assembled from files that did not parse as expected is not the benchmark."""


def _parse(name: str, split: str, text: str) -> dict[str, str]:
    """One upstream `.lean` file into one corpus record, checking every assumption it rests on."""
    for expected in (EXPECTED_IMPORT, EXPECTED_OPENS, EXPECTED_SET_OPTION):
        if expected not in text:
            raise VendorError(f"{split}/{name}: expected header line {expected!r} is missing")

    matches = _THEOREM.findall(text)
    if len(matches) != 1:
        raise VendorError(f"{split}/{name}: expected exactly one theorem, found {len(matches)}")

    body = text[_THEOREM.search(text).start() :].rstrip()  # type: ignore[union-attr]
    if not body.endswith("sorry"):
        raise VendorError(f"{split}/{name}: expected the theorem to end in `sorry`")

    # The statement keeps its `:= by` and drops only the `sorry`, so a consumer splices a proof
    # exactly where upstream's own placeholder was rather than reconstructing the syntax.
    return {
        "id": name,
        "split": split,
        "statement": body[: -len("sorry")].rstrip(),
        "statement_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


def build(commit: str = COMMIT) -> dict[str, Any]:
    url = f"https://github.com/{REPO}/archive/{commit}.tar.gz"
    with urllib.request.urlopen(url, timeout=300) as response:
        raw = response.read()

    problems: list[dict[str, str]] = []
    toolchain = ""
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in sorted(archive.getmembers(), key=lambda m: m.name):
            parts = Path(member.name).parts
            if len(parts) == 2 and parts[1] == "lean-toolchain":
                handle = archive.extractfile(member)
                toolchain = handle.read().decode().strip() if handle else ""
            if not member.isfile() or not member.name.endswith(".lean"):
                continue
            if len(parts) != 4 or parts[1] != "MiniF2F" or parts[2] not in ("Test", "Valid"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            problems.append(_parse(Path(parts[3]).stem, parts[2].lower(), handle.read().decode()))

    if len(problems) != 488:
        raise VendorError(f"expected miniF2F's 488 problems, parsed {len(problems)}")

    return {
        "name": "miniF2F",
        "provenance": {
            "repo": f"https://github.com/{REPO}",
            "commit": commit,
            "license": "MIT",
            "copyright": "Copyright (c) Meta Platforms, Inc. and affiliates.",
            # Recorded because it is the difference between "this statement is wrong" and "this
            # statement was written for a different Mathlib" -- the exact ambiguity that has had
            # Phase 1 gate 1 blocked since planning.
            "upstream_toolchain": toolchain,
        },
        "header": {
            "imports": ["Mathlib"],
            "opens": EXPECTED_OPENS,
        },
        "problems": problems,
    }


def corpus_digest(corpus: dict[str, Any]) -> str:
    """Digest of the statements alone, deliberately excluding provenance and header.

    What must not drift silently is the set of things being proved. Re-pinning to a new upstream
    commit that changes no statement should not read as a changed benchmark, and a commit that
    edits one statement must -- so the digest covers exactly the problems.
    """
    payload = json.dumps(corpus["problems"], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default=COMMIT, help="upstream commit to vendor")
    args = parser.parse_args()

    corpus = build(args.commit)
    corpus["corpus_sha256"] = corpus_digest(corpus)
    DATA_DIR.mkdir(exist_ok=True)
    CORPUS_PATH.write_text(json.dumps(corpus, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {CORPUS_PATH} ({len(corpus['problems'])} problems)")
    print(f"corpus_sha256 = {corpus['corpus_sha256']}")
    print(f"upstream toolchain = {corpus['provenance']['upstream_toolchain']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
