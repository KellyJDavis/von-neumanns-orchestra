"""Vendor tokenizer files and record `transformers`' own chat-template output as the reference.

Run manually, in an environment that has `transformers` (this repo does not depend on it):

    source ~/.venv-vllm-metal/bin/activate
    uv run --with transformers python tests/models/record_templates.py

`template.py` reimplements the rendering half of `apply_chat_template`, so the only thing that
makes that safe is being held to the reference's actual output. Recording it here means CI checks
that byte-for-byte without installing a multi-gigabyte dependency, in the same spirit as M3.2's
recorded vLLM responses.

**Two models, each earning its place by exposing a bug the other hides.**

- *TinyLlama* has a trivial 410-character template and a 1.8 MB tokenizer, and it is the one that
  catches every known way to get this wrong: the reference's `trim_blocks`/`lstrip_blocks` settings
  (a plain Jinja environment renders it with extra newlines), `add_special_tokens=False` (with
  `True` it acquires a second BOS), and the metaspace conversion below. Small enough to vendor
  whole, so the render *and* encode halves are both checkable offline.
- *Qwen3* has a 4 KB template using `namespace()`, `tojson`, `.split()`, `loop.*` and eighteen
  conditionals -- a realistic one, close in shape to the prover models this system will actually
  run. Only its `tokenizer_config.json` is vendored (9.5 KB); its `tokenizer.json` is 11 MB and
  would buy nothing the TinyLlama one does not, since the encode path is identical.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "data"

#: `(repo id, vendor tokenizer.json too?)`. See the module docstring for why only one gets weights.
MODELS: list[tuple[str, bool]] = [
    ("TinyLlama/TinyLlama-1.1B-Chat-v1.0", True),
    ("Qwen/Qwen3-0.6B", False),
]

#: Message shapes worth pinning: a bare user turn, a system prompt, a multi-turn exchange, and
#: content carrying the characters Jinja's *built-in* `tojson` would HTML-escape (the reference
#: overrides that filter, so a renderer using the built-in would differ here and nowhere else).
CONVERSATIONS: dict[str, list[dict[str, str]]] = {
    "single_user": [{"role": "user", "content": "Prove that 2 + 2 = 4 in Lean 4."}],
    "system_and_user": [
        {"role": "system", "content": "You are a Lean 4 prover. Answer with a tactic block."},
        {"role": "user", "content": "Prove 2 + 2 = 4."},
    ],
    "multi_turn": [
        {"role": "user", "content": "Prove n + 0 = n."},
        {"role": "assistant", "content": "by simp"},
        {"role": "user", "content": "Now prove n + m = m + n."},
    ],
    "html_ish_content": [
        {"role": "user", "content": 'Show a < b & c > d, and the string "x" <tag>.'}
    ],
    "unicode_content": [
        {"role": "user", "content": "Prove ∀ n : ℕ, n + 0 = n — with ← rewriting."}
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DATA / "chat_templates.json")
    args = parser.parse_args()

    import transformers
    from huggingface_hub import snapshot_download
    from transformers import AutoTokenizer

    records: list[dict[str, Any]] = []
    for repo_id, vendor_weights in MODELS:
        slug = repo_id.split("/")[-1]
        target = DATA / "tokenizers" / slug
        target.mkdir(parents=True, exist_ok=True)

        source = Path(snapshot_download(repo_id, allow_patterns=["tokenizer*.json"]))
        shutil.copy(source / "tokenizer_config.json", target / "tokenizer_config.json")

        tokenizer = AutoTokenizer.from_pretrained(repo_id)
        if vendor_weights:
            # The *converted* tokenizer, not upstream's `tokenizer.json`. `transformers` rewrites
            # SentencePiece/Llama-family tokenizers when it loads them -- for TinyLlama it drops
            # the `Prepend("_")` normalizer and installs a `Metaspace(prepend_scheme="first")`
            # pre-tokenizer -- and the difference is not cosmetic: loading the raw file with
            # `tokenizers` inserts a spurious metaspace token after every special token, so
            # `<|user|>...</s>\n<|assistant|>` gains an id the model never saw there.
            #
            # Recording the converted artifact rather than reimplementing the conversion is the
            # same choice M3.2 made for vLLM responses: pin what the reference actually produced.
            # It also removes a reproducibility hazard, since a `transformers` upgrade can change
            # the conversion, and a pinned artifact cannot.
            (target / "tokenizer.converted.json").write_text(tokenizer.backend_tokenizer.to_str())
        cases: list[dict[str, Any]] = []
        for name, messages in CONVERSATIONS.items():
            for add_generation_prompt in (True, False):
                rendered = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=add_generation_prompt
                )
                case: dict[str, Any] = {
                    "conversation": name,
                    "add_generation_prompt": add_generation_prompt,
                    "messages": messages,
                    "rendered": rendered,
                }
                if vendor_weights:
                    encoded = tokenizer.apply_chat_template(
                        messages, tokenize=True, add_generation_prompt=add_generation_prompt
                    )
                    ids = encoded["input_ids"]
                    case["token_ids"] = (
                        list(ids[0]) if ids and isinstance(ids[0], list) else list(ids)
                    )
                cases.append(case)

        records.append(
            {
                "repo_id": repo_id,
                "directory": f"tokenizers/{slug}",
                "has_converted_tokenizer": vendor_weights,
                "cases": cases,
            }
        )
        print(f"  {repo_id}: {len(cases)} cases, weights={'yes' if vendor_weights else 'no'}")

    document = {
        "provenance": {
            "reference": f"transformers {transformers.__version__}",
            "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "method": "AutoTokenizer.apply_chat_template(tokenize=False | True)",
            "note": (
                "Reference output from the real implementation. `template.py` reimplements the "
                "rendering half and is held to these bytes; nothing here is hand-written."
            ),
        },
        "models": records,
    }
    args.out.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
