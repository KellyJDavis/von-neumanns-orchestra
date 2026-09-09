import Lean
import LeanKernel.Serve

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

unsafe def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  match args with
  | "serve" :: imports =>
    LeanKernel.runServe (imports.map parseModuleName).toArray
  | _ =>
    IO.eprintln "leankernel: usage: leankernel serve [<import>...]"
    return 1
