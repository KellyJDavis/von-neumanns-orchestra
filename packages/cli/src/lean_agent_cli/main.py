"""The CLI (spec §3's `run, batch, from-folder, status, materialize`), over the public API.

An httpx client of §6.1 and nothing else -- no database handle, no Lean toolchain, no shared code
path with the server. That is the point of the milestone: if the CLI can do it, so can anyone
else's client, which is the only real test of whether §6.1 is a complete API rather than a
convenient subset of one.

`argparse`, not `click`/`typer`. This repo has consistently declined dependencies it does not need
(no `aiofiles` for local disk I/O, no `prometheus_client` for eight lines of text, no
`pytest-asyncio`), and five subcommands over a handful of flags is squarely inside what the
standard library does well.

Exit codes are part of the interface, because these commands get put in scripts:

* `0` -- did what was asked. For `run --wait` and `materialize`, that additionally means the
  artifact is *complete*: every hole filled, the file elaborates, every declaration links.
* `1` -- the request succeeded but the answer is "no": a run that finished incomplete, an artifact
  with unfilled holes, a submission whose goals all failed to seal.
* `2` -- the request itself failed (unknown run, unreachable server, bad usage).

Separating 1 from 2 is what lets a script tell "this file could not be proved" from "the system is
broken", which is the same distinction `infra_error` draws inside the pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lean_agent_cli.client import ApiClient, ApiError

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_ERROR = 2

#: Statuses a run's obligations can still leave. Anything else is settled, and a `--wait` that kept
#: polling past them would hang on a run that is simply finished with work outstanding.
LIVE_STATUSES = frozenset({"open", "in_progress", "decomposed"})


def _emit(payload: Any, *, as_json: bool, human: str) -> None:
    """`--json` prints the server's own object, unreshaped.

    Reshaping would make the CLI a second, undocumented schema that drifts from the OpenAPI
    document; a caller that wants structure should get exactly what the API said.
    """
    print(json.dumps(payload, indent=2, sort_keys=True) if as_json else human)


def _submit(client: ApiClient, args: argparse.Namespace, source: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "base_env": args.base_env,
        "policy": args.policy,
        "max_depth": args.max_depth,
        "budget_attempts": args.budget_attempts,
    }
    if source is not None:
        payload["source"] = source
    else:
        payload["statement"] = args.statement
    if args.allow_sorry:
        payload["allow_sorry"] = True
    if args.axiom_allowlist:
        payload["axiom_allowlist"] = args.axiom_allowlist
    return client.create_run(payload)


def _describe_submission(created: dict[str, Any]) -> str:
    lines = [f"run {created['run_id']}  manifest {created['manifest_hash'][:12]}"]
    lines.append(f"  {len(created['root_obligations'])} obligation(s)")
    for failure in created["seal_failures"]:
        # Reported, never fatal: spec §6.1 returns `seal_failures` rather than raising, so a
        # submission with nine good goals and one bad one is nine obligations and one line here.
        lines.append(f"  seal failed: {failure['name']}: {failure['reason']}")
    for obligation_id, signals in created["admission"].items():
        if signals.get("closed_by"):
            lines.append(
                f"  admission: {obligation_id} closed by {signals['closed_by']} in "
                f"{signals['closed_in_ms']} ms -- possible mis-formalization (spec §4.5)"
            )
        if signals.get("needs_auto_implicit"):
            lines.append(
                f"  admission: {obligation_id} elaborates only with autoImplicit -- a mistyped "
                "identifier may have been silently generalized (spec §4.5)"
            )
    return "\n".join(lines)


def _wait_for(client: ApiClient, run_id: str, timeout_s: float, poll_s: float) -> dict[str, Any]:
    """Poll `GET /v1/runs/{id}` until nothing is live.

    Polling rather than subscribing to `/events`: an SSE consumer in a CLI means holding a
    streaming connection open and reassembling the state it describes, when the thing being waited
    for is precisely the aggregate `GET /v1/runs/{id}` already returns.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        status = client.get_run(run_id)
        counts = status["obligations_by_status"]
        if not any(counts.get(s) for s in LIVE_STATUSES):
            return status
        if time.monotonic() >= deadline:
            status["timed_out"] = True
            return status
        time.sleep(poll_s)


def cmd_run(client: ApiClient, args: argparse.Namespace) -> int:
    source = Path(args.file).read_text() if args.file else None
    created = _submit(client, args, source)
    run_id = str(created["run_id"])

    if not args.wait:
        _emit(created, as_json=args.json, human=_describe_submission(created))
        return EXIT_OK if created["root_obligations"] else EXIT_INCOMPLETE

    status = _wait_for(client, run_id, args.timeout, args.poll)
    proved = status["obligations_by_status"].get("proved", 0)
    total = sum(status["obligations_by_status"].values())
    human = f"{_describe_submission(created)}\n  {proved}/{total} proved" + (
        "  (timed out waiting)" if status.get("timed_out") else ""
    )
    _emit({"created": created, "status": status}, as_json=args.json, human=human)
    return EXIT_OK if total and proved == total else EXIT_INCOMPLETE


def cmd_batch(client: ApiClient, args: argparse.Namespace) -> int:
    return _run_many(client, args, [Path(p) for p in args.files])


def cmd_from_folder(client: ApiClient, args: argparse.Namespace) -> int:
    files = sorted(Path(args.folder).rglob("*.lean"))
    if not files:
        print(f"no .lean files under {args.folder}", file=sys.stderr)
        return EXIT_ERROR
    return _run_many(client, args, files)


def _run_many(client: ApiClient, args: argparse.Namespace, files: Sequence[Path]) -> int:
    """One run per file, and a failure on one file does not abandon the rest.

    A batch that stopped at the first unparseable file would make the whole command hostage to its
    worst input, which for a benchmark folder is the normal case rather than the exception. Each
    result is reported and the exit code summarizes.
    """
    results: list[dict[str, Any]] = []
    worst = EXIT_OK
    for path in files:
        try:
            created = _submit(client, args, path.read_text())
        except (ApiError, OSError) as exc:
            results.append({"file": str(path), "error": str(exc)})
            worst = EXIT_ERROR
            if not args.json:
                print(f"{path}: ERROR {exc}", file=sys.stderr)
            continue
        entry: dict[str, Any] = {"file": str(path), "created": created}
        if args.wait:
            entry["status"] = _wait_for(client, str(created["run_id"]), args.timeout, args.poll)
            counts = entry["status"]["obligations_by_status"]
            proved, total = counts.get("proved", 0), sum(counts.values())
            if not total or proved != total:
                worst = max(worst, EXIT_INCOMPLETE)
            if not args.json:
                print(f"{path}: {proved}/{total} proved  (run {created['run_id']})")
        else:
            if not created["root_obligations"]:
                worst = max(worst, EXIT_INCOMPLETE)
            if not args.json:
                print(
                    f"{path}: {len(created['root_obligations'])} obligation(s)  "
                    f"(run {created['run_id']})"
                )
        results.append(entry)
    if args.json:
        _emit(results, as_json=True, human="")
    return worst


def cmd_status(client: ApiClient, args: argparse.Namespace) -> int:
    status = client.get_run(args.run_id)
    counts = status["obligations_by_status"]
    lines = [
        f"run {status['run_id']}  {status['status']}",
        "  " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no obligations"),
        (
            f"  spend: {status['spend']['tokens']} tokens, "
            f"{status['spend']['kernel_ms']} kernel ms, {status['spend']['attempts']} attempts"
        ),
    ]
    if args.obligations:
        # Fetched once and reused for both renderings: two calls could disagree, since a run is
        # still being written to while someone is looking at it.
        obligations = client.list_obligations(args.run_id, status=args.filter_status)
        lines.extend(
            f"  {o['id']}  {o['status']:<12} depth={o['depth']}  {o['goal_src']}"
            for o in obligations
        )
        status["obligations"] = obligations
    _emit(status, as_json=args.json, human="\n".join(lines))
    return EXIT_OK


def cmd_materialize(client: ApiClient, args: argparse.Namespace) -> int:
    """Spec §6.3 step 6's artifact, written out.

    Writes the file even when it is incomplete, and says so on stderr while exiting 1. A partially
    proved run's artifact is the useful thing to hand back -- it names its open holes -- and
    refusing to write it would make the failure mode "no output at all" rather than "output with
    the gaps marked".
    """
    artifact = client.get_artifact(args.run_id)
    if args.output:
        Path(args.output).write_text(artifact["source"])
    elif not args.json:
        print(artifact["source"])

    if args.json:
        _emit(artifact, as_json=True, human="")
    if not artifact["complete"]:
        print(
            f"incomplete: {len(artifact['unfilled'])} unfilled hole(s) "
            f"{artifact['unfilled']}; elaborates={artifact['elaborates']} "
            f"links={artifact['links']}",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE
    if args.output:
        print(f"wrote {args.output} ({artifact['holes']} hole(s) filled, links verified)")
    return EXIT_OK


def _exchange_lines(exchange: dict[str, Any]) -> list[str]:
    """One request: its prompt exactly as sent, then every sample, each verbatim."""
    prompt = exchange["prompt"]
    if prompt["text"] is not None:
        how, body = f"decoded with tokenizer {prompt['decoded_with']}", prompt["text"]
    else:
        how = prompt.get("note") or "token ids only"
        body = " ".join(str(i) for i in prompt.get("token_ids") or [])
    lines = [f"  === prompt: {prompt['token_count']} tokens, {how} ===", body]
    for sample in exchange["completions"]:
        text = sample["text"]
        if text is None:
            text = " ".join(str(i) for i in sample.get("token_ids") or [])
        lines.append(
            f"  === sample {sample['index']}: {sample['token_count']} tokens, finish "
            f"{sample['finish_reason'] or 'not recorded'}, log-probability "
            f"{sample['logprob_sum']:.2f} ==="
        )
        lines.append(text)
    return lines


def cmd_trajectory(client: ApiClient, args: argparse.Namespace) -> int:
    """Spec §7.4's trajectory viewer, in a terminal: one attempt, every prompt and sample
    verbatim, in the order it happened.

    Nothing is collapsed or cut -- pipe it to a pager. It is the same data the API serves as a page
    at `/attempts/{id}`, and prompts are the stored token ids decoded, never the policy's messages
    re-rendered (the API says so, per prompt, when it cannot decode).
    """
    t = client.get_trajectory(args.attempt_id)
    lines = [
        f"attempt {t['attempt_id']}  provenance={t['provenance']}  model={t['model_id'] or 'none'}",
        (
            f"  tokenizer={t.get('tokenizer_revision') or 'none'}  opening sampling="
            f"{t.get('sampling')}  seed={t.get('seed')}"
        ),
    ]
    exchanges = t.get("exchanges") or []
    for number, step in enumerate(t["steps"], start=1):
        mark = "ok" if step["ok"] else "failed"
        detail = f"  {step['detail']}" if step.get("detail") else ""
        lines.append(f"\n[{number}] {step['label']}  {step['action']}  {mark}{detail}")
        index = step.get("exchange")
        if index is not None and 0 <= index < len(exchanges):
            lines.extend(_exchange_lines(exchanges[index]))
        if step.get("development"):
            lines.extend(["  --- submitted ---", step["development"]])
        for diagnostic in step.get("diagnostics") or []:
            lines.extend(["  --- diagnostic ---", diagnostic.rstrip("\n")])
    verdict = t.get("verdict")
    if verdict is None:
        lines.append("\nverdict: none (nothing reached /v1/link)")
    else:
        lines.append(
            f"\nverdict: {verdict['kind']}  link={verdict['link_ok']} replay={verdict['replay_ok']}"
            f" audit={verdict['axiom_audit_ok']}  axioms={', '.join(verdict['axioms'])}"
        )
        if verdict.get("proof"):
            lines.extend(["--- accepted proof ---", verdict["proof"]])
    _emit(t, as_json=args.json, human="\n".join(lines))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lean-agent",
        description="Submit Lean files and statements to a von-neumann's-orchestra API, and "
        "collect the proofs it produces.",
    )
    parser.add_argument(
        "--api",
        default="http://localhost:8000",
        help="Base URL of the public API (spec §6.1).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the API's own JSON.")

    # The same two flags again on every subcommand, because `lean-agent run --json` is how anyone
    # would actually type it and `lean-agent --json run` is the only form argparse accepts by
    # default. `SUPPRESS` is what makes accepting both safe: without it the subparser's own default
    # would overwrite a value given before the subcommand, silently turning `--json run` back off.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_submit_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--base-env", required=True, help="Base environment digest.")
        sub.add_argument("--policy", default="SymbolicPortfolio")
        sub.add_argument("--allow-sorry", action="store_true")
        sub.add_argument("--axiom-allowlist", nargs="*", default=None)
        sub.add_argument("--max-depth", type=int, default=6)
        sub.add_argument("--budget-attempts", type=int, default=8)

    def add_wait_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--wait", action="store_true", help="Poll until nothing is schedulable.")
        sub.add_argument("--timeout", type=float, default=600.0)
        sub.add_argument("--poll", type=float, default=2.0)

    run = subparsers.add_parser("run", help="Submit one file or statement.", parents=[common])
    group = run.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="A .lean file containing `sorry`s.")
    group.add_argument("--statement", help="A bare goal.")
    add_submit_flags(run)
    add_wait_flags(run)
    run.set_defaults(handler=cmd_run)

    batch = subparsers.add_parser(
        "batch", help="Submit several files, one run each.", parents=[common]
    )
    batch.add_argument("files", nargs="+")
    add_submit_flags(batch)
    add_wait_flags(batch)
    batch.set_defaults(handler=cmd_batch)

    folder = subparsers.add_parser(
        "from-folder", help="Submit every .lean file under a folder.", parents=[common]
    )
    folder.add_argument("folder")
    add_submit_flags(folder)
    add_wait_flags(folder)
    folder.set_defaults(handler=cmd_from_folder)

    status = subparsers.add_parser(
        "status", help="Status, spend and counts for a run.", parents=[common]
    )
    status.add_argument("run_id")
    status.add_argument("--obligations", action="store_true", help="List them too.")
    status.add_argument("--filter-status", default=None)
    status.set_defaults(handler=cmd_status)

    materialize = subparsers.add_parser(
        "materialize", help="Fetch a run's assembled .lean artifact.", parents=[common]
    )
    materialize.add_argument("run_id")
    materialize.add_argument("-o", "--output", help="Write here instead of stdout.")
    materialize.set_defaults(handler=cmd_materialize)

    trajectory = subparsers.add_parser(
        "trajectory",
        help="One attempt's trajectory: every prompt, sample, submission and the verdict.",
        parents=[common],
    )
    trajectory.add_argument("attempt_id")
    trajectory.set_defaults(handler=cmd_trajectory)

    return parser


def main(argv: Sequence[str] | None = None, *, client: ApiClient | None = None) -> int:
    """`client` is injectable so the tests drive the real command functions against the real API
    rather than a mock of either -- the CLI's own argument parsing and exit codes are as much a
    part of the interface as the requests it makes."""
    args = build_parser().parse_args(argv)
    owned = client is None
    api = client or ApiClient(args.api)
    try:
        return int(args.handler(api, args))
    except ApiError as exc:
        # The server's `detail` explains itself; a stack trace would not.
        print(f"error: {exc.detail}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if owned:
            api.close()


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
