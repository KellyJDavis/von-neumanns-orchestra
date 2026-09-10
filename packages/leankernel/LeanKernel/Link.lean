import Lean
import LeanKernel.Audit

/-!
Kernel link: resolve the agent's entry declaration by name against the sealed goal and add it to
the kernel with forced options. See spec §4.2.
-/

namespace LeanKernel

open Lean

/-- Result of attempting to link `entry` against a sealed `goal` (spec §4.2).

`kernelOk` and `ok` are deliberately separate. `kernelOk` is the §4.2 question alone -- did the
kernel accept the constructed declaration, i.e. is the agent's term genuinely a term of the sealed
goal's type -- while `ok` additionally requires §4.4's axiom audit to pass. `verdict` stores these
as two independent columns (`link_ok`, `axiom_audit_ok`), and a proof the kernel accepted but whose
axiom cone falls outside the run's allowlist is a real and *informative* combination: the term is
correct, the trust base is not. Collapsing both into `ok` would report that as a link failure. -/
structure LinkReport where
  ok          : Bool
  kernelOk    : Bool
  diagnostics : Array String
  axiomReport : Option AxiomReport
  deriving ToJson

/--
Link `entry` against the sealed `goal`.

* The goal must be an *imported* constant from `expectedModuleIdx`, matching the module it was
  actually sealed into. Lean itself already refuses to redeclare an existing name outright
  (confirmed empirically: attempting to redefine an imported constant's exact name fails with
  "has already been declared" before this check would even run), so the practical threat this
  guards against is not an agent redeclaring the goal in-session, but the surrounding
  infrastructure resolving the name to the wrong module in the first place -- a stale cached
  bundle, a search-path ordering bug, or similar. `open`/`export` cannot redirect a fully
  qualified `Name` lookup either, since `Environment.find?` performs exact lookup, not
  scope-sensitive resolution; both are worth naming because the *absence* of a working exploit
  through them is precisely what makes this a non-issue rather than a gap.
* Universes are read from the sealed goal's own `levelParams`, never re-derived from the agent's
  declaration -- `entry` must be universe-polymorphic at exactly the goal's arity.
* The constructed declaration's *type* is fixed to the goal's own type (`mkConst goal lvlArgs`)
  and its *value* is the agent's term (`mkConst entry lvlArgs`). Kernel type-checking this forces
  `entry`'s real type to be definitionally equal to the goal's type -- this is what makes
  weakening structurally impossible: the agent never gets to state what it's proving, and a proof
  of a strictly weaker (or merely different) statement fails kernel type-checking, not because
  Link inspected anything, but because the declaration itself doesn't type-check.
* The kernel receives its own forced options (`debug.skipKernelTC` explicitly `false`), bypassing
  whatever `Lean.addDecl` would otherwise honor from ambient elaboration options -- options the
  agent's own submitted `set_option`s could have poisoned.

`LeanAgent.__link` is a scratch declaration purely so `auditAxioms` has something to inspect;
nothing depends on it existing afterward.
-/
def link (goal entry : Name) (expectedModuleIdx : ModuleIdx) (allow : Array Name) :
    MetaM LinkReport := do
  let env ← getEnv
  let some idx := env.getModuleIdxFor? goal
    | return { ok := false, kernelOk := false, axiomReport := none,
               diagnostics := #["goal constant absent (not an imported module)"] }
  unless idx == expectedModuleIdx do
    return { ok := false, kernelOk := false, axiomReport := none,
             diagnostics := #["goal shadowed or redeclared"] }
  let some goalInfo := env.find? goal
    | return { ok := false, kernelOk := false, axiomReport := none,
               diagnostics := #["goal missing"] }
  let some entryInfo := env.find? entry
    | return { ok := false, kernelOk := false, axiomReport := none,
               diagnostics := #["entry point missing"] }

  let lvls := goalInfo.levelParams
  unless entryInfo.levelParams.length == lvls.length do
    return { ok := false, kernelOk := false, axiomReport := none,
             diagnostics := #["entry is not universe-polymorphic at the goal's arity"] }
  let lvlArgs := lvls.map mkLevelParam
  let type := mkConst goal lvlArgs
  let value := mkConst entry lvlArgs

  let isProp ← Meta.isProp type
  let linkName := `LeanAgent.__link
  let decl := if isProp
    then Declaration.thmDecl { name := linkName, levelParams := lvls, type, value }
    else Declaration.defnDecl { name := linkName, levelParams := lvls, type, value,
                                hints := .opaque, safety := .safe }

  let opts := Options.empty.setBool `debug.skipKernelTC false
  match Kernel.Environment.addDecl env.toKernelEnv opts decl with
  | .error ex =>
    let msg ← (ex.toMessageData opts).toString
    return { ok := false, kernelOk := false, diagnostics := #[msg], axiomReport := none }
  | .ok kenv' =>
    -- `Environment.ofKernelEnv` is documented as producing a degraded environment ("should be
    -- temporary and not leak into elaboration") -- it drops elaborator extension state (parser
    -- tables, attributes, ...), keeping only what the kernel itself tracks. That's exactly
    -- enough for `auditAxioms` (which only walks kernel-level constant data), but this must
    -- never become the caller's ambient environment: `link` runs inside a shared warm-REPL
    -- session, so leaking either the degraded environment or the scratch `__link` declaration
    -- into it would corrupt later work in that session. `withEnv` scopes the swap to just this
    -- one call and restores the real environment immediately after.
    let axiomReport ← withEnv (Environment.ofKernelEnv kenv') (auditAxioms linkName allow)
    return { ok := axiomReport.ok, kernelOk := true, diagnostics := #[],
             axiomReport := some axiomReport }

end LeanKernel
