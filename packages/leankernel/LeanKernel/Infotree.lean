import Lean

/-!
`InfoTree` traversal: locate `sorry` occurrences (tactic-mode and term-mode) with their
`LocalContext` and source position. See spec §4.6.

Adapted from `leanprover-community/repl`'s `REPL/Lean/InfoTree.lean` (Apache-2.0, same license as
this project), which solves exactly this problem for the REPL's own "sorries with their goal
states" reporting. Reused rather than reimplemented: `InfoTree` traversal has enough sharp edges
(synthetic vs. original syntax, tactic combinators nested around a real tactic, matching a
term-mode `sorry` to its expected type) that redoing it from scratch would just re-earn bugs this
already-deployed implementation has paid down.
-/

namespace LeanKernel

open Lean Elab

namespace Infotree

/-- The byte-offset range of a `Syntax`, for splicing source text (as opposed to a line/column
`Position`, which is for display). Falls back to an empty range at the start of the file for
synthetic syntax with no position at all, which should not occur for an explicit `sorry`. -/
def stxRange (stx : Syntax) : Syntax.Range :=
  stx.getRange?.getD { start := 0, stop := 0 }

/-- Is this `Syntax` an explicit invocation of the `sorry` tactic? -/
def isSorryTactic (stx : Syntax) : Bool :=
  s!"{stx}" = "(Tactic.tacticSorry \"sorry\")"

/-- Is this `Syntax` an explicit `sorry` term? -/
def isSorryTerm (stx : Syntax) : Bool :=
  s!"{stx}" = "(Term.sorry \"sorry\")"

/-- Analogue of `Lean.Elab.InfoTree.findInfo?`, but returns all matches. -/
partial def findAllInfo (t : InfoTree) (ctx? : Option ContextInfo) (p : Info → Bool)
    (stop : Info → Bool := fun _ => false) : List (Info × Option ContextInfo) :=
  match t with
  | .context ctx t => findAllInfo t (ctx.mergeIntoOuter? ctx?) p stop
  | .node i ts =>
    let info := if p i then [(i, ctx?)] else []
    let rest := if stop i then [] else ts.toList.flatMap (fun t => findAllInfo t ctx? p stop)
    info ++ rest
  | _ => []

/-- All `TacticInfo` nodes corresponding to explicit `sorry` tactic invocations, each paired with
its `ContextInfo`. -/
def findSorryTacticNodes (t : InfoTree) : List (TacticInfo × ContextInfo) :=
  let infos := findAllInfo t none fun i => match i with
    | .ofTacticInfo i => isSorryTactic i.stx && !i.goalsBefore.isEmpty
    | _ => false
  infos.filterMap fun p => match p with
    | (.ofTacticInfo i, some ctx) => (i, ctx)
    | _ => none

/-- All `TermInfo` nodes corresponding to explicit `sorry` terms, each paired with its
`ContextInfo`. Traversal stops descending past a `sorry` tactic node, since a term-mode `sorry`
inside a tactic block is reported via `findSorryTacticNodes` instead. -/
def findSorryTermNodes (t : InfoTree) : List (TermInfo × ContextInfo) :=
  let infos := findAllInfo t none
    (fun i => match i with | .ofTermInfo i => isSorryTerm i.stx | _ => false)
    (fun i => match i with | .ofTacticInfo i => isSorryTactic i.stx | _ => false)
  infos.filterMap fun p => match p with
    | (.ofTermInfo i, some ctx) => (i, ctx)
    | _ => none

/-- A located `sorry`: either a tactic-mode goal (identified by its `MVarId`) or a term-mode
placeholder (identified by its `LocalContext` and expected type). -/
inductive SorryType where
  | tactic (goal : MVarId)
  | term (lctx : LocalContext) (expectedType? : Option Expr)
  deriving Inhabited

/-- Every `sorry` occurrence in `t`, with the `ContextInfo` needed to run `MetaM` at that point
and the source range of the `sorry` itself. -/
def sorries (t : InfoTree) : List (ContextInfo × SorryType × Syntax.Range) :=
  (findSorryTacticNodes t |>.map fun (i, ctx) =>
    ({ ctx with mctx := i.mctxBefore, ngen := ctx.ngen.mkChild.1 }, .tactic i.goalsBefore.head!,
      stxRange i.stx)) ++
  (findSorryTermNodes t |>.map fun (i, ctx) =>
    (ctx, .term i.lctx i.expectedType?, stxRange i.stx))

end Infotree

end LeanKernel
