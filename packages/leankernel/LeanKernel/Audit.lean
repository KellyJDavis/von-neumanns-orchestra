import Lean

/-!
Axiom audit: `collectAxioms` against the run's allowlist. See spec §4.4.

Allowlist, never deny-list. Lean's native-evaluation tactics (`native_decide`, `bv_decide`)
generate a fresh, uniquely-named axiom per call site (e.g. `foo._native.native_decide.ax_1_1`
at v4.33.1, confirmed empirically -- the exact suffix is an implementation detail), so no
static deny-list of axiom names can ever catch them. `ok` below is decided purely by set
membership against the caller-supplied `allow` array, so any axiom outside it -- known name or
not -- is rejected by default rather than passed by accident.
-/

namespace LeanKernel

open Lean

/-- Per-declaration axiom-audit result (spec §4.4). -/
structure AxiomReport where
  decl              : Name
  axioms            : Array Name
  usesSorry         : Bool
  usesCompilerTrust : Bool
  ok                : Bool
  deriving ToJson

/-- The default allowlist (spec §4.4, §5.3), matching `leanprover-community/axiom-audit`. -/
def defaultAllowlist : Array Name := #[`propext, `Classical.choice, `Quot.sound]

/--
Audit `decl`'s full transitive axiom dependency cone against `allow`.

`usesSorry` and `usesCompilerTrust` are informational classifications only, never the
enforcement mechanism (`ok`). `sorryAx` is Lean's one stable, version-independent name for
`sorry`, so it is safe to name directly for reporting. `usesCompilerTrust` flags any axiom that
is neither `sorryAx` nor one of Lean's three intrinsic logical axioms (`propext`,
`Classical.choice`, `Quot.sound`) -- in practice this means native-evaluation axioms and
user-declared axioms, without needing to recognize their (often auto-generated, unstable) names.

Whether `sorryAx` itself is *permitted* is entirely up to whether the caller included it in
`allow` (i.e. `run.allow_sorry`); this function applies no special case for it.
-/
def auditAxioms (decl : Name) (allow : Array Name) : CoreM AxiomReport := do
  let axioms ← collectAxioms decl
  let standardLogicalAxioms : Array Name := #[`propext, `Classical.choice, `Quot.sound]
  return {
    decl
    axioms
    usesSorry := axioms.contains `sorryAx
    usesCompilerTrust := axioms.any fun ax => ax != `sorryAx && !standardLogicalAxioms.contains ax
    ok := axioms.all allow.contains
  }

end LeanKernel
