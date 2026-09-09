import Lean

/-!
`lean4checker` driver: replay a module's environment from its imports in a fresh kernel. See
spec §4.3.

`lean4checker` itself is deprecated -- merged into Lean as the built-in `leanchecker`
(`lake env leanchecker`) since v4.28.0. What both tools are built on, and what this file uses
directly, is the public `Lean.Environment.replay` primitive.
-/

namespace LeanKernel

open Lean

/-- Result of replaying the constants new to `env` (relative to a fresh, import-only base)
through the kernel (spec §4.3). -/
structure ReplayReport where
  ok           : Bool
  diagnostics  : Array String
  checkedCount : Nat
  deriving ToJson

/--
Replay every constant present in `env` but absent from `baseEnv` through the kernel, starting
from `baseEnv` -- which the caller must have reconstructed fresh from imports, never reused from
`env`'s own already-checked state.

This is what makes Replay meaningful on top of Link, not merely redundant with it. Link's kernel
check on `LeanAgent.__link` verifies exactly one declaration, and it does so by *trusting*
whatever type is already stored for `entry` (and anything `entry` depends on) in the ambient
environment -- the kernel does not re-derive `entry`'s type from its value at that point, since
that verification is assumed to have already happened when `entry` was first added. If the
agent's submitted development used metaprogramming to smuggle a declaration into the session
through some path that never actually went through genuine kernel type-checking (spec's
"environment hacking"), Link's check alone would be resting on that forged premise without
knowing it. Replay closes this gap by re-deriving *everything* new -- `entry`, anything it
depends on that isn't part of the trusted base, and `__link` itself -- from scratch against an
independently reconstructed environment, exactly as spec §4.3 describes.

Per spec, this does **not** re-check `baseEnv` itself (Mathlib, the sealed bundle): `.olean`
loading performs no kernel checking, so that trust rests on the read-only mount and image digest
covering the whole tree, not on replay.
-/
def replay (baseEnv env : Environment) : IO ReplayReport := do
  let newConstants : Std.HashMap Name ConstantInfo :=
    env.constants.fold (init := {}) fun acc n ci =>
      if baseEnv.constants.contains n then acc else acc.insert n ci
  try
    discard <| Environment.replay newConstants baseEnv
    return { ok := true, diagnostics := #[], checkedCount := newConstants.size }
  catch ex =>
    return { ok := false, diagnostics := #[toString ex], checkedCount := newConstants.size }

end LeanKernel
