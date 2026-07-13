# issue-itree-maintenance

## Non-negotiable rules

- Reread the live GitHub issue tree and current doctor report before choosing a repair.
- Preserve the current substantive work unit.
  Tree maintenance records and repairs structure; it does not complete, replace, or close that work.
- Treat a warning as asynchronous maintenance.
  Treat an error as synchronous maintenance before dependent work continues.
  Neither is an unexplained terminal stop.
- Record an evidence-backed remediation ledger entry for every finding handled.

## Context

`itree` owns one ordered GitHub issue tree per repository.
The root ledger anchors the traversal domain.
Work units are PR-sized leaves.
Grouping issues organize work without becoming work units themselves.

Release and backlog ownership are separate root-child branches:

```text
Ledger
├── Milestone: TITLE -> release-scoped descendants with native milestone TITLE
└── Backlog -> unscoped descendants with no native milestone
```

Use `itree doctor OWNER/REPO --explain CODE` to retrieve the diagnostic's ideal model, observed deviation, repair routes, and maintenance timing.
Use the diagnostic catalog as the remediation policy; do not infer a repair from a warning title alone.

## Task

Heal the assigned `itree` finding while keeping the calling agent's substantive work visible and resumable.

## Procedure

1. Read the assigned finding, its `--explain CODE` output, the live tree, the affected issues, and their native GitHub milestone assignments.
2. State the protected ideal model, the observed deviation, and the smallest repair consistent with the diagnostic's remediation policy.
3. Create or update the remediation ledger entry with: finding code, affected issue references, selected repair, dispatch timing, current substantive work unit, and live evidence before repair.
4. Perform the structural repair, or route an owner-required repair with the exact live evidence and no invented workaround.
5. Reread the live tree and doctor report.
   Record after-state evidence and a disposition: repaired, routed with owner/action, or blocked by a concrete external authority boundary.
6. Return the calling agent to its substantive work.
   Do not present the ledger, a doctor invocation, or a repaired tree as completion of that separate work.

## Output

Return an evidence-backed disposition containing the finding code, ideal model, affected live objects, remediation ledger entry, repair or route, post-repair doctor/tree evidence, and the preserved substantive work unit.

## Reference skills

- `git-guidelines` for GitHub issue-tree mutations and checkpoints.
- `epistemic-integrity` for live-state evidence and any partial-read conclusion.
- `writing-for-agent-audiences` for the remediation ledger and handoff.
