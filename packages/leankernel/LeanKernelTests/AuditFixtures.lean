import Lean

/-!
Adversarial declarations for `Audit.lean`'s test suite (spec §4.4 gates 2 and 3). Kept
Mathlib-free so this target builds fast enough to run on every commit.
-/

namespace LeanKernelTests.AuditFixtures

/-- Clean: no axioms beyond ordinary kernel reduction. -/
theorem clean : (2 : Nat) + 2 = 4 := by decide

/-- Native evaluation: generates a fresh, uniquely-named axiom per call site (confirmed
empirically against v4.33.1: `LeanKernelTests.AuditFixtures.usesNativeDecide._native.\
native_decide.ax_1_1`). This is exactly the case a name-based deny-list cannot catch. -/
theorem usesNativeDecide : (List.range 1000).sum = 499500 := by native_decide

/-- Uses `sorryAx`, Lean's one stable, version-independent axiom name for `sorry`. -/
theorem usesSorry : True := by sorry

/-- A user-declared axiom outside the standard logical set. -/
axiom customAxiom : True

theorem usesCustomAxiom : True := customAxiom

end LeanKernelTests.AuditFixtures
