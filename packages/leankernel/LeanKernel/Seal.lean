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
declaration does not depend on `sorryAx`.

The `sorryAx` check is the point, and the error-log check alone cannot do it: a `sorry` in the
statement -- written by the submitter, or inserted by Lean's own error recovery for a statement
that failed to elaborate -- produces only a *warning*, so a contaminated goal would otherwise seal
cleanly and every proof against it would be meaningless.

This deliberately does **not** require the cone to be *empty*, which is what it demanded until
M2.10. That rule was wrong, and wrong in a way only real mathematics exposes: a goal's cone
includes the axioms behind the *definitions its type mentions*, so anything built on `Real` carries
`Classical.choice` and `Quot.sound`, and even `91 ^ 2 = 8281` carries `propext` through its `Monoid`
instance. Measured against real Mathlib goals, an empty-cone rule refuses to seal 8 of 13 miniF2F
statements this system can actually prove -- effectively excluding classical mathematics. It went
unnoticed for nine milestones because every earlier test sealed bare `Nat` statements, whose cones
happen to be empty.

What the statement's own definitions rest on is the *base environment's* trust question, settled by
read-only mounts and image digests (spec §4.3), not something a goal can be blamed for. The
*proof*'s axioms are a separate matter entirely and are audited at link time against the run's own
allowlist, which is where spec §4.4 puts them.

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
    -- A rejection here must say why. `hasErrors` already put its reason in `diagnostics`; a
    -- `sorry`-contaminated statement has only a warning to its name, so without this the caller
    -- would get `ok := false` and an empty `diagnostics` array and no way to tell what happened.
    let sorryDiagnostic :=
      if axiomReport.usesSorry then
        #[s!"sealed goal {decl} depends on sorryAx: a goal statement may not contain `sorry`"]
      else
        #[]
    return {
      decl
      levelParams := info.levelParams.toArray
      diagnostics := diagnostics ++ sorryDiagnostic
      ok := !hasErrors && !axiomReport.usesSorry
    }

end LeanKernel
