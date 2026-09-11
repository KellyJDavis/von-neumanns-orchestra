"""M3.3 -- `template.py` held to `transformers`' own output.

This module reimplements the rendering half of `apply_chat_template`, so the only thing that makes
that safe is being byte-identical to the reference. Everything here compares against
`data/chat_templates.json`, recorded from a real `transformers` by `record_templates.py` -- not
against hand-written expectations, which would only encode what this module's author believed.

Three known ways to get it wrong are covered by name below, and all three were found by measuring
rather than by reading the reference: block whitespace, special tokens on encode, and the fact that
`transformers` rewrites the tokenizer file as it loads it. Every one of them is silent, and every
one is invisible in Qwen3 and obvious in TinyLlama -- which is why both are vendored.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import jinja2
import pytest
from lean_agent_models.template import (
    ChatTemplate,
    TemplateError,
    load_chat_template,
    load_chat_tokenizer,
    tokenizer_digest,
)

DATA = Path(__file__).parent / "data"
REFERENCE_PATH = DATA / "chat_templates.json"


@pytest.fixture(scope="module")
def reference() -> dict[str, Any]:
    return json.loads(REFERENCE_PATH.read_text())


def _model(reference: dict[str, Any], repo_id: str) -> dict[str, Any]:
    for model in reference["models"]:
        if model["repo_id"] == repo_id:
            return model
    raise KeyError(repo_id)


def _cases(reference: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{model['repo_id']}::{case['conversation']}::gen={case['add_generation_prompt']}", case)
        for model in reference["models"]
        for case in model["cases"]
    ]


def _directory(reference: dict[str, Any], repo_id: str) -> Path:
    return DATA / _model(reference, repo_id)["directory"]


# --------------------------------------------------------------------------------------------
# Conformance against the recorded reference.
# --------------------------------------------------------------------------------------------


def test_rendering_matches_transformers_byte_for_byte(reference: dict[str, Any]) -> None:
    """The whole justification for reimplementing this. Every recorded conversation, both with and
    without a generation prompt, across a trivial template and a realistic one."""
    checked = 0
    for model in reference["models"]:
        template = load_chat_template(DATA / model["directory"] / "tokenizer_config.json")
        for case in model["cases"]:
            rendered = template.render(
                case["messages"], add_generation_prompt=case["add_generation_prompt"]
            )
            assert rendered == case["rendered"], (
                f"{model['repo_id']} / {case['conversation']} / "
                f"add_generation_prompt={case['add_generation_prompt']}"
            )
            checked += 1
    assert checked == 24, "the reference should cover both models across every conversation"


def test_token_ids_match_transformers_byte_for_byte(reference: dict[str, Any]) -> None:
    """Render *and* encode, end to end, against ids the reference produced.

    Both models since M3.9. Qwen3's converted tokenizer is byte-identical to Goedel-Prover-V2-8B's
    -- the prover spec's Appendix B names -- so the Qwen3 half of this test is the real prover's
    prompt path being held to `transformers`, including a fenced-Lean prover prompt."""
    checked = 0
    for model in reference["models"]:
        assert model["has_converted_tokenizer"], model["repo_id"]
        chat = load_chat_tokenizer(DATA / model["directory"])
        for case in model["cases"]:
            ids = chat.to_token_ids(
                case["messages"], add_generation_prompt=case["add_generation_prompt"]
            )
            assert list(ids) == case["token_ids"], f"{model['repo_id']} / {case['conversation']}"
            checked += 1
    assert checked == 24


# --------------------------------------------------------------------------------------------
# The three bugs, pinned so they cannot come back.
# --------------------------------------------------------------------------------------------


def test_a_plain_jinja_environment_would_get_the_whitespace_wrong(
    reference: dict[str, Any],
) -> None:
    """Bug one, pinned by demonstrating it.

    The reference compiles with `trim_blocks=True, lstrip_blocks=True`. Without those, TinyLlama's
    template gains newlines around every block -- a prompt the model was never trained on, and
    nothing raises. This test renders the same template through a plain environment and asserts it
    differs from the reference, so if someone ever "simplifies" the environment construction, the
    failure explains itself.
    """
    model = _model(reference, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
    config = json.loads((DATA / model["directory"] / "tokenizer_config.json").read_text())
    case = model["cases"][0]

    naive = (
        jinja2.Environment(autoescape=False)
        .from_string(config["chat_template"])
        .render(
            messages=case["messages"],
            add_generation_prompt=case["add_generation_prompt"],
            eos_token=config["eos_token"],
            bos_token=config["bos_token"],
        )
    )
    assert naive != case["rendered"]
    assert naive.replace("\n", "") == case["rendered"].replace("\n", ""), (
        "the difference should be whitespace only -- if it is not, this test is now pinning "
        "something other than the trim_blocks/lstrip_blocks bug"
    )

    correct = load_chat_template(DATA / model["directory"] / "tokenizer_config.json").render(
        case["messages"], add_generation_prompt=case["add_generation_prompt"]
    )
    assert correct == case["rendered"]


def test_encoding_with_special_tokens_would_add_a_second_bos(reference: dict[str, Any]) -> None:
    """Bug two, pinned the same way.

    The template already emits whatever special tokens the model expects, so encoding with
    `add_special_tokens=True` gives TinyLlama a duplicate BOS. Qwen3's tokenizer adds none either
    way, which is exactly why testing against Qwen3 alone would not have caught this.
    """
    model = _model(reference, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
    chat = load_chat_tokenizer(DATA / model["directory"])
    case = model["cases"][0]
    rendered = case["rendered"]

    correct = chat.encode(rendered)
    with_specials = tuple(chat.tokenizer.encode(rendered, add_special_tokens=True).ids)

    assert list(correct) == case["token_ids"]
    assert len(with_specials) == len(correct) + 1
    assert with_specials[1:] == correct, "the extra token should be a prepended BOS"


# --------------------------------------------------------------------------------------------
# Behaviour this module adds on purpose.
# --------------------------------------------------------------------------------------------


def test_an_unpinned_date_is_refused_rather_than_silently_changing_daily() -> None:
    """A template calling `strftime_now` produces a prompt that differs every day, which defeats
    prefix caching (§6.6: bands 1-2 must be byte-stable "or prefix caching is defeated and cost
    multiplies silently") and makes the trajectory unreplayable. The reference just calls the
    clock; this module requires the caller to pin it."""
    template = ChatTemplate(
        template="Today is {{ strftime_now('%Y-%m-%d') }}.{{ messages[0]['content'] }}",
        special_tokens={},
    )
    messages = [{"role": "user", "content": " go"}]

    with pytest.raises(TemplateError, match="unreplayable"):
        template.render(messages)

    pinned = template.render(messages, now=datetime.datetime(2020, 5, 17, tzinfo=datetime.UTC))
    assert pinned == "Today is 2020-05-17. go"
    # Pinned means reproducible, which is the entire point.
    assert pinned == template.render(
        messages, now=datetime.datetime(2020, 5, 17, tzinfo=datetime.UTC)
    )


def test_tojson_does_not_html_escape() -> None:
    """Jinja's built-in `tojson` escapes `<`, `>` and `&`; the reference overrides it with plain
    `json.dumps(ensure_ascii=False)`. A template serializing tool schemas would otherwise render a
    prompt the model never saw in training."""
    template = ChatTemplate(template="{{ payload | tojson }}", special_tokens={})
    rendered = template.render([], payload={"expr": "a < b & c > d", "unicode": "∀"})
    assert "<" in rendered and ">" in rendered and "&" in rendered
    assert "\\u003c" not in rendered and "\\u2200" not in rendered
    assert "∀" in rendered


def test_raise_exception_surfaces_as_a_template_error() -> None:
    """Templates use `raise_exception` to reject unsupported message shapes. It has to arrive as
    this module's own error type, not as a bare Jinja exception the executor cannot classify."""
    template = ChatTemplate(
        template="{{ raise_exception('only user turns are supported') }}", special_tokens={}
    )
    with pytest.raises(TemplateError, match="only user turns are supported"):
        template.render([{"role": "system", "content": "x"}])


def test_a_generation_block_is_refused_rather_than_rendered_differently() -> None:
    """`{% generation %}` needs the reference's AssistantTracker extension to mark
    assistant-generated spans. Unimplemented here -- and a template using it must fail loudly
    rather than render a prompt that is subtly not the reference's."""
    template = ChatTemplate(template="a{% generation %}b{% endgeneration %}", special_tokens={})
    with pytest.raises(TemplateError, match="AssistantTracker"):
        template.render([])


def test_a_config_without_a_chat_template_is_an_error(tmp_path: Path) -> None:
    config = tmp_path / "tokenizer_config.json"
    config.write_text(json.dumps({"eos_token": "</s>"}))
    with pytest.raises(TemplateError, match="no string `chat_template`"):
        load_chat_template(config)


def test_special_tokens_are_read_in_both_shapes(tmp_path: Path) -> None:
    """`tokenizer_config.json` writes a special token either as a bare string or as an AddedToken
    object, and both appear in the wild. Reading only one shape would leave `eos_token` undefined,
    which Jinja renders as an empty string -- a truncated prompt with nothing raised."""
    config = tmp_path / "tokenizer_config.json"
    config.write_text(
        json.dumps(
            {
                "chat_template": "{{ bos_token }}|{{ eos_token }}",
                "bos_token": "<s>",
                "eos_token": {"content": "</s>", "lstrip": False, "rstrip": False},
            }
        )
    )
    template = load_chat_template(config)
    assert template.special_tokens == {"bos_token": "<s>", "eos_token": "</s>"}
    assert template.render([]) == "<s>|</s>"


def test_the_vendored_reference_says_where_it_came_from(reference: dict[str, Any]) -> None:
    assert reference["provenance"]["reference"].startswith("transformers ")
    assert "apply_chat_template" in reference["provenance"]["method"]
    assert reference["provenance"]["recorded_at"]


def test_the_vendored_tokenizer_is_the_converted_artifact_not_the_raw_file(
    reference: dict[str, Any],
) -> None:
    """Bug three, pinned without vendoring a second 1.8 MB file to demonstrate it.

    `transformers` rewrites a SentencePiece tokenizer when it loads one; upstream's
    `tokenizer.json` carries a `Prepend` normalizer and no pre-tokenizer, while the converted
    artifact has no normalizer and a `Metaspace(prepend_scheme="first")` pre-tokenizer. Only the
    second gives the reference's ids -- the first inserts a metaspace token after every special
    token, silently. Asserting the *shape* of what is vendored is what stops someone "fixing" this
    by dropping the raw upstream file in its place.
    """
    model = _model(reference, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
    state = json.loads((DATA / model["directory"] / "tokenizer.converted.json").read_text())

    assert state["normalizer"] is None, "a Prepend normalizer here means this is the raw file"
    pre = state["pre_tokenizer"]
    assert pre["type"] == "Metaspace"
    assert pre["prepend_scheme"] == "first"


def test_a_directory_without_a_converted_tokenizer_is_refused(tmp_path: Path) -> None:
    """Pointing this at a raw upstream download is not a supported shortcut, so it fails with an
    explanation rather than quietly producing different ids."""
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({"chat_template": "{{ x }}"}))
    (tmp_path / "tokenizer.json").write_text("{}")
    with pytest.raises(TemplateError, match="not upstream's `tokenizer.json`"):
        load_chat_tokenizer(tmp_path)


def test_a_pinned_tokenizer_digest_is_verified(reference: dict[str, Any]) -> None:
    """M3.4 -- what turns "a converted file is present" into an actual pin.

    Requiring `tokenizer.converted.json` only requires that *some* converted tokenizer is sitting
    in the directory. Swap in a different model's and every prompt tokenizes differently, with
    nothing raised -- which is the failure mode this whole module exists to prevent. Every other
    pinned artifact in this repo is digest-checked (the miniF2F corpus, the Phase 2 baseline); this
    one is too, and a deployment supplies the expected digest from
    `[models.<role>].tokenizer_revision`.
    """
    directory = DATA / _model(reference, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")["directory"]
    digest = tokenizer_digest(directory)
    assert len(digest) == 64

    # The right digest loads.
    chat = load_chat_tokenizer(directory, expect_sha256=digest)
    assert chat.revision == digest

    # A wrong one refuses, and says both values so the mismatch is diagnosable.
    with pytest.raises(TemplateError, match="expected " + "0" * 64):
        load_chat_tokenizer(directory, expect_sha256="0" * 64)


def test_the_digest_is_of_the_converted_artifact_not_an_upstream_revision(
    reference: dict[str, Any],
) -> None:
    """Two upstream revisions can convert to the same tokenizer, and one upstream revision can
    convert differently under a different `transformers`. What decides the token ids is this file's
    bytes, so this file's bytes are what is pinned."""
    directory = DATA / _model(reference, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")["directory"]
    import hashlib

    expected = hashlib.sha256((directory / "tokenizer.converted.json").read_bytes()).hexdigest()
    assert tokenizer_digest(directory) == expected


def test_an_explicit_revision_wins_over_the_computed_digest(reference: dict[str, Any]) -> None:
    """A deployment that already names its tokenizer by an upstream revision should be able to
    record that on the trajectory instead; the digest is the default, not a straitjacket."""
    directory = DATA / _model(reference, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")["directory"]
    chat = load_chat_tokenizer(directory, revision="upstream-abc123")
    assert chat.revision == "upstream-abc123"
