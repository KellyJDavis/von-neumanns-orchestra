import Lake
open Lake DSL

package «leankernel» where
  testDriver := "kernel_tests"

require mathlib from git
  "https://github.com/leanprover-community/mathlib4" @ "v4.33.1"

@[default_target]
lean_lib «LeanKernel» where
  globs := #[.submodules `LeanKernel]

lean_exe «leankernel» where
  root := `LeanKernel.Main
  supportInterpreter := true

lean_lib «LeanKernelTests» where
  globs := #[.submodules `LeanKernelTests]

lean_exe «kernel_tests» where
  root := `LeanKernelTests.Main
  supportInterpreter := true
