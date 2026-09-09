import Lean
import LeanKernel.Audit
import LeanKernel.Seal
import LeanKernel.Link
import LeanKernel.Replay
import LeanKernel.Sorries
import LeanKernel.Serve
import LeanKernelTests.AuditFixtures
import LeanKernelTests.Goals

/-!
Test runner for `LeanKernel`. Covers spec §4.4 gates 2 and 3 (Audit) and §4.1's seal-failure
detection (Seal). Drives Lean's own elaborator directly rather than shelling out and parsing
CLI output -- the same technique `lean4checker`'s `Main.lean` used for headless environment
loading before that tool was merged into Lean core.
-/

open Lean

namespace LeanKernelTests

structure Check where
  name     : String
  expected : Bool
  actual   : Bool

def Check.passed (c : Check) : Bool := c.expected == c.actual

def mkCoreCtx : Core.Context := { fileName := "<kernel-tests>", fileMap := FileMap.ofString "" }

def auditChecks (env : Environment) : IO (Array Check) := do
  let run (decl : Name) (allow : Array Name) : IO LeanKernel.AxiomReport :=
    (LeanKernel.auditAxioms decl allow).toIO' mkCoreCtx { env }

  let clean ← run `LeanKernelTests.AuditFixtures.clean LeanKernel.defaultAllowlist
  let nativeDecide ← run `LeanKernelTests.AuditFixtures.usesNativeDecide LeanKernel.defaultAllowlist
  let sorryDenied ← run `LeanKernelTests.AuditFixtures.usesSorry LeanKernel.defaultAllowlist
  let sorryAllowed ← run `LeanKernelTests.AuditFixtures.usesSorry
    (LeanKernel.defaultAllowlist.push `sorryAx)
  let customAxiom ← run `LeanKernelTests.AuditFixtures.usesCustomAxiom LeanKernel.defaultAllowlist

  return #[
    -- A `decide`-only proof needs no axioms at all and passes the default allowlist cleanly.
    { name := "audit/clean: no axioms", expected := true, actual := clean.axioms.isEmpty },
    { name := "audit/clean: audit passes", expected := true, actual := clean.ok },

    -- Gate 3: rejected against the default allowlist. This assertion never names the axiom
    -- native_decide generates -- only that *some* axiom outside the allowlist appears, and
    -- that the audit therefore fails. That is the property a deny-list cannot express.
    { name := "audit/native_decide: fails against default allowlist",
      expected := false, actual := nativeDecide.ok },
    { name := "audit/native_decide: flagged as compiler trust",
      expected := true, actual := nativeDecide.usesCompilerTrust },
    { name := "audit/native_decide: not flagged as sorry",
      expected := false, actual := nativeDecide.usesSorry },

    -- Gate 2: `usesSorry` is detected regardless of whether it happens to be permitted --
    -- detection and permission are separate, so there is no false negative to hide behind an
    -- allowlist that happens to include `sorryAx`.
    { name := "audit/sorry: detected when denied", expected := true, actual := sorryDenied.usesSorry },
    { name := "audit/sorry: fails when denied", expected := false, actual := sorryDenied.ok },
    { name := "audit/sorry: detected when allowed", expected := true, actual := sorryAllowed.usesSorry },
    { name := "audit/sorry: passes when allowed", expected := true, actual := sorryAllowed.ok },

    -- A user-declared axiom outside the standard logical set is compiler-trust-classified and
    -- rejected by the same allowlist mechanism, no special-casing required.
    { name := "audit/custom axiom: fails", expected := false, actual := customAxiom.ok },
    { name := "audit/custom axiom: flagged as compiler trust",
      expected := true, actual := customAxiom.usesCompilerTrust }
  ]

/--
Elaborate `source` as a complete, standalone module -- including its own `import` line, exactly
as a real sealed-goal bundle carries `import <base environment>` at the top (spec §4.1). This
mirrors `leanprover-community/repl`'s `processInput` (the same technique leanserv's warm REPL
workers will use in production) rather than pre-supplying an import array: two earlier attempts
at a simpler path each failed for reasons worth recording, since they will recur if this ever
gets rewritten.

1. `withImportModules` (used by the Audit suite above, fine for inspecting already-*compiled*
   constants) hardcodes `loadExts := false`, which leaves parser/notation extensions
   un-populated. Elaborating fresh source needs `loadExts := true`.
2. Even with `loadExts := true`, manually pre-importing via `importModules` and then running
   `Lean.Elab.process` against that environment left the *term-elaborator* attribute table empty
   (`termElabAttribute.getEntries` returned `[]` for `=`, even though parsing itself worked) --
   builtin elaborator entries are native closures, not serializable data, so they are registered
   by each module's own `initialize`/`builtin_initialize` side effect, and something about
   driving that import step by hand rather than through `Parser.parseHeader` + `processHeader`
   skipped it. Following the header-parsing path below does not have this problem.
-/
unsafe def sealOf (source : String) (decl : Name) : IO LeanKernel.SealReport := do
  enableInitializersExecution
  let inputCtx := Parser.mkInputContext source "<seal-test>"
  let (header, parserState, messages) ← Parser.parseHeader inputCtx
  let (env, messages) ← Lean.Elab.processHeader header {} messages inputCtx
  let frontendState ← Lean.Elab.IO.processCommands inputCtx parserState (Lean.Elab.Command.mkState env messages {})
  let commandState := frontendState.commandState
  (LeanKernel.checkSealed decl).toIO' mkCoreCtx { env := commandState.env, messages := commandState.messages }

unsafe def sealChecks : IO (Array Check) := do
  let ok ← sealOf "import Init\ndef G_ok : Sort _ := ∀ n : Nat, n + 0 = n" `G_ok
  let broken ← sealOf "import Init\ndef G_broken : Sort _ := SomeUndefinedThing" `G_broken
  -- `sorry` in the statement itself elaborates with only a *warning*, not an error (confirmed
  -- empirically) -- this is exactly the case the zero-axiom check exists to catch, since
  -- `hasErrors` alone would miss it.
  let sorryInType ← sealOf "import Init\ndef G_sorry : Sort _ := (sorry : Prop)" `G_sorry

  return #[
    { name := "seal/clean statement: passes", expected := true, actual := ok.ok },
    { name := "seal/clean statement: no diagnostics",
      expected := true, actual := ok.diagnostics.isEmpty },

    { name := "seal/undefined identifier: fails", expected := false, actual := broken.ok },
    { name := "seal/undefined identifier: has diagnostics",
      expected := true, actual := !broken.diagnostics.isEmpty },

    { name := "seal/sorry-in-statement: fails", expected := false, actual := sorryInType.ok },
    -- The only message present is the `sorry` warning itself (no error was also generated) --
    -- confirming this genuinely exercises the axiom-check defense, not an error we missed.
    { name := "seal/sorry-in-statement: exactly the one sorry warning, no error",
      expected := true, actual := sorryInType.diagnostics.size == 1 }
  ]

/--
Elaborate `source` -- which must `import LeanKernelTests.Goals` (a genuinely *compiled* module,
not something defined inline; see `Goals.lean`'s docstring for why that's required) -- and link
`entry` against `goal` within it.

`expectedModuleIdx` is read from the environment produced by header-processing alone, *before*
any of the agent's own commands run. That mirrors production: the caller (leanserv) knows which
module a goal was sealed into at the time sealing happened, and Link's job is to confirm the
name still resolves there *later*, after the agent's submission has had a chance to run -- not to
discover the expected module from scratch each time, which would defeat the check entirely.
-/
unsafe def linkOf (source : String) (goal entry : Name) (allow : Array Name) :
    IO LeanKernel.LinkReport := do
  enableInitializersExecution
  let inputCtx := Parser.mkInputContext source "<link-test>"
  let (header, parserState, messages) ← Parser.parseHeader inputCtx
  let (envAfterHeader, messages) ← Lean.Elab.processHeader header {} messages inputCtx
  let some expectedModuleIdx := envAfterHeader.getModuleIdxFor? goal
    | throw <| IO.userError s!"test setup error: {goal} was not importable from the header alone"
  let frontendState ← Lean.Elab.IO.processCommands inputCtx parserState
    (Lean.Elab.Command.mkState envAfterHeader messages {})
  let commandState := frontendState.commandState
  ((LeanKernel.link goal entry expectedModuleIdx allow).run').toIO' mkCoreCtx
    { env := commandState.env, messages := commandState.messages }

/--
Like `linkOf`, but also replays the session's development against the environment produced by
header-processing alone (i.e. before any agent command ran) -- exactly the fresh, import-only
base Replay is supposed to check against. This is the "gate 4 with replay enabled" follow-up:
M1.2 proved Link's own forced options work regardless of ambient poisoning; this proves the two
mechanisms are genuinely independent and agree, and specifically that `Environment.replay`'s own
verification does not depend on -- and cannot be fooled by -- whatever ambient options (e.g. a
poisoned `debug.skipKernelTC`) were active when the agent's development was elaborated, since
`Environment.replay` uses its own hardcoded checking options, never the session's.
-/
unsafe def linkAndReplayOf (source : String) (goal entry : Name) (allow : Array Name) :
    IO (LeanKernel.LinkReport × LeanKernel.ReplayReport) := do
  enableInitializersExecution
  let inputCtx := Parser.mkInputContext source "<link-replay-test>"
  let (header, parserState, messages) ← Parser.parseHeader inputCtx
  let (envAfterHeader, messages) ← Lean.Elab.processHeader header {} messages inputCtx
  let some expectedModuleIdx := envAfterHeader.getModuleIdxFor? goal
    | throw <| IO.userError s!"test setup error: {goal} was not importable from the header alone"
  let frontendState ← Lean.Elab.IO.processCommands inputCtx parserState
    (Lean.Elab.Command.mkState envAfterHeader messages {})
  let commandState := frontendState.commandState
  let linkReport ← ((LeanKernel.link goal entry expectedModuleIdx allow).run').toIO' mkCoreCtx
    { env := commandState.env, messages := commandState.messages }
  let replayReport ← LeanKernel.replay envAfterHeader commandState.env
  return (linkReport, replayReport)

unsafe def linkChecks : IO (Array Check) := do
  let goalAddZero := `LeanKernelTests.Goals.G_add_zero
  let goalPoly := `LeanKernelTests.Goals.G_poly

  -- Gate 6: a universe-polymorphic, data-producing (`isProp = false`) goal links at matching
  -- arity, exercising the `defnDecl` branch (as opposed to `G_add_zero`'s `thmDecl` branch).
  -- `G_poly.{u} : Sort u := PUnit.{u}` -- G_poly's *value* is `PUnit.{u}`, a type, since it is
  -- itself an element of `Sort u`. Solving it means producing an inhabitant of that type (i.e.
  -- of `PUnit.{u}`, once `G_poly.{u}` is unfolded), not another `Sort`-valued definition shaped
  -- like the goal itself -- an earlier version of this fixture made exactly that mistake and
  -- failed kernel type-checking for a reason that had nothing to do with `Link`.
  let polyOk ← linkOf
    "import LeanKernelTests.Goals\ndef sol_poly.{v} : PUnit.{v} := PUnit.unit"
    goalPoly `sol_poly LeanKernel.defaultAllowlist
  -- Gate 6: arity mismatch (goal has one universe param, this entry has zero) is rejected.
  let polyMono ← linkOf
    "import LeanKernelTests.Goals\ndef sol_poly_mono : Sort 1 := PUnit.{1}"
    goalPoly `sol_poly_mono LeanKernel.defaultAllowlist

  -- Gate 5: textually different but defeq -- the entry's statement goes through an `abbrev`
  -- indirection (`Zero'` unfolds reducibly to `0`) that the goal's own statement never mentions.
  let defeq ← linkOf
    "import LeanKernelTests.Goals\nabbrev Zero' : Nat := 0\ndef sol_ok : ∀ n : Nat, n + Zero' = n := fun n => rfl"
    goalAddZero `sol_ok LeanKernel.defaultAllowlist
  -- Gate 5: a strictly weaker statement (existential instead of universal) does not link --
  -- rejected by kernel type-checking of the constructed declaration itself, not by inspection.
  let weak ← linkOf
    "import LeanKernelTests.Goals\ndef sol_weak : ∃ n : Nat, n + 0 = n := ⟨0, rfl⟩"
    goalAddZero `sol_weak LeanKernel.defaultAllowlist
  -- Gate 4: the same weakened entry, but with the ambient session's own options poisoned via
  -- `set_option debug.skipKernelTC true` before the entry is declared. Link must still reject
  -- it, because it builds its own `Options.empty`-based `opts` rather than reading `getOptions`
  -- -- proving Link's own defense holds independently of replay (not yet implemented; replay is
  -- trivially "disabled" here, matching what this gate specifically asks to isolate).
  let weakPoisoned ← linkOf
    "import LeanKernelTests.Goals\nset_option debug.skipKernelTC true\n\
     def sol_weak2 : ∃ n : Nat, n + 0 = n := ⟨0, rfl⟩"
    goalAddZero `sol_weak2 LeanKernel.defaultAllowlist

  return #[
    { name := "link/poly: matching arity links", expected := true, actual := polyOk.ok },
    { name := "link/poly: mismatched arity fails", expected := false, actual := polyMono.ok },
    { name := "link/poly: mismatched-arity diagnostic mentions arity",
      expected := true, actual := polyMono.diagnostics.any fun d => (d.splitOn "arity").length > 1 },

    { name := "link/defeq-via-abbrev: links", expected := true, actual := defeq.ok },
    { name := "link/weakened mutant: fails", expected := false, actual := weak.ok },
    { name := "link/weakened mutant: has diagnostics", expected := true,
      actual := !weak.diagnostics.isEmpty },

    { name := "link/weakened mutant with debug.skipKernelTC poisoned: still fails",
      expected := false, actual := weakPoisoned.ok }
  ]

/--
Positive/plumbing check: a genuinely new, legitimately-checked declaration, elaborated on top of
a freshly-imported base, replays successfully -- confirming `LeanKernel.replay`'s own
new-vs-base delta computation finds it and that a normal declaration survives independent
kernel re-verification.
-/
unsafe def replayPositiveCheck : IO Check := do
  enableInitializersExecution
  let baseEnv ← importModules #[{ module := `Init }] {} (loadExts := true)
  let (env, _messages) ← Lean.Elab.process "def foo : Nat := 5" baseEnv {}
  let report ← LeanKernel.replay baseEnv env
  return { name := "replay/legitimate new declaration: replays successfully",
            expected := true, actual := report.ok && report.checkedCount > 0 }

/--
Replay's actual value proposition, tested directly against the primitive it wraps
(`Lean.Environment.replay`): a `ConstantInfo` that *claims* a type its value does not have --
exactly what a metaprogram bypassing normal declaration-adding machinery would produce (spec
§4.3's "environment hacking") -- is rejected by the kernel regardless of what it claims about
itself. This is deliberately not routed through `LeanKernel.replay`'s own delta computation: a
raw `Kernel.Environment` cannot be hand-constructed from outside `Lean` (its constructor is
private), so there is no way to get a hand-fabricated bogus `ConstantInfo` into an `Environment`
value except through the exact mechanism this test exercises directly. That is itself a good
sign, not a gap in the test: it means there is no public API surface for smuggling an unchecked
constant into an environment in the first place.
-/
unsafe def replayCatchesBogusConstantCheck : IO Check := do
  enableInitializersExecution
  let baseEnv ← importModules #[{ module := `Init }] {} (loadExts := true)
  -- `True.intro : True`, not `False` -- kernel type-checking must reject this outright.
  let bogus : ConstantInfo := .defnInfo {
    name := `bad
    levelParams := []
    type := mkConst `False
    value := mkConst `True.intro
    hints := .opaque
    safety := .safe
  }
  let ok ← try
    discard <| Environment.replay (({} : Std.HashMap Name ConstantInfo).insert `bad bogus) baseEnv
    pure true
  catch _ =>
    pure false
  return { name := "replay/bogus constant with mismatched type: rejected",
            expected := false, actual := ok }

/-- Gate 4, revisited now that Replay exists (M1.2 tested it with replay trivially "disabled",
since Replay didn't exist yet): a legitimate proof has Link and Replay independently agree, and
a proof that Link correctly rejects via its own forced options is *also* correctly judged
kernel-sound *on its own terms* by Replay, despite the ambient session having
`debug.skipKernelTC` poisoned when it was elaborated -- because Replay never looks at the
session's options at all. Link and Replay check different things (entry-matches-goal vs.
everything-new-is-kernel-sound) and neither depends on the other, or on the ambient session
state, to be correct. -/
unsafe def gate4WithReplayChecks : IO (Array Check) := do
  let goalAddZero := `LeanKernelTests.Goals.G_add_zero
  let (defeqLink, defeqReplay) ← linkAndReplayOf
    "import LeanKernelTests.Goals\nabbrev Zero' : Nat := 0\ndef sol_ok : ∀ n : Nat, n + Zero' = n := fun n => rfl"
    goalAddZero `sol_ok LeanKernel.defaultAllowlist
  let (weakLink, weakReplay) ← linkAndReplayOf
    "import LeanKernelTests.Goals\nset_option debug.skipKernelTC true\n\
     def sol_weak2 : ∃ n : Nat, n + 0 = n := ⟨0, rfl⟩"
    goalAddZero `sol_weak2 LeanKernel.defaultAllowlist
  return #[
    { name := "gate4+replay/legitimate proof: link and replay both agree it's fine",
      expected := true, actual := defeqLink.ok && defeqReplay.ok },
    { name := "gate4+replay/link rejects the entry-goal mismatch despite poisoned options",
      expected := false, actual := weakLink.ok },
    { name := "gate4+replay/replay independently judges the poisoned session's own \
      declarations kernel-sound (it checks soundness, not entry-goal matching, and is not \
      itself fooled by the poisoned option)",
      expected := true, actual := weakReplay.ok }
  ]

unsafe def replayChecks : IO (Array Check) := do
  return #[← replayPositiveCheck, ← replayCatchesBogusConstantCheck] ++ (← gate4WithReplayChecks)

/--
Full round-trip: run `decompose`, then confirm its output is actually *usable* -- declare stub
children with their real extracted types (as axioms; their own proof doesn't matter for this
check) directly via `Kernel.Environment.addDecl`, sidestepping pretty-printing entirely, then
elaborate the reassembly text against that environment and check it produces no errors. This is
the honest way to verify decompose's two outputs agree with each other: the lemma *types* and the
reassembly *text* were produced somewhat independently (one from `abstractSorry`'s returned
`Expr`, the other from splicing argument names as text), so only actually elaborating the
reassembly against real declarations of those exact types proves they match.
-/
unsafe def decomposeRoundTripOf (source : String) : IO (LeanKernel.Decomposition × Bool) := do
  let decomp ← LeanKernel.decompose source
  enableInitializersExecution
  -- The reassembly text still carries its own `import` line (spliced from the original source),
  -- so it must go through the same header-parsing route `decompose` itself uses -- not
  -- `Lean.Elab.process`, which expects an already-imported base and no header of its own.
  let inputCtx := Parser.mkInputContext decomp.reassembly "<decompose-roundtrip>"
  let (header, parserState, headerMessages) ← Parser.parseHeader inputCtx
  let (env, headerMessages) ← Lean.Elab.processHeader header {} headerMessages inputCtx
  -- `Lean.addDecl` (elaborator-level), not the raw kernel bypass Link.lean uses: that path goes
  -- through `Environment.ofKernelEnv`, which is documented as degrading the environment (drops
  -- notation/elaborator extensions) -- fine for auditing, fatal here, since processing the rest
  -- of the reassembly needs full elaborator support.
  --
  -- `levelParams` is derived from `ty` via `collectLevelParams`, not hardcoded `[]`: a lemma
  -- abstracted from a goal with a `Type*` binder has `abstractSorry`'s generalized level
  -- parameter baked into `ty` as a bare `Level.param`, and declaring it with `levelParams := []`
  -- produces "invalid reference to undefined universe level parameter" -- confirmed empirically
  -- on the real-Mathlib instance-implicit case below, which is exactly the class of case spec's
  -- Appendix C warns only surfaces on real Mathlib-dependent goals. `Decomposition.lemmas` only
  -- carries `(name, type)` (matching spec), so any real caller declaring one of these lemmas --
  -- not just this test -- needs to do the same derivation.
  let addStubs : CoreM Unit := decomp.lemmas.forM fun (name, ty) => do
    let levelParams := (Lean.collectLevelParams {} ty).params.toList
    Lean.addDecl (Declaration.axiomDecl { name, levelParams, type := ty, isUnsafe := false })
  let (_, coreState) ← addStubs.toIO mkCoreCtx { env }
  let frontendState ← Lean.Elab.IO.processCommands inputCtx parserState
    (Lean.Elab.Command.mkState coreState.env headerMessages {})
  let messages := frontendState.commandState.messages
  if messages.hasErrors then
    -- Kept unconditionally (not just while developing this test): a reassembly failure is
    -- otherwise a bare `false` with no way to tell why from the test output alone.
    IO.eprintln s!"decomposeRoundTripOf: reassembly failed to elaborate:\n{decomp.reassembly}"
    for (name, ty) in decomp.lemmas do
      let tyText ← ((Meta.ppExpr ty).run').toIO mkCoreCtx { env }
      IO.eprintln s!"  lemma {name} : {tyText.1}"
    for m in messages.toArray do
      IO.eprintln s!"  error: {← m.toString}"
  return (decomp, !messages.hasErrors)

unsafe def decomposeChecks : IO (Array Check) := do
  -- No local context to abstract: the goal itself is the whole statement.
  let (noCtx, noCtxOk) ← decomposeRoundTripOf
    "import Init\ntheorem parent1 : (1 : Nat) + 1 = 2 ∧ (2 : Nat) + 2 = 4 := by\n  \
     constructor\n  · sorry\n  · sorry"
  -- A local context (a hypothesis) that must be captured and correctly reapplied.
  let (withCtx, withCtxOk) ← decomposeRoundTripOf
    "import Init\ntheorem parent2 (n : Nat) (h : n > 0) : n + 1 > 1 := by\n  sorry"

  -- Real Mathlib content with an *anonymous* instance-implicit binder -- spec's Appendix C names
  -- this exact shape as only surfacing on real Mathlib-dependent goals, unlike the two synthetic
  -- cases above. `[Group G]` with no bound name is the normal Mathlib style, which is precisely
  -- what makes it a real test: Lean must auto-generate an inaccessible name for it.
  let (withInstance, withInstanceOk) ← decomposeRoundTripOf
    "import Mathlib.Algebra.Group.Defs\n\
     theorem group_test {G : Type*} [Group G] (a b : G) : a * b * b⁻¹ = a := by sorry"

  return #[
    { name := "decompose/no local context: extracts both sorries",
      expected := true, actual := noCtx.lemmas.size == 2 },
    { name := "decompose/no local context: reassembly round-trips against stub children",
      expected := true, actual := noCtxOk },

    { name := "decompose/with local context: extracts the one sorry",
      expected := true, actual := withCtx.lemmas.size == 1 },
    { name := "decompose/with local context: abstracted type is a function (hypotheses captured)",
      expected := true, actual := withCtx.lemmas.all fun (_, ty) => ty.isForall },
    { name := "decompose/with local context: reassembly round-trips against stub children",
      expected := true, actual := withCtxOk },

    { name := "decompose/anonymous instance-implicit (real Mathlib): extracts the sorry",
      expected := true, actual := withInstance.lemmas.size == 1 },
    { name := "decompose/anonymous instance-implicit (real Mathlib): reassembly round-trips",
      expected := true, actual := withInstanceOk }
  ]

/--
M1.8.1's `serve` dispatch logic (`LeanKernel.checkAgainst`/`handleLine`), tested directly against
the pure functions rather than through the actual stdin/stdout loop -- the loop itself was
verified empirically by hand (spawning `lake exe leankernel serve` and piping real JSON at it,
including against a real Mathlib-derived base environment; see CLAUDE.md), since a genuine
process/pipe round-trip isn't naturally exercised by `kernel_tests`' direct-call style the way
`Serve.lean`'s own request handling is.
-/
unsafe def serveChecks : IO (Array Check) := do
  enableInitializersExecution
  let baseEnv ← importModules #[{ module := `Init }] {} (loadExts := true)

  let clean ← LeanKernel.checkAgainst baseEnv "def foo : Nat := 5"
  let typeError ← LeanKernel.checkAgainst baseEnv "def bad : Nat := true"
  -- Isolation: a declaration from one `checkAgainst` call must never be visible to another,
  -- since unrelated agent attempts are checked against the very same warm `baseEnv` in sequence
  -- and must not be able to see each other's names.
  let isolated ← LeanKernel.checkAgainst baseEnv "def usesFoo : Nat := foo"

  -- A literal JSON string, not `toJson` on a `CheckRequest` value: production never serializes a
  -- `CheckRequest` from the Lean side (Python sends the line; `serve` only ever decodes one), so
  -- `CheckRequest` derives `FromJson` only -- adding `ToJson` would be surface area with no real
  -- caller, purely to make this one test line more convenient.
  let validRequest ← LeanKernel.handleLine baseEnv
    "{\"id\": \"req-1\", \"body\": \"def bar : Nat := 6\"}"
  let malformed ← LeanKernel.handleLine baseEnv "not json at all"

  return #[
    { name := "serve/clean check: ok with no diagnostics",
      expected := true, actual := clean.ok && clean.diagnostics.isEmpty },
    { name := "serve/type-error check: fails with a diagnostic",
      expected := true, actual := !typeError.ok && !typeError.diagnostics.isEmpty },
    { name := "serve/isolation: an earlier call's declaration is not visible to a later one",
      expected := true, actual := !isolated.ok },
    { name := "serve/handleLine: valid request echoes its id back and succeeds",
      expected := true, actual := validRequest.ok && validRequest.id == some "req-1" },
    { name := "serve/handleLine: malformed JSON has no id and fails",
      expected := true, actual := !malformed.ok && malformed.id == none }
  ]

end LeanKernelTests

unsafe def main : IO UInt32 := do
  initSearchPath (← findSysroot)
  let auditResults ← withImportModules #[{ module := `LeanKernelTests.AuditFixtures }] {}
    (fun env => LeanKernelTests.auditChecks env)
  let sealResults ← LeanKernelTests.sealChecks
  let linkResults ← LeanKernelTests.linkChecks
  let replayResults ← LeanKernelTests.replayChecks
  let decomposeResults ← LeanKernelTests.decomposeChecks
  let serveResults ← LeanKernelTests.serveChecks
  let checks := auditResults ++ sealResults ++ linkResults ++ replayResults ++ decomposeResults
    ++ serveResults
  let mut failures := 0
  for c in checks do
    if c.passed then
      IO.println s!"PASS  {c.name}"
    else
      IO.println s!"FAIL  {c.name}  (expected {c.expected}, got {c.actual})"
      failures := failures + 1
  IO.println s!"{checks.size - failures}/{checks.size} checks passed"
  return if failures == 0 then 0 else 1
