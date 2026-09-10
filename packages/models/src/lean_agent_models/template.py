"""Client-side chat templating to token ids (spec §6.5).

> Render the chat template client-side with `AutoTokenizer.apply_chat_template(tokenize=True)`
> against a tokenizer pinned by revision, and send the id list. Sending a string is not enough:
> `tokenize=False` output does not re-tokenize to the identity around special tokens and
> whitespace. Server-side templating loses the exact token sequence entirely, which breaks replay
> and on-policy RL.

This module does that without depending on `transformers`, which is a deliberate choice: the
reference implementation is a very large dependency whose relevant part is a Jinja environment and
a call into `tokenizers` -- and `tokenizers` is exactly what `transformers`' fast tokenizers use
underneath (`AutoTokenizer(...).backend_tokenizer` is a `tokenizers.Tokenizer`). So the tokenizing
half is the same library either way, and only the rendering half is reimplemented.

**Reimplementing it means the output has to be proven identical, not assumed.** It is not
obviously identical, and *three* ways of getting it wrong were found here by measuring rather than
by reading -- each of them silent, and each producing a prompt or an id sequence the model was
never trained on:

1. **Whitespace.** The reference compiles templates with `trim_blocks=True, lstrip_blocks=True`. A
   plain `jinja2.Environment` renders TinyLlama's template with extra newlines around every block
   -- a prompt the model was never trained on, and nothing errors. Qwen3's template hides this
   completely, because it uses explicit `{%-` markers throughout, so testing against Qwen3 alone
   would have shipped the bug.
2. **Special tokens.** `apply_chat_template(tokenize=True)` encodes with
   `add_special_tokens=False`, because the template already emits whatever special tokens the model
   expects. Encoding with `True` gives TinyLlama a second BOS. Qwen3 again shows nothing, since its
   tokenizer adds none either way.
3. **The tokenizer file itself is not the tokenizer.** `transformers` *rewrites* a
   SentencePiece/Llama-family tokenizer as it loads it -- for TinyLlama, replacing the file's
   `Prepend("_")` normalizer with a `Metaspace(prepend_scheme="first")` pre-tokenizer. Feed the raw
   `tokenizer.json` to `tokenizers` and every special token in the middle of a prompt is followed
   by a spurious metaspace id. `load_chat_tokenizer` therefore requires the *converted* artifact;
   see its docstring.

`tests/models/test_template.py` holds this to recorded `transformers` output for both models, and
`record_templates.py` regenerates that reference.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
import jinja2.ext
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer

from lean_agent_models.errors import ModelBackendError

#: Special-token names the reference makes available to a template. Anything a template reaches for
#: that is absent renders as an empty string in Jinja rather than erroring, which is precisely how a
#: silently wrong prompt happens -- so `render` requires every name the template actually uses.
SPECIAL_TOKEN_KEYS = (
    "bos_token",
    "eos_token",
    "unk_token",
    "sep_token",
    "pad_token",
    "cls_token",
    "mask_token",
)


class TemplateError(ModelBackendError):
    """The template could not be rendered into a prompt this system is willing to send.

    A subclass of `ModelBackendError` because from the control loop's point of view it is the same
    kind of event as a backend refusing a request: no completion, and not evidence about the
    obligation.
    """


def _tojson(value: Any, **kwargs: Any) -> str:
    """The reference's `tojson`, not Jinja's.

    Jinja's built-in escapes HTML characters; `transformers` overrides it with a plain
    `json.dumps(..., ensure_ascii=False)`. A template that serializes tool schemas would otherwise
    render `<`, `>` and `&` as escapes and produce a prompt the model never saw in training.
    """
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(value, **kwargs)


def _raise_exception(message: str) -> Any:
    raise TemplateError(f"chat template raised: {message}")


@dataclass(frozen=True)
class ChatTemplate:
    """One model's chat template plus the special tokens it may reference."""

    template: str
    special_tokens: Mapping[str, str]

    def render(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        add_generation_prompt: bool = True,
        now: datetime.datetime | None = None,
        **extra: Any,
    ) -> str:
        """The prompt text, byte-identical to `apply_chat_template(tokenize=False)`.

        `now` exists because some templates call `strftime_now` to put today's date in the system
        prompt. Left to the clock, that makes the rendered prompt change daily -- which silently
        defeats prefix caching (spec §6.6: bands 1-2 must be byte-stable across resamples "or
        prefix caching is defeated and cost multiplies silently") and makes a trajectory
        unreplayable. So a template that wants the date must be given one explicitly, and a caller
        that supplies none gets an error rather than a prompt that will not reproduce tomorrow.
        """
        if "{% generation %}" in self.template or "{%- generation %}" in self.template:
            raise TemplateError(
                "template uses `{% generation %}`, which needs the reference's AssistantTracker "
                "extension to mark assistant-generated spans. Not implemented here; a template "
                "needing it must not be rendered by this module."
            )

        def strftime_now(fmt: str) -> str:
            if now is None:
                raise TemplateError(
                    "template called `strftime_now`, so its output depends on the current date. "
                    "Pass an explicit `now=` to pin it -- an unpinned date changes the prompt "
                    "daily, defeating prefix caching and making the trajectory unreplayable."
                )
            return now.strftime(fmt)

        environment = ImmutableSandboxedEnvironment(
            trim_blocks=True,
            lstrip_blocks=True,
            extensions=[jinja2.ext.loopcontrols],
        )
        environment.filters["tojson"] = _tojson
        environment.globals["raise_exception"] = _raise_exception
        environment.globals["strftime_now"] = strftime_now

        try:
            compiled = environment.from_string(self.template)
            return compiled.render(
                messages=[dict(message) for message in messages],
                add_generation_prompt=add_generation_prompt,
                **dict(self.special_tokens),
                **extra,
            )
        except TemplateError:
            raise
        except jinja2.TemplateError as exc:
            raise TemplateError(f"chat template failed to render: {exc}") from exc


@dataclass(frozen=True)
class ChatTokenizer:
    """A chat template and the tokenizer it renders for, pinned together.

    Together, not separately, because the pair is what has to be reproducible: spec §7.3 records
    `tokenizer_revision` on the trajectory precisely so a published result names the exact
    tokenizer its token ids came from, and a template rendered for one tokenizer and encoded with
    another produces ids nobody can replay.
    """

    template: ChatTemplate
    tokenizer: Tokenizer
    revision: str | None = None

    def encode(self, text: str) -> tuple[int, ...]:
        """Token ids for already-rendered text.

        `add_special_tokens=False`, which is what the reference does and is not a detail: the
        template has already emitted whatever special tokens the model expects, so letting the
        tokenizer add its own gives TinyLlama a second BOS. Measured, not assumed.
        """
        return tuple(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def to_token_ids(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        add_generation_prompt: bool = True,
        now: datetime.datetime | None = None,
        **extra: Any,
    ) -> tuple[int, ...]:
        """Messages straight to the id list `CompletionRequest.prompt_token_ids` wants."""
        return self.encode(
            self.template.render(
                messages, add_generation_prompt=add_generation_prompt, now=now, **extra
            )
        )


def _special_token(value: Any) -> str | None:
    """`tokenizer_config.json` writes a special token either as a bare string or as an
    `AddedToken` object; both appear in the wild, so both are read."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return str(value["content"])
    return None


def load_chat_template(tokenizer_config_path: Path) -> ChatTemplate:
    """Read a `tokenizer_config.json`'s `chat_template` and special tokens."""
    config = json.loads(tokenizer_config_path.read_text())
    template = config.get("chat_template")
    if not isinstance(template, str):
        raise TemplateError(
            f"{tokenizer_config_path} has no string `chat_template`; this model cannot be "
            "prompted as a chat model"
        )
    special = {
        key: token
        for key in SPECIAL_TOKEN_KEYS
        if (token := _special_token(config.get(key))) is not None
    }
    return ChatTemplate(template=template, special_tokens=special)


#: The tokenizer file this module loads, and it is deliberately **not** upstream's
#: `tokenizer.json`. See `load_chat_tokenizer` for why.
CONVERTED_TOKENIZER_NAME = "tokenizer.converted.json"


def load_chat_tokenizer(directory: Path, *, revision: str | None = None) -> ChatTokenizer:
    """Load a chat template and its *converted* tokenizer from one directory.

    **`transformers` rewrites a tokenizer when it loads one, and the rewrite changes token ids.**
    For TinyLlama it replaces the file's `Prepend("_")` normalizer with a
    `Metaspace(prepend_scheme="first")` pre-tokenizer. Handing the raw `tokenizer.json` to
    `tokenizers` instead inserts a spurious metaspace token after *every* special token, so
    `<|user|>...</s>\n<|assistant|>` gains an id the model was never trained to see there --
    measured, not guessed, and nothing raises when it happens.

    So this loads `tokenizer.converted.json`: the artifact `transformers` actually produces
    (`AutoTokenizer.from_pretrained(...).backend_tokenizer.to_str()`), generated once by
    `tests/models/record_templates.py` and pinned. That is the same choice M3.2 made for vLLM
    responses -- record what the reference produced rather than reimplement how it produced it --
    and it is better than calling `transformers` at runtime for reproducibility as well: a pinned
    artifact cannot change under a dependency upgrade, and §7.3 wants a published result to name
    the exact tokenizer its ids came from.

    The consequence for a deployment is real and worth stating plainly: **a new model needs its
    tokenizer converted once, by something that has `transformers`, before this system can prompt
    it.** Pointing this at a raw upstream `tokenizer.json` is not a supported shortcut, which is
    why the file name differs rather than being an optional override.
    """
    converted = directory / CONVERTED_TOKENIZER_NAME
    if not converted.exists():
        raise TemplateError(
            f"{converted} not found. This must be the tokenizer as `transformers` configures it, "
            "not upstream's `tokenizer.json` -- loading the raw file yields different token ids "
            "for SentencePiece models. Generate it with tests/models/record_templates.py."
        )
    return ChatTokenizer(
        template=load_chat_template(directory / "tokenizer_config.json"),
        tokenizer=Tokenizer.from_str(converted.read_text()),
        revision=revision,
    )
