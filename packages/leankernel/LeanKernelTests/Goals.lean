import Lean

/-!
Real, compiled "sealed goal" fixtures for `Link.lean`'s test suite (spec §4.2, gates 4-6).
Compiled normally as part of this package's own build, so `getModuleIdxFor?` sees them as
genuinely *imported* constants -- exactly what Link's shadow/redeclaration check requires, and
something an inline `def` in a dynamically-elaborated test string can never satisfy (confirmed
empirically: `getModuleIdxFor?` only tracks constants that came from a *different*, already
-compiled module; anything defined in the module currently being elaborated returns `none`).
-/

namespace LeanKernelTests.Goals

/-- A Prop goal, for the defeq-vs-textual and weakened-mutant tests (gate 5). -/
def G_add_zero : Prop := ∀ n : Nat, n + 0 = n

/-- A universe-polymorphic, data-producing goal (gate 6: arity matching, and "a Type-valued goal
links"). -/
def G_poly.{u} : Sort u := PUnit.{u}

end LeanKernelTests.Goals
