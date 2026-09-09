import Lean
import LeanKernel.Audit

/-!
Seal: goal bundle validation. See spec §4.1.

The goal source is elaborated once into the environment (by whatever ran the enclosing
command -- a warm REPL in production, `withImportModules` plus a frontend run in tests).
`checkSealed` runs *after* that elaboration and decides whether sealing actually succeeded.

This split matters because Lean's own error recovery is deceptive here: a command that
reports an elaboration error still leaves a declaration in the environment, silently backed
by `sorryAx` (confirmed empirically against v4.33.1 -- an undefined-identifier error in a
bare `def` still produces a `#check`-able constant whose value is a sorry stub). Existence in
the environment is therefore *not* sufficient evidence that sealing succeeded; the message
log's error messages are the authoritative signal, and the zero-axiom check below is a
structural second line of defense that catches the same failure even if a caller forgot to
inspect diagnostics.
-/

namespace LeanKernel

open Lean

/-- Result of validating a sealed goal declaration (spec §4.1). -/
structure SealReport where
  decl        : Name
  levelParams : Array Name
  diagnostics : Array String
  ok          : Bool
  deriving ToJson

/--
Validate that `decl` was sealed successfully: elaboration logged no errors, and the resulting
declaration's axiom cone is empty. A goal is a bare type/proposition, not a proof -- it should
never carry `sorryAx` or any other axiom, whether from the agent's input or from Lean's own
error-recovery stub for a statement that failed to elaborate.

Universe parameters are read directly off the environment's `ConstantInfo` rather than
recomputed: Lean's top-level `def` elaboration already generalizes any free universe
metavariable into an explicit parameter (confirmed empirically -- this happens
unconditionally, not gated by `autoImplicit`), so by the time a declaration exists at all its
`levelParams` are already exactly what spec §4.2's Link needs for the arity check. A universe
metavariable that Lean's elaborator genuinely cannot generalize surfaces as an elaboration
error instead, which the diagnostics check below already catches.
-/
def checkSealed (decl : Name) : CoreM SealReport := do
  let diagnostics ← (← get).messages.toList.toArray.mapM (·.toString)
  let hasErrors := (← get).messages.hasErrors
  let env ← getEnv
  match env.find? decl with
  | none =>
    return { decl, levelParams := #[], diagnostics, ok := false }
  | some info =>
    let axiomReport ← auditAxioms decl #[]
    return {
      decl
      levelParams := info.levelParams.toArray
      diagnostics
      ok := !hasErrors && axiomReport.axioms.isEmpty
    }

end LeanKernel
