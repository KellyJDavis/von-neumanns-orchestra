import Lake
open Lake DSL

package «leankernel» where

require mathlib from git
  "https://github.com/leanprover-community/mathlib4" @ "v4.33.1"

@[default_target]
lean_lib «LeanKernel» where
  globs := #[.submodules `LeanKernel]

lean_exe «leankernel» where
  root := `LeanKernel.Main
