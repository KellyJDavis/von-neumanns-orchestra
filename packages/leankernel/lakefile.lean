import Lake
open Lake DSL

package «leankernel» where
  testDriver := "kernel_tests"

require mathlib from git
  "https://github.com/leanprover-community/mathlib4" @ "v4.33.1"

@[default_target]
lean_lib «LeanKernel» where
  globs := #[.submodules `LeanKernel]

-- A default target, not just a declared one. `lake build` builds only default targets, and
-- `LeanKernel.Main`'s `.olean` being built as part of the `LeanKernel` glob above is not the same
-- thing as the binary being *linked* -- so without this, `.lake/build/bin/leankernel` never
-- existed in CI, and every Python test that spawns a real worker skipped there silently from
-- M1.8.2 until M2.1.1 noticed `tests/leanserv tests/eval` finishing in two seconds.
-- `kernel_tests` needs no such marking: `testDriver` above builds it as part of `lake test`.
@[default_target]
lean_exe «leankernel» where
  root := `LeanKernel.Main
  supportInterpreter := true

lean_lib «LeanKernelTests» where
  globs := #[.submodules `LeanKernelTests]

lean_exe «kernel_tests» where
  root := `LeanKernelTests.Main
  supportInterpreter := true
