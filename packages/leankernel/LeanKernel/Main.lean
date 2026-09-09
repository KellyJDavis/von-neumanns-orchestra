import Lean
import LeanKernel.Serve
import LeanKernel.DecomposeFuzz

/-! `lake exe leankernel <subcommand>` entry point. See spec §3. Subcommands (seal, link, replay,
audit, decompose) land in Phase 1 alongside the modules they dispatch to. -/

open Lean

/-- A dotted module name (e.g. `Mathlib.Algebra.Group.Defs`) as a CLI argument, split on `.` and
folded into the hierarchical `Name` `importModules` expects -- not relying on a `String → Name`
stdlib helper, since none was confirmed to do exactly this without risking a different quoting
convention (e.g. escaping reserved-word components as `«...»`, which a plain module path never
needs). -/
def parseModuleName (s : String) : Name :=
  (s.splitOn ".").foldl Name.mkStr Name.anonymous

/-- Phase 1 exit gate 7's periodic/manual validation run (CLAUDE.md: "a periodic/manual exit-gate
run rather than something `lake test` runs on every commit" -- at the gate's own named scale
(5,000-10,000 declarations), one run costs on the order of 20-35 minutes, measured directly: 2,000
real Mathlib theorems took ~427s (~213ms/theorem, dominated by `collectAxioms`'s transitive
dependency walk, not a per-call inefficiency worth optimizing away for a periodic run). A small,
fast slice of the same check runs on every commit instead, as `kernel_tests`' own
`decomposeFuzzChecks`. -/
unsafe def runDecomposeFuzzCli (seed count : Nat) (imports : Array Name) : IO UInt32 := do
  let importArray : Array Name := if imports.isEmpty then #[`Mathlib] else imports
  let report ← withImportModules (importArray.map fun m => ({ module := m } : Import)) {} fun env =>
    LeanKernel.runDecomposeFuzz env seed count
  IO.println (toJson report).compress
  return if report.failures.isEmpty then 0 else 1

unsafe def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  match args with
  | "serve" :: imports =>
    LeanKernel.runServe (imports.map parseModuleName).toArray
  | "decompose-fuzz" :: seedStr :: countStr :: imports =>
    let some seed := seedStr.toNat?
      | IO.eprintln s!"leankernel: decompose-fuzz: '{seedStr}' is not a natural number seed"
        return 1
    let some count := countStr.toNat?
      | IO.eprintln s!"leankernel: decompose-fuzz: '{countStr}' is not a natural number count"
        return 1
    runDecomposeFuzzCli seed count (imports.map parseModuleName).toArray
  | _ =>
    IO.eprintln "leankernel: usage: leankernel serve [<import>...]"
    IO.eprintln "            leankernel decompose-fuzz <seed> <count> [<import>...]"
    return 1
