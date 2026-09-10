import Lean
import LeanKernel.Infotree

/-!
`InfoTree` → `SorryGoal` → `Decomposition`: walk sorry occurrences, abstract their local context
into binders, emit closed standalone statements plus a reassembly term. See spec §4.6.
-/

namespace LeanKernel

open Lean Elab Meta

/-- A located `sorry`, ready to be abstracted into a standalone lemma (spec §4.6). `range` is a
byte-offset range suitable for splicing the reassembly term back into the original source;
`isTactic` records whether it needs to be spliced back as a tactic (`exact ...`) or a bare term,
since the two occur in different syntactic positions. -/
structure SorryGoal where
  goalType      : Expr
  lctx          : LocalContext
  range         : Syntax.Range
  isTactic      : Bool
  suggestedName : Name
  deriving Inhabited

/-- Walk every `InfoTree` produced while elaborating a development and collect its `sorry`
occurrences, each with the local context and goal type needed to abstract it into a standalone
statement. A term-mode `sorry` with no recorded expected type (rare, but possible when
elaboration couldn't determine one) is skipped: there is nothing to abstract without a type. -/
def extractSorries (trees : PersistentArray InfoTree) : IO (Array SorryGoal) := do
  let mut result := #[]
  for t in trees do
    for (ctx, sorryType, range) in Infotree.sorries t do
      let suggestedName := Name.mkSimple s!"sorry_{result.size + 1}"
      match sorryType with
      | .tactic goal =>
        let (goalType, lctx) ← ctx.runMetaM {} do
          let decl ← goal.getDecl
          return (← instantiateMVars decl.type, decl.lctx)
        result := result.push { goalType, lctx, range, isTactic := true, suggestedName }
      | .term lctx expectedType? =>
        if let some ty := expectedType? then
          let goalType ← ctx.runMetaM lctx (instantiateMVars ty)
          result := result.push { goalType, lctx, range, isTactic := false, suggestedName }
  return result

/--
Abstract a sorry's local context into a standalone, closed statement.

Reuses `MVarId.revert` -- the same mechanism the `revert` tactic itself uses -- rather than
hand-rolling binder abstraction: `revert` already computes the correct dependency order via
`collectForwardDeps` (regardless of the local context's own declaration order) and already
preserves each hypothesis's original `BinderInfo`, including instance-implicit. Both are exactly
the properties spec §4.6 calls out as needing special handling, and `revert` gets them for free
because dependency-correct reverting *is* its entire job -- reimplementing binder abstraction by
hand would just be re-deriving what `revert` already does, with more room to get it wrong.

Returns the lemma name, the closed statement (any leftover universe metavariable generalized into
an explicit parameter, exactly as ordinary top-level declaration elaboration does -- confirmed
unconditional during M1.1's Seal work), and the term to substitute for this `sorry` when
reassembling the parent: the new lemma applied back to the original context's free variables.
-/
def abstractSorry (g : SorryGoal) : MetaM (Name × Expr × Expr) := do
  let mvar ← mkFreshExprMVarAt g.lctx {} g.goalType
  -- `revert`'s first return value is the fvars it *actually* reverted, in the order it reverted
  -- them -- not necessarily `g.lctx.getFVarIds` unchanged. In particular, a top-level `theorem`
  -- proved by tactic elaborates with an auxiliary "recursive reference to self" declaration
  -- prepended to its local context (confirmed empirically: a proof's own theorem name shows up
  -- as if it were a captured hypothesis); `clearAuxDeclsInsteadOfRevert` drops it instead of
  -- reverting it, so `revertedFVars` is the true, correct argument list and must be used for the
  -- reassembly application below -- reusing `g.lctx.getFVarIds` here reintroduces exactly that
  -- bogus argument.
  let (revertedFVars, mvarId') ← mvar.mvarId!.revert g.lctx.getFVarIds (preserveOrder := false)
    (clearAuxDeclsInsteadOfRevert := true)
  let abstractedType ← instantiateMVars (← mvarId'.getType)

  let mctx ← getMCtx
  let result := mctx.levelMVarToParam (fun _ => false) (fun _ => false) abstractedType `u 1
  setMCtx result.mctx

  let fvarIds := revertedFVars
  -- `result.newParamNames` is only the level parameters *this* generalization step introduced
  -- from metavariables -- it is not the child's full level-parameter list whenever `g.goalType`
  -- already mentions a concrete `Level.param` directly (confirmed empirically: a goal built from
  -- an already-compiled declaration's own type, as gate 7's decomposition-fuzz harness does, has
  -- no level metavariables at all for this step to generalize, yet `result.expr` still legitimately
  -- depends on that declaration's own universe parameter). A caller reusing only
  -- `result.newParamNames` for the child's declared `levelParams` would under-declare it, and a
  -- `reassemblyTerm` built from that same incomplete list -- as this used to do -- references the
  -- child with too few level arguments, rejected by the kernel with "incorrect number of universe
  -- levels parameters" the moment such a child is actually declared and applied. Deriving
  -- `levelParams` from `Lean.collectLevelParams` on the abstracted type itself, the same way
  -- `Sorries.lean`'s own `decompose` already tells *callers* of `Decomposition.lemmas` they must
  -- (see its docstring), keeps this function's own returned `reassemblyTerm` consistent with
  -- whatever declaration a correct caller builds from `result.expr` -- both are now derived the
  -- same way, from the same expression.
  let levelParams := (Lean.collectLevelParams {} result.expr).params.toList
  let reassemblyTerm := mkAppN (mkConst g.suggestedName (levelParams.map mkLevelParam))
    (fvarIds.map mkFVar)
  return (g.suggestedName, result.expr, reassemblyTerm)

/-- A decomposition of one development into standalone lemmas plus how to reassemble them
(spec §4.6). `Expr` has no `ToJson` instance, so unlike the other reports in this package this
one isn't JSON-derivable as-is; a caller crossing a process boundary would pretty-print each
lemma's type rather than serialize the `Expr` directly. -/
structure Decomposition where
  lemmas     : Array (Name × Expr)
  reassembly : String

/--
The shared half of decomposition: given an already-elaborated environment and its infotrees,
abstract every `sorry` into a standalone lemma and splice a reference to each back into `source`.

Factored out because there are two ways to reach this point and only one of them should exist
twice: `decompose` elaborates a complete module cold (its own `import` header), while
`decomposeWarm` elaborates a bare body against a base environment a `serve` worker already holds.
Everything that is actually delicate -- `revert`'s own reverted-fvar list, `@`-application,
hygiene-mangled instance binders, splicing back-to-front -- lives here, once.
-/
def decomposeElaborated (env : Environment) (trees : PersistentArray InfoTree)
    (source : String) (fileMap : FileMap) (fileName : String) : IO Decomposition := do
  let sorryGoals ← extractSorries trees
  let coreCtx : Core.Context := { fileName, fileMap }
  let mut lemmas : Array (Name × Expr) := #[]
  let mut splices : Array (Syntax.Range × String) := #[]
  for g in sorryGoals do
    let action : MetaM (Name × Expr × String) := do
        let (name, ty, reassemblyTerm) ← abstractSorry g
        -- Argument text is read off `reassemblyTerm`'s own application spine -- the fvars
        -- `abstractSorry` actually reverted -- rather than recomputed from `g.lctx.getFVarIds`
        -- independently. The two are *not* the same array: a top-level `theorem` proved by
        -- tactic carries an auxiliary "recursive reference to self" declaration in its raw local
        -- context that `revert`'s `clearAuxDeclsInsteadOfRevert` drops, and recomputing from
        -- `g.lctx` directly silently reintroduced it as a bogus extra argument (confirmed
        -- empirically: this produced `exact (sorry_1 parent1)`, applying the lemma to the
        -- *theorem itself*). Deriving from the term `abstractSorry` already built makes the two
        -- impossible to disagree.
        --
        -- An anonymous instance binder (Mathlib's normal style, e.g. `[Group G]`) gets a
        -- hygiene-mangled name from Lean with no valid surface syntax at all (confirmed
        -- empirically: splicing it verbatim produced a parse error, not just an unknown
        -- identifier) -- exactly the case flagged above and in spec's Appendix C. Since these
        -- are overwhelmingly instance arguments in practice, `‹Type›` (anonymous-instance
        -- lookup-by-type syntax) sidesteps the naming problem entirely: it finds the argument by
        -- searching the local context for something of the right type, never referencing its
        -- name. This covers instances; a non-instance hypothesis with a hygiene-mangled name
        -- (rare, but not provably impossible) would still need a real fix.
        -- Pretty-printing a captured type must run with `g.lctx` actually ambient, or any fvar
        -- the type itself mentions (e.g. `G` inside `Group G`) resolves to its raw internal name
        -- instead of its display name -- confirmed empirically (`‹Group _fvar.127›`, not
        -- `‹Group G›`) before adding `withLCtx` here.
        let argsText ← withLCtx g.lctx {} <| reassemblyTerm.getAppArgs.foldlM (init := "")
          fun acc arg => do
            let some decl := g.lctx.find? arg.fvarId! | return acc
            if decl.userName.hasMacroScopes then
              let tyText := toString (← Meta.ppExpr decl.type)
              return s!"{acc} (‹{tyText}›)"
            else
              return s!"{acc} {decl.userName}"
        return (name, ty, argsText)
    let ((name, ty, argsText), _) ← action.run'.toIO coreCtx { env }
    lemmas := lemmas.push (name, ty)
    -- `@`-prefixed: every reverted fvar (implicit `{G}`, instance `[Group G]`, and explicit
    -- alike) is spliced back positionally. Without `@`, ordinary application auto-inserts a
    -- metavariable for each implicit/instance parameter before consuming explicit arguments, so
    -- the supplied names land in the wrong slots entirely (confirmed empirically: `sorry_1 G
    -- ‹Group G› a b` elaborated as if `G` were `sorry_1`'s *first explicit* parameter, not its
    -- implicit one, shifting everything). Relying on elaboration to re-infer/re-synthesize the
    -- implicit and instance arguments instead of `@`-application was considered and rejected:
    -- reassembly should reproduce the *exact* captured context deterministically, not whatever
    -- typeclass search happens to find.
    let appText := s!"(@{name}{argsText})"
    let replacement := if g.isTactic then s!"exact {appText}" else appText
    splices := splices.push (g.range, replacement)

  -- Splice from the end of the source backwards, so earlier byte offsets stay valid as later
  -- (higher-offset) replacements are applied first.
  let sorted := splices.qsort (fun a b => a.1.start.byteIdx > b.1.start.byteIdx)
  let mut result := source
  for (range, replacement) in sorted do
    let before := (Substring.Raw.mk result {} range.start).toString
    let after := (Substring.Raw.mk result range.stop ⟨result.utf8ByteSize⟩).toString
    result := before ++ replacement ++ after
  return { lemmas, reassembly := result }

/--
Elaborate `source`, extract every `sorry`, abstract each into a standalone lemma, and splice a
reference to each lemma back into `source` in place of the `sorry` it replaced.

Text-splicing is safe here specifically because each replaced span is the `sorry` token itself
(a syntactic leaf), not some larger reconstructed expression -- there is no
precedence/parenthesization concern to get wrong, since a parenthesized application is valid
wherever a single term (or, prefixed with `exact`, a closing tactic) was expected. A captured
argument with a hygiene-mangled name (Mathlib's normal anonymous-instance style, e.g. `[Group G]`)
has no valid source-level identifier to splice in at all -- confirmed empirically, this isn't
merely cosmetic, it's a parse error -- so it's spliced as `‹Type›` (anonymous instance lookup by
type) instead of by name; see CLAUDE.md for what this does and doesn't cover.

Per spec §4.6, reassembly is a full acceptance check when it is later run -- link, replay, and
audit against the parent's sealed goal -- not merely a text-substitution exercise. `decompose`
only produces the reassembly source; it does not itself re-run the acceptance path on it.
-/
unsafe def decompose (source : String) (fileName : String := "<decompose>") : IO Decomposition := do
  enableInitializersExecution
  let inputCtx := Parser.mkInputContext source fileName
  let (header, parserState, messages) ← Parser.parseHeader inputCtx
  let (env, messages) ← Lean.Elab.processHeader header {} messages inputCtx
  let commandState := { Lean.Elab.Command.mkState env messages {} with infoState.enabled := true }
  let frontendState ← Lean.Elab.IO.processCommands inputCtx parserState commandState
  let finalState := frontendState.commandState
  decomposeElaborated finalState.env finalState.infoState.trees source inputCtx.fileMap fileName

/--
Elaborate `source` against an already-warm `baseEnv` and decompose it -- the same work
`decompose` does, minus the cold `import` handling, for a `serve` worker that already holds the
base environment (spec §4.1's warm-vs-cold measurement: cold per-child compilation was ~78% of
pipeline time against under 1% warm).

`source` therefore carries no `import` line of its own, exactly like a `check` body. The only
other difference from `Lean.Elab.process` (which `checkAgainst`/`sealGoal`/`linkSubmission` all
use) is `infoState.enabled := true`: infotrees are what `extractSorries` walks, and they are off
by default because building them is not free -- which is why this is a separate entry point
rather than something the other handlers turn on for everyone.

Returns the message log alongside the decomposition so a caller can tell "no sorries because the
development is complete" from "no sorries because it did not elaborate" -- two very different
outcomes that an empty `lemmas` array alone cannot distinguish.
-/
def decomposeWarm (baseEnv : Environment) (source : String)
    (fileName : String := "<decompose>") : IO (Decomposition × MessageLog) := do
  let inputCtx := Parser.mkInputContext source fileName
  let commandState :=
    { Lean.Elab.Command.mkState baseEnv {} {} with infoState.enabled := true }
  let frontendState ← Lean.Elab.IO.processCommands inputCtx {} commandState
  let finalState := frontendState.commandState
  let decomposition ←
    decomposeElaborated finalState.env finalState.infoState.trees source inputCtx.fileMap fileName
  return (decomposition, finalState.messages)

end LeanKernel
