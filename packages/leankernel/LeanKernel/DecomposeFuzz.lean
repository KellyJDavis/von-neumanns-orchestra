import Lean
import LeanKernel.Sorries
import LeanKernel.Audit

/-!
Phase 1 exit gate 7: "Decomposition round-trip on a Mathlib sample (hypothesis fuzz): children
link standalone, reassembly links against the parent, no new axioms."

Applied to an already-compiled declaration `origName` rather than to freshly-elaborated source
with an agent-introduced `sorry`: peel `origName`'s own Pi-type telescope into a synthetic
`SorryGoal` (its own binders as the captured local context, its own conclusion as the goal to
"sorry"), abstract that exactly as `decompose` would, then check the three things gate 7 names --
all settled by content that's already real and already proven, not synthesized:

1. **Children link standalone**: the original declaration's own value inhabits the abstracted
   type, checked by the kernel via `addDecl`, not assumed.
2. **Reassembly links against the parent**: reintroducing the same binders around the reassembly
   term reproduces something of exactly `origName`'s own type, again kernel-checked.
3. **No new axioms**: the reassembled development's audited axiom cone is exactly `origName`'s
   own (never more), since the child is nothing but a reference to `origName` itself.

Deliberately entirely at the `Expr`/kernel level -- no pretty-printing or source-text
round-tripping of `origName`'s own (arbitrarily complex, real Mathlib) type is needed, since the
telescope and the reassembly are both built and checked as `Expr`s directly. That sidesteps the
one real engineering risk source-text round-tripping would add (pretty-printing not always
round-tripping back through the parser) for exactly the part of `decompose` this gate needs to
stress at scale: `abstractSorry`'s binder-abstraction fidelity, across whatever diversity of real
binder shapes (instance-implicits, universe polymorphism, dependent types) a Mathlib sample
actually has.
-/

namespace LeanKernel

open Lean Meta

/-- Result of round-trip-testing decomposition against one already-compiled declaration. -/
structure DecomposeFuzzResult where
  name        : Name
  ok          : Bool
  diagnostics : Array String
  deriving ToJson

/-- `SorryGoal.range`/`.isTactic` are only used by `decompose`'s own text-splicing step (never by
`abstractSorry` itself, which reads only `goalType`/`lctx`/`suggestedName`) -- there is no source
text here at all, so these are unused placeholders, not real positions. -/
private def dummyRange : Syntax.Range := ⟨⟨0⟩, ⟨0⟩⟩

def decomposeFuzzOne (origName : Name) : MetaM DecomposeFuzzResult := do
  let env ← getEnv
  let some info := env.find? origName
    | return { name := origName, ok := false, diagnostics := #[s!"{origName}: not found in environment"] }

  forallTelescope info.type fun fvars conclusion => do
    let lctx ← getLCtx
    let sorryGoal : SorryGoal :=
      { goalType := conclusion, lctx, range := dummyRange, isTactic := false, suggestedName := `child }
    let (childName, childType, reassemblyTerm) ← abstractSorry sorryGoal
    let childLevelParams := (Lean.collectLevelParams {} childType).params.toList

    -- (1) Children link standalone: origName's own value inhabits the abstracted type.
    -- Level *arguments* here are positional against origName's own declared parameter list
    -- (`info.levelParams`), not against `childLevelParams` -- `mkConst` substitutes by position
    -- into whatever declaration it names, regardless of what the *caller's* own level parameters
    -- happen to be called.
    let childValue := mkConst origName (info.levelParams.map mkLevelParam)
    let childDecl := Declaration.thmDecl
      { name := childName, levelParams := childLevelParams, type := childType, value := childValue }
    let opts := Options.empty.setBool `debug.skipKernelTC false
    match Kernel.Environment.addDecl env.toKernelEnv opts childDecl with
    | .error ex =>
      let msg ← (ex.toMessageData opts).toString
      return { name := origName, ok := false, diagnostics := #[s!"child does not link standalone: {msg}"] }
    | .ok kenvWithChild =>
      -- (2) Reassembly links against the parent: close the reassembly term over the same
      -- telescope binders and check it has exactly origName's own type.
      let reassembledValue ← mkLambdaFVars fvars reassemblyTerm
      let reassembledName := origName ++ `__reassembled
      let reassembledDecl := Declaration.thmDecl
        { name := reassembledName, levelParams := info.levelParams, type := info.type,
          value := reassembledValue }
      match Kernel.Environment.addDecl kenvWithChild opts reassembledDecl with
      | .error ex =>
        let msg ← (ex.toMessageData opts).toString
        return { name := origName, ok := false,
                  diagnostics := #[s!"reassembly does not link against the parent: {msg}"] }
      | .ok kenvWithReassembly =>
        -- (3) No new axioms: reassembly's audited cone must not exceed origName's own.
        let origAxioms ← withEnv (Environment.ofKernelEnv kenvWithReassembly) (auditAxioms origName #[])
        let reassemblyAxioms ← withEnv (Environment.ofKernelEnv kenvWithReassembly)
          (auditAxioms reassembledName origAxioms.axioms)
        if reassemblyAxioms.ok then
          return { name := origName, ok := true, diagnostics := #[] }
        else
          let msg := s!"reassembly introduced axioms beyond origName's own: {reassemblyAxioms.axioms} vs {origAxioms.axioms}"
          return { name := origName, ok := false, diagnostics := #[msg] }

/-- A real, non-internal theorem sourced from a `Mathlib` module -- the population gate 7's
sample is drawn from. Excludes non-`Prop`-producing declarations (`decomposeFuzzOne` only makes
sense for a genuine theorem, not a `def`/`structure`/`instance`), compiler-generated auxiliary
declarations (`Name.hasMacroScopes`; equation lemmas and match auxiliaries are real top-level
constants in the environment but were never written by a person, so decomposing one tests
nothing about a real Mathlib development), and anything not sourced from a module literally named
`Mathlib...` (core/Init/Std declarations are exactly what the rest of `kernel_tests` already
covers Mathlib-free; this gate is specifically about the Mathlib-dependent cases M1.4's own
Appendix C note says only surface there). -/
def isEligibleForFuzz (env : Environment) (name : Name) (info : ConstantInfo) : Bool :=
  (info matches .thmInfo _) &&
  !name.hasMacroScopes &&
  ((env.getModuleIdxFor? name).map fun idx =>
    ((env.allImportedModuleNames.getD idx.toNat default).toString).startsWith "Mathlib"
  ).getD false

/-- Every eligible name currently in `env`, in whatever order `SMap.fold` happens to visit them --
`sampleNames` below is what actually randomizes the selection; this is just the population. -/
def eligibleNamesForFuzz (env : Environment) : Array Name :=
  env.constants.fold (init := #[]) fun acc name info =>
    if isEligibleForFuzz env name info then acc.push name else acc

/-- Pick `count` names from `pool` uniformly at random without replacement, seeded by `seed` for
reproducibility -- the same `seed` against the same `pool` always picks the same sample, so a
failure gate 7 finds can be reproduced exactly by rerunning with the same seed. `eraseIdx!` makes
each draw `O(pool.size)`, so this is `O(count * pool.size)` overall -- acceptable for the sample
sizes this has actually been run at so far; revisit with a swap-and-pop removal (`O(1)` per draw)
if scaling to the full 5,000-10,000-declaration gate run makes this measurably slow, not before.
-/
partial def sampleNames (pool : Array Name) (seed count : Nat) : Array Name :=
  go (mkStdGen seed) pool (min count pool.size) #[]
where
  go (gen : StdGen) (remaining : Array Name) (n : Nat) (acc : Array Name) : Array Name :=
    if n == 0 || remaining.isEmpty then
      acc
    else
      let (idx, gen') := randNat gen 0 (remaining.size - 1)
      go gen' (remaining.eraseIdx! idx) (n - 1) (acc.push remaining[idx]!)

/-- Aggregate result of running `decomposeFuzzOne` over a sample -- what gate 7's own report
needs: how many passed, and the full detail of anything that didn't (never just a count for
failures -- a gate that can fail silently on *which* declaration isn't one you can act on). -/
structure DecomposeFuzzReport where
  sampleSize : Nat
  passed     : Nat
  failures   : Array DecomposeFuzzResult
  deriving ToJson

def runDecomposeFuzz (env : Environment) (seed count : Nat) : IO DecomposeFuzzReport := do
  let sample := sampleNames (eligibleNamesForFuzz env) seed count
  let mut failures := #[]
  let mut passed := 0
  for name in sample do
    let result ← (decomposeFuzzOne name).run'.toIO' { fileName := "<gate7>", fileMap := default } { env }
    if result.ok then
      passed := passed + 1
    else
      failures := failures.push result
  return { sampleSize := sample.size, passed, failures }

end LeanKernel
