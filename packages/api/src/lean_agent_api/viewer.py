"""The trajectory viewer's page (spec §7.4, M3.11): one attempt as HTML, read-only.

Spec §9 names this the one web UI in scope ("A web UI beyond the trajectory viewer" is deferred),
so it is deliberately plain: server-rendered, no script, no external asset, one page per attempt.
What it must get right is fidelity, not polish:

* **Everything the model saw or wrote is escaped and shown verbatim** in `<pre>`, whitespace and
  special tokens included. A prompt is model *input*, and model output is untrusted text (spec
  §7.2 treats AI-generated proofs as malicious code), so neither is ever interpreted as markup.
* **Steps are shown in the order they happened**, each request next to the exact prompt and
  samples it stands for, each submission next to the development it submitted and every
  diagnostic it drew -- the view a person needs to see why a repair turn said what it said.
* **Nothing is summarized away.** Long text is collapsed behind `<details>`, never truncated: the
  page is a debugging tool, and the hidden part is the part someone will need.
"""

from __future__ import annotations

from html import escape

from lean_agent_api.schemas import ExchangeBody, StepBody, TrajectoryResponse

_STYLE = """
:root { --fg: #1d1d1f; --bg: #fbfbf8; --muted: #6b6b6b; --line: #ddd; --ok: #1a7f37;
        --bad: #b42318; --panel: #f2f2ee; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e8e8e3; --bg: #16171a; --muted: #9a9a9a; --line: #333; --ok: #4ac26b;
          --bad: #f97066; --panel: #1f2125; }
}
body { font: 14px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 72rem; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.25rem; } h2 { font-size: 1.05rem; margin-top: 2rem; }
table { border-collapse: collapse; } td, th { padding: .15rem .8rem .15rem 0; text-align: left;
       vertical-align: top; } th { color: var(--muted); font-weight: 500; }
pre { background: var(--panel); padding: .6rem .8rem; overflow-x: auto; white-space: pre-wrap;
      word-break: break-word; border-radius: 4px; }
.step { border-left: 3px solid var(--line); padding: .2rem 0 .2rem .8rem; margin: .8rem 0; }
.ok { color: var(--ok); } .bad { color: var(--bad); } .muted { color: var(--muted); }
summary { cursor: pointer; }
"""


def _pre(value: str | None) -> str:
    return f"<pre>{escape(value or '')}</pre>"


def _details(summary: str, body: str, *, open_: bool = False) -> str:
    return f"<details{' open' if open_ else ''}><summary>{summary}</summary>{body}</details>"


def _exchange(exchange: ExchangeBody) -> str:
    prompt = exchange.prompt
    if prompt.text is not None:
        prompt_body = _pre(prompt.text)
        how = f"decoded from its token ids with tokenizer {escape(prompt.decoded_with or '')}"
    else:
        prompt_body = f"<p class='muted'>{escape(prompt.note or '')}</p>" + _pre(
            " ".join(str(i) for i in prompt.token_ids or [])
        )
        how = "token ids only"
    parts = [
        _details(f"Prompt &mdash; {prompt.token_count} tokens, {how}", prompt_body),
        f"<p class='muted'>sampling {escape(str(exchange.sampling))}, seed {exchange.seed}</p>",
    ]
    for completion in exchange.completions:
        text = (
            completion.text
            if completion.text is not None
            else " ".join(str(i) for i in completion.token_ids or [])
        )
        parts.append(
            _details(
                f"Sample {completion.index} &mdash; {completion.token_count} tokens, "
                f"finish {escape(completion.finish_reason or 'not recorded')}, "
                f"log-probability {completion.logprob_sum:.2f}",
                _pre(text),
            )
        )
    return "".join(parts)


def _step(index: int, step: StepBody, exchanges: list[ExchangeBody]) -> str:
    mark = "<span class='ok'>ok</span>" if step.ok else "<span class='bad'>failed</span>"
    head = f"<b>{index}. {escape(step.label)}</b> &middot; {escape(step.action)} &middot; {mark}"
    body = [f"<div>{head}</div>"]
    if step.detail:
        body.append(f"<div class='muted'>{escape(step.detail)}</div>")
    if step.exchange is not None and 0 <= step.exchange < len(exchanges):
        body.append(_exchange(exchanges[step.exchange]))
    if step.development is not None:
        body.append(_details("Submitted development", _pre(step.development)))
    if step.diagnostics:
        body.append(
            _details(
                f"Diagnostics ({len(step.diagnostics)})",
                "".join(_pre(d) for d in step.diagnostics),
                open_=not step.ok,
            )
        )
    return f"<div class='step'>{''.join(body)}</div>"


def render_html(trajectory: TrajectoryResponse) -> str:
    """The whole page. Every value that came from the database is passed through `escape`."""
    t = trajectory
    header = "".join(
        f"<tr><th>{escape(k)}</th><td>{escape(str(v))}</td></tr>"
        for k, v in (
            ("attempt", t.attempt_id),
            ("provenance", t.provenance),
            ("model", t.model_id or "none (symbolic)"),
            ("weights", t.model_weights_hash or "not reported"),
            ("tokenizer", t.tokenizer_revision or "none"),
            ("opening sampling", t.sampling),
            ("seed", t.seed),
            ("steps", t.n_steps),
        )
    )
    steps = "".join(_step(i, s, t.exchanges) for i, s in enumerate(t.steps, start=1))
    # Exchanges no step points at -- recorded before steps carried their exchange index -- are
    # shown after the steps rather than silently dropped.
    linked = {s.exchange for s in t.steps if s.exchange is not None}
    orphans = "".join(
        f"<h3>Exchange {e.index}</h3>{_exchange(e)}" for e in t.exchanges if e.index not in linked
    )
    tools = (
        "<table><tr><th>step</th><th>tool</th><th>trust</th><th>ok</th><th>ms</th></tr>"
        + "".join(
            f"<tr><td>{c.step_index}</td><td>{escape(c.server)}/{escape(c.tool)}</td>"
            f"<td>{escape(c.trust)}</td><td>{c.ok}</td><td>{c.latency_ms}</td></tr>"
            for c in t.tool_calls
        )
        + "</table>"
        if t.tool_calls
        else "<p class='muted'>none</p>"
    )
    if t.verdict is None:
        verdict = "<p class='muted'>no verdict: nothing reached <code>/v1/link</code></p>"
    else:
        v = t.verdict
        verdict = (
            f"<p><b>{escape(v.kind)}</b> &middot; link {v.link_ok} &middot; replay {v.replay_ok}"
            f" &middot; audit {v.axiom_audit_ok} &middot; axioms {escape(', '.join(v.axioms))}"
            f" &middot; {v.elapsed_ms} ms</p>"
            + (_details("Accepted proof", _pre(v.proof), open_=True) if v.proof else "")
            + (
                _details(
                    f"Diagnostics ({len(v.diagnostics)})", "".join(_pre(d) for d in v.diagnostics)
                )
                if v.diagnostics
                else ""
            )
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Attempt {escape(str(t.attempt_id))}</title><style>{_STYLE}</style></head><body>"
        f"<h1>Attempt {escape(str(t.attempt_id))}</h1><table>{header}</table>"
        f"<h2>Steps</h2>{steps or '<p class=muted>none</p>'}{orphans}"
        f"<h2>Tool calls</h2>{tools}<h2>Verdict</h2>{verdict}</body></html>"
    )
