import Init

/-!
Real, compiled "sealed goal" fixtures for `Link.lean`'s test suite (spec §4.2, gates 4-6).
Compiled normally as part of this package's own build, so `getModuleIdxFor?` sees them as
genuinely *imported* constants -- exactly what Link's shadow/redeclaration check requires, and
something an inline `def` in a dynamically-elaborated test string can never satisfy (confirmed
empirically: `getModuleIdxFor?` only tracks constants that came from a *different*, already
-compiled module; anything defined in the module currently being elaborated returns `none`).

`import Init`, not `import Lean`. Nothing here needs the Lean library -- these are two ordinary
declarations -- and importing it made every test that elaborates against this module carry a whole
extra copy of Lean's own environment. That was measured at several GiB of the `kernel_tests`
binary's peak footprint, which is what actually caused CI's `lean` job to be killed (exit 143,
"the runner has received a shutdown signal"). `import Init` is also the more faithful fixture: a
real sealed bundle imports its *base environment*, which is Mathlib or `Init`, never `Lean`.
-/

namespace LeanKernelTests.Goals

/-- A Prop goal, for the defeq-vs-textual and weakened-mutant tests (gate 5). -/
def G_add_zero : Prop := ∀ n : Nat, n + 0 = n

/-- A universe-polymorphic, data-producing goal (gate 6: arity matching, and "a Type-valued goal
links"). -/
def G_poly.{u} : Sort u := PUnit.{u}

end LeanKernelTests.Goals
