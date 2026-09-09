import Lean

/-!
`serve`: a persistent process that elaborates against one warm base environment across many
requests, instead of paying import cost per check (spec §6.2's worker model -- "one REPL process
per worker, keyed by `base_env_digest`"; cold per-child compilation was measured at ~78% of
pipeline time vs. <1% warm, per CLAUDE.md's M1.1 notes). `leanserv`'s Python `pool.py` (a later
milestone) spawns one of these per warm worker slot and talks to it over stdin/stdout.

Protocol: newline-delimited JSON, one request line in, exactly one response line out, in order --
simple enough that a Python subprocess wrapper can pair requests with responses by position alone,
without a length-prefixed framing layer. Only `check` exists so far (spec §6.2's `/v1/check`):
`seal`/`link`/`replay`/`decompose` requests are deferred to whichever later milestone actually
drives `leanserv`'s pool against them, since adding request kinds nothing sends yet would be
speculative surface area with no way to check the shape is right (same reasoning as
`packages/core/src/lean_agent_core/protocols.py`'s `BlobStore`-only choice in M1.7).
-/

namespace LeanKernel

open Lean

/-- A `check` request: elaborate `body` (one or more commands, no `import` line of its own --
the base environment already carries whatever imports the worker was started with) against the
warm base environment. `id` is echoed back unexamined so callers can correlate responses without
relying on line ordering alone. -/
structure CheckRequest where
  id   : String
  body : String
  deriving FromJson

/-- Uniform response envelope for every request `serve` handles, successful or not. `id` is
`none` only when the input line wasn't even valid JSON (so there was no `id` field to recover) --
every other failure (unknown-shaped request, an internal exception while elaborating) still
carries the request's own `id` back, so a line that WAS understood as a request always gets a
correlatable response. -/
structure CheckResponse where
  id          : Option String := none
  ok          : Bool
  diagnostics : Array String := #[]
  deriving ToJson

/-- Elaborate `body` against `baseEnv` fresh each call -- `Lean.Elab.process` returns a new
environment rather than mutating `baseEnv` in place, so one request's declarations never leak
into the next. This is the isolation different agent attempts checked against the same warm
worker need: two unrelated submissions must never see each other's names. -/
def checkAgainst (baseEnv : Environment) (body : String) : IO CheckResponse := do
  try
    let (_env, messages) ← Lean.Elab.process body baseEnv {}
    let diagnostics ← messages.toList.toArray.mapM (·.toString)
    return { ok := !messages.hasErrors, diagnostics }
  catch ex =>
    return { ok := false, diagnostics := #[toString ex] }

/-- Handle one already-read line: parse it as a `CheckRequest` and dispatch, or produce an error
response if it isn't one. Parsing is pure (`Json.parse`/`fromJson?` both return `Except`, never
throw), so only the actual elaboration in `checkAgainst` needs its own exception handling. -/
def handleLine (baseEnv : Environment) (line : String) : IO CheckResponse := do
  match Json.parse line >>= fromJson? (α := CheckRequest) with
  | .error err => return { id := none, ok := false, diagnostics := #[err] }
  | .ok req => checkAgainst baseEnv req.body >>= fun resp => return { resp with id := some req.id }

/-- Build the warm base environment once, then loop: read a line, respond with exactly one line,
repeat until stdin closes (`getLine` returns `""` at EOF, confirmed via Lean's own `IO.FS.Handle`
docstring -- not a bare empty *input* line, which still carries the line-break `getLine` strips
only at genuine EOF).

`importModules`, not `withImportModules`: the latter's own docstring warns it frees the
environment's compacted regions the moment its callback returns, which is fatal here since the
whole point of `serve` is for `baseEnv` to outlive any single call (see CLAUDE.md's M1.1 notes,
where an earlier milestone paid for this the hard way returning a raw `Environment` from
`withImportModules`'s callback). `enableInitializersExecution` is required before importing for
the same M1.1-discovered reason `decompose`/`sealOf` need it: without it, builtin term
elaborators silently report "has not been implemented" even though parsing works, since builtin
elaborator entries are native closures populated by each module's own initializer, not
serializable `.olean` data.

Every response line is written with a trailing newline and flushed immediately -- output is
buffered by default, and a caller blocking on `readline()` on the other end of the pipe would
hang indefinitely on a response `serve` has already computed but not yet flushed to the OS pipe. -/
unsafe def runServe (imports : Array Name) : IO UInt32 := do
  enableInitializersExecution
  let baseEnv ← importModules (imports.map fun m => { module := m }) {} (loadExts := true)
  let stdin ← IO.getStdin
  let stdout ← IO.getStdout
  let mut running := true
  while running do
    let line ← stdin.getLine
    if line.isEmpty then
      running := false
    else
      let resp ← handleLine baseEnv line.trimAscii.toString
      stdout.putStrLn (toJson resp).compress
      stdout.flush
  return 0

end LeanKernel
