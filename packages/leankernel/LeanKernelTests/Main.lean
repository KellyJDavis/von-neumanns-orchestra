import Lean
import LeanKernel.Audit
import LeanKernel.Seal
import LeanKernel.Link
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

end LeanKernelTests

unsafe def main : IO UInt32 := do
  initSearchPath (← findSysroot)
  let auditResults ← withImportModules #[{ module := `LeanKernelTests.AuditFixtures }] {}
    (fun env => LeanKernelTests.auditChecks env)
  let sealResults ← LeanKernelTests.sealChecks
  let linkResults ← LeanKernelTests.linkChecks
  let checks := auditResults ++ sealResults ++ linkResults
  let mut failures := 0
  for c in checks do
    if c.passed then
      IO.println s!"PASS  {c.name}"
    else
      IO.println s!"FAIL  {c.name}  (expected {c.expected}, got {c.actual})"
      failures := failures + 1
  IO.println s!"{checks.size - failures}/{checks.size} checks passed"
  return if failures == 0 then 0 else 1
