/-! `lake exe leankernel <subcommand>` entry point. See spec §3. Subcommands (seal, link, replay,
audit, decompose) land in Phase 1 alongside the modules they dispatch to. -/

def main (_args : List String) : IO UInt32 := do
  IO.eprintln "leankernel: no subcommands implemented yet (Phase 0 scaffold)"
  return 1
