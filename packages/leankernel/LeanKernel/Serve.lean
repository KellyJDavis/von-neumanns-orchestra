import Lean
import LeanKernel.Seal

/-!
`serve`: a persistent process that elaborates against one warm base environment across many
requests, instead of paying import cost per check (spec §6.2's worker model -- "one REPL process
per worker, keyed by `base_env_digest`"; cold per-child compilation was measured at ~78% of
pipeline time vs. <1% warm, per CLAUDE.md's M1.1 notes). `leanserv`'s Python `pool.py` spawns one
of these per warm worker slot and talks to it over stdin/stdout.

Protocol: newline-delimited JSON, one request line in, exactly one response line out, in order --
simple enough that a Python subprocess wrapper can pair requests with responses by position alone,
without a length-prefixed framing layer. A request's `kind` field selects the handler and defaults
to `"check"` when absent, so the single-kind protocol M1.8.1 shipped stays valid on the wire.
`check` (spec §6.2's `/v1/check`) and `seal` (`/v1/seal`, M2.1.1) exist; `link`/`replay`/
`decompose` land with the milestones that actually drive them (M2.1.2, M2.1.3).
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

/-- One goal to seal: `name` is the unqualified declaration name (the bundle puts it under
`LeanAgent.Goals`), `statement` the goal's own type expression as source text. -/
structure SealGoal where
  name      : String
  statement : String
  deriving FromJson

/-- A `seal` request (spec §4.1, §6.2's `/v1/seal`): elaborate and freeze a goal bundle. -/
structure SealRequest where
  id    : String
  goals : Array SealGoal
  deriving FromJson

/-- Per-goal seal outcome plus the bundle source those goals belong to. `reports` is parallel to
the request's own `goals`, so a caller can create obligations for the entries that sealed and
report the ones that didn't (spec §6.1: "a submission with ten goals of which one does not
elaborate creates nine obligations and reports the tenth").

`bundleSource` is the complete, compilable bundle file, `import` header included -- spec §4.1's
"the `.olean` artifact is produced lazily out of band". Nothing here waits on the build system;
the caller stores this text and compiles it later, off the hot path. -/
structure SealResponse where
  id           : Option String := none
  ok           : Bool
  diagnostics  : Array String := #[]
  reports      : Array SealReport := #[]
  bundleSource : String := ""
  deriving ToJson

/-- The namespace every sealed goal is declared under (spec §4.1's bundle template). -/
def goalsNamespace : String := "LeanAgent.Goals"

/-- Spec §4.1's system-fixed option set, forced at seal time rather than left to the submission:
`autoImplicit` silently generalizing a mistyped identifier is one of §4.5's admission signals, so
sealing must not be the place it slips through. -/
def sealedOptionLines : String :=
  "set_option autoImplicit false\nset_option relaxedAutoImplicit false"

/-- The source elaborated for one goal, and emitted verbatim as that goal's lines in the bundle --
the same text in both places deliberately, so what is verified at seal time is what is compiled
out of band later, rather than two separately-generated forms that could drift.

`Sort _` uniformly rather than spec's template's mix of `Sort _` and `Prop`: `Prop` is `Sort 0`, so
the inferred form covers both the data-producing and propositional cases, and M1.1 established
that a top-level `def`'s universe parameters are generalized unconditionally -- they do not need to
be written out to end up explicit on the sealed constant. -/
def goalDeclSource (goal : SealGoal) : String :=
  s!"def {goal.name} : Sort _ := {goal.statement}"

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

/-- A dotted string (`LeanAgent.Goals.G_x`, `Mathlib.Algebra.Group.Defs`) as the hierarchical
`Name` Lean's own parser would have produced from the same text: split on `.` and folded, rather
than `Name.mkSimple`, which buries the dots inside one component and would then match nothing.
Not delegating to a `String → Name` stdlib helper, since none was confirmed to do exactly this
without risking a different quoting convention (e.g. escaping reserved-word components as
`«...»`, which neither a module path nor a generated goal name ever needs). -/
def dottedName (s : String) : Name :=
  (s.splitOn ".").foldl Name.mkStr Name.anonymous

/-- Every constant in `env` absent from `baseEnv` -- the same new-vs-base delta `Replay.lean`
computes, reused here to check what a goal's own elaboration actually introduced. -/
def newConstantNames (baseEnv env : Environment) : Array Name :=
  env.constants.fold (init := #[]) fun acc n _ =>
    if baseEnv.constants.contains n then acc else acc.push n

/-- Whether `s` is acceptable as a goal's unqualified declaration name: a single identifier
component, allowlisted character by character (leading letter or `_`, then letters, digits, `_`,
`'`).

An allowlist rather than a scan for dangerous characters, for the same reason spec §4.4 requires
one for axioms: `name` is spliced into generated source just as `statement` is, so `def {name} :
Sort _ := ...` is an injection site too, and a deny-list only excludes the escapes thought of
today. Dots are excluded along with everything else, which additionally keeps `dottedName` faithful
-- the sealed constant is then exactly `LeanAgent.Goals.<name>`, never something nested deeper that
a caller's own name-to-obligation mapping would not predict. -/
def isValidGoalName (s : String) : Bool :=
  match s.toList with
  | [] => false
  | c :: rest => (c.isAlpha || c == '_') && rest.all fun c => c.isAlphanum || c == '_' || c == '\''

/--
Seal one goal (spec §4.1): elaborate its own bundle lines against the warm base environment, then
decide whether sealing actually succeeded via `checkSealed` (M1.1) -- which checks both the message
log and the declaration's axiom cone, since a `sorry` in a *statement* elaborates with only a
warning and would otherwise pass a diagnostics-only check.

Each goal is elaborated on its own rather than the whole bundle at once, deliberately.
`checkSealed` reads the message log as a whole, so one bad goal in a single combined elaboration
would mark every other goal in the same bundle failed too -- which would break exactly the
behaviour spec §6.1 asks for ("a submission with ten goals of which one does not elaborate creates
nine obligations and reports the tenth"). Goals never reference each other, so elaborating them
independently costs a few extra passes over an already-warm environment and buys exact per-goal
attribution.

A goal whose elaboration introduces any constant that is not the sealed declaration itself (or one
of Lean's own auxiliaries *under* it, e.g. `...G_x.match_1`) fails to seal. Both `name` and
`statement` are spliced into generated source, so either can be crafted to close the `def` early
and append commands of its own, smuggling declarations -- or a `set_option` re-enabling the very
`autoImplicit` this bundle forces off -- into what is supposed to be "an environment the agent
cannot influence" (§1.1). Checking the constants actually introduced catches that structurally,
rather than by trying to sanitize the text. The check is against the goal's own declaration name,
not merely `LeanAgent.Goals`: a smuggled `LeanAgent.Goals.Helper` is inside the namespace but is
still a declaration nobody sealed, and could collide with a later goal's name.
-/
def sealGoal (baseEnv : Environment) (goal : SealGoal) : IO SealReport := do
  let declName := dottedName s!"{goalsNamespace}.{goal.name}"
  if !isValidGoalName goal.name then
    return { decl := declName, levelParams := #[], ok := false,
             diagnostics := #[s!"goal name is not a plain identifier: {goal.name}"] }
  let source :=
    s!"{sealedOptionLines}\nnamespace {goalsNamespace}\n{goalDeclSource goal}\nend {goalsNamespace}"
  try
    let (env, messages) ← Lean.Elab.process source baseEnv {}
    let escaped := (newConstantNames baseEnv env).filter fun n => !declName.isPrefixOf n
    if !escaped.isEmpty then
      return { decl := declName, levelParams := #[], ok := false,
               diagnostics := #[s!"elaboration introduced declarations other than {declName}: {escaped}"] }
    let coreCtx : Core.Context := { fileName := "<seal>", fileMap := FileMap.ofString source }
    checkSealed declName |>.toIO' coreCtx { env, messages }
  catch ex =>
    return { decl := declName, levelParams := #[], ok := false, diagnostics := #[toString ex] }

/-- Seal every goal in the request, then assemble the bundle from the goals that actually sealed.

Only the sealed goals, deliberately -- this is what makes spec §6.1's "a submission with ten goals
of which one does not elaborate creates nine obligations and reports the tenth" true of the
*artifact* and not just of the report array. `bundleSource` is compiled out of band later, with
nothing re-verifying it at that point, so including a goal whose text was rejected here would
hand the compile step exactly the source this handler just refused -- an injected declaration that
failed to seal would otherwise still end up in the compiled `.olean` backing its nine innocent
siblings. `reports` stays parallel to the request's own `goals` regardless, so the caller can still
report the tenth.

A name used by more than one goal fails all of its users rather than only the later ones: each
elaborates fine alone, but Lean refuses to redeclare a name (CLAUDE.md's M1.2 note), so the bundle
they share would not compile -- and there is no principled way to pick which duplicate is "the"
goal. Catching it here keeps that failure at seal time, where it is attributable, instead of in
an out-of-band compile with no request to attribute it to.
-/
def sealBundle (baseEnv : Environment) (imports : Array Name) (req : SealRequest) :
    IO SealResponse := do
  let names := req.goals.map (·.name)
  let reports ← req.goals.mapM fun goal =>
    if (names.filter (· == goal.name)).size > 1 then
      pure { decl := dottedName s!"{goalsNamespace}.{goal.name}", levelParams := #[], ok := false,
             diagnostics := #[s!"goal name used by more than one goal in this bundle: {goal.name}"] }
    else
      sealGoal baseEnv goal
  let sealedGoals := (req.goals.zip reports).filterMap fun (goal, report) =>
    if report.ok then some goal else none
  let importLines := String.intercalate "\n" (imports.toList.map (s!"import {·}"))
  let declLines := String.intercalate "\n" (sealedGoals.toList.map goalDeclSource)
  let bundleSource :=
    s!"{importLines}\n{sealedOptionLines}\nnamespace {goalsNamespace}\n{declLines}\nend {goalsNamespace}\n"
  return { ok := reports.all (·.ok), reports, bundleSource }

/-- Handle one already-read line: read its `kind` (absent means `"check"`, keeping M1.8.1's
single-kind wire format valid), decode into that kind's own request type, and dispatch. Parsing is
pure (`Json.parse`/`fromJson?` both return `Except`, never throw), so only the handlers' own
elaboration needs exception handling.

Returns already-serialized `Json` rather than one response type: `check` and `seal` answer with
genuinely different payloads, and forcing both into one envelope would mean optional fields that
are meaningless for whichever kind didn't produce them. Both share `id`/`ok`/`diagnostics`, so a
caller can always read the outcome without knowing which kind it asked for. -/
def handleLine (baseEnv : Environment) (imports : Array Name) (line : String) : IO Json := do
  let errorResponse (id : Option String) (msg : String) : Json :=
    toJson ({ id, ok := false, diagnostics := #[msg] } : CheckResponse)
  match Json.parse line with
  | .error err => return errorResponse none err
  | .ok json =>
    let id? := (json.getObjValAs? String "id").toOption
    let kind := (json.getObjValAs? String "kind").toOption.getD "check"
    match kind with
    | "check" =>
      match fromJson? (α := CheckRequest) json with
      | .error err => return errorResponse id? err
      | .ok req =>
        let resp ← checkAgainst baseEnv req.body
        return toJson { resp with id := some req.id }
    | "seal" =>
      match fromJson? (α := SealRequest) json with
      | .error err => return errorResponse id? err
      | .ok req =>
        let resp ← sealBundle baseEnv imports req
        return toJson { resp with id := some req.id }
    | other => return errorResponse id? s!"unknown request kind: {other}"

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
      let resp ← handleLine baseEnv imports line.trimAscii.toString
      stdout.putStrLn resp.compress
      stdout.flush
  return 0

end LeanKernel
