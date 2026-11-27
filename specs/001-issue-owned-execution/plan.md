# Implementation plan for issue-owned Spec Kit execution

| Branch | Design date | Product contract | Delivery owner |
| --- | --- | --- | --- |
| `main` | 2026-11-27 | [Issue-owned execution](spec.md) | [itree roadmap #64](https://github.com/dzackgarza/itree/issues/64) and its work units |

## Summary

Give a contributor a qualified issue reference and a fresh checkout. They can load the
original contract, run a Spec Kit stage and recover its confirmed result from GitHub
before code merges. The first delivery boundary is a planning-stage round trip. The
full workflow adds issue-local task selection, interruption and recovery, and a
completion decision tied to the reviewed contract and code revision.

The integration design in this directory is shared across the roadmap. Each delivery
issue owns its current plan, task ordering, progress, findings and acceptance evidence.

## Technical context

| Concern | Design |
| --- | --- |
| Runtime and dependencies | Python 3.14+, Cyclopts CLI, Pydantic models, NetworkX graph operations, GitHub CLI transport, uv project manifest and lock. Add the Spec Kit dependency at the roadmap's exact upstream Git revision when implementing #65. |
| State and identity | Work-unit body and comments, selected by qualified GitHub reference. Use issue node ID for identity at write time. Git contains code and durable design or proof artifacts. Materialize a temporary feature directory on each stage invocation. |
| Write authority | During a stage, **one writer per issue** applies body edits from contributors, maintainers and other automation. Use a repository-owned Actions workflow to drain durable proposals. Native GitHub edits between managed sessions remain possible. [Writer contract](contracts/issue-writer.md) |
| Execution | Pinned Spec Kit extension, preset, harness adapters and workflow engine. The shell or command step executes mandatory hooks. Bind each attempt, hook, and receipt to its issue. |
| Scope | Existing CLI and supported agent harnesses. Work-unit choice retains itree's repository preorder. Task choice is local to the selected issue. The lean-categories M11 policy remains owned by that repository. |
| Evidence and scale | Use the existing CLI test boundary for small graphs, errors, interrupted writes and repeatable interleavings. Paginate issue comments; move large research and evidence into indexed comments. Use real GitHub, the writer workflow, and real harness sessions at the delivery milestone. Tier 1 must stay under five minutes. No product latency objective is specified. |

## Constitution check

| Principle | Before research | Design decision after research |
| --- | --- | --- |
| I. Work selection | Preserve existing ready work-unit selection (NS-001). | Use the native issue graph and grouping-ancestor checks. An external task prerequisite is an issue-local error; leave native blockers intact. |
| II. Ownership | Recover unfinished work from the issue (NS-002). | Preserve original contract bytes, current sections and confirmed stage receipts on GitHub. Render scratch files for upstream stages and reload from the issue after interruption. |
| III. Observable changes | Make partial or concurrent mutations identifiable (NS-002). | Serialize all participating body editors, compare section bases, record intended and confirmed effects, and stop on conflict or uncertainty. The maintainer selected this editing policy for the active stage. |
| IV. Acceptance | Preserve every original obligation until independent evidence establishes it (NS-003). | Record the exact contract, code SHA, references and disposition. A stale review, missing evidence, or failed verification leaves the issue open. |
| V. Upstream execution | Make stage and hook results visible to the next session (NS-002, NS-003). | Pin the examined runtime, run mandatory hooks, and reconstruct stage state from confirmed issue receipts. Preserve external repositories' own selection policies. |

The design satisfies these planning gates under the single-writer policy. Runtime
acceptance depends on observing the cases below. A bypassed writer, unconfirmed body
change, or unexecuted mandatory hook fails its gate. [Constitution](../../.specify/memory/constitution.md)

## Project structure and ownership

- Extend `src/itree/cli.py` at the existing selection, diagnosis and closure entry
  points. Keep parsing and I/O outside pure readiness and audit operations.
- Reuse issue references and graph models in `src/itree/models.py`, GitHub transport
  in `src/itree/github.py`, and selection in `src/itree/readiness.py`. Add the
  issue-body section codec and stage receipt handling at the work-package boundary.
  Preserve arbitrary unowned body bytes and existing `itree-contract` fences.
- Extend `src/itree/audit.py` and use existing declarations from
  `src/itree/contracts.py` for the semantics they already express. Add review evidence
  and task prerequisites at their own owners; preserve existing doctor findings.
- Ship the extension, preset, workflow and consumer writer definition as itree package
  resources. The installer renders upstream harness files and installs
  `.github/workflows/itree-issue-writer.yml` in a participating repository. Make
  installation repeatable, preserve existing harness settings and hooks, and identify
  collisions before overwriting a file. Implement there only after the design is
  approved for delivery. No writer is deployed by this plan.

## Phase 0. Research decisions

The [research record](research.md) gives the exact upstream pin, alternatives, source
links and limitations. GitHub's documented REST and GraphQL issue updates expose no
expected body version. The selected writer serializes the final read, merge, update,
and confirmation across sessions. Parallel planning is still possible. The upstream
package supports scratch directories and local workflow resume. Recovering state on
another machine requires issue-owned attempt receipts.

## Phase 1. Design and delivery sequence

The following boundaries give the implementation order recorded by the roadmap.
Use the native blockers to determine which work unit can run. Ordering in this table
records how the design fits together; the individual issues carry execution state.

| Work units | Decision and evidence |
| --- | --- |
| [#65](https://github.com/dzackgarza/itree/issues/65), [#71](https://github.com/dzackgarza/itree/issues/71) | Load the full original contract, goals, cited content, decisions and open findings. Establish the issue layout, writer protocol, no-change round trip, conflicting edits and uncertain-write recovery. A separate session must see each confirmed result. |
| [#68](https://github.com/dzackgarza/itree/issues/68), [#72](https://github.com/dzackgarza/itree/issues/72), [#69](https://github.com/dzackgarza/itree/issues/69) | Render one extension through all required harness adapters. At intake place an independent unit under a grouping or absorb work into its owner. Adapt specify, clarify, plan and tasks to the selected issue. Demonstrate the first planning-stage handoff through a real generated command. |
| [#70](https://github.com/dzackgarza/itree/issues/70), [#66](https://github.com/dzackgarza/itree/issues/66), [#73](https://github.com/dzackgarza/itree/issues/73) | Load the approved agenda and require each contribution to name its mechanism. Analyze coverage of the original contract before implementation. Converge against actual evidence, reconcile repeated findings and gate completion close on independent review. |
| [#67](https://github.com/dzackgarza/itree/issues/67), [#74](https://github.com/dzackgarza/itree/issues/74), [#75](https://github.com/dzackgarza/itree/issues/75) | Select a ready internal task. Drive the upstream workflow through analysis, implementation, convergence and review. Record stage attempts, execute mandatory hooks, and reproduce a failed stage and its recovery on a real issue. |

## Acceptance by requirement

| Requirement | Observable result | Design and validation |
| --- | --- | --- |
| FR-001 | A fresh session retrieves the original contract and all confirmed issue state before merge. | [Data model](data-model.md), [handoff procedure](quickstart.md#handoff-and-recovery-us1-us5); US1, US2, SC-001 |
| FR-002 | A late task edit survives a plan write. A conflict or uncertain result leaves the stage incomplete. A resumed attempt reuses confirmed effects. | [Writer contract](contracts/issue-writer.md), [two-writer run](quickstart.md#two-writers-us2); US1, US2, US5, SC-001, SC-004 |
| FR-003 | Global and within-issue selection produce the expected ready leaf and task. The interleaved cross-issue example leaves native blockers untouched. | [CLI contract](contracts/cli-and-stages.md), [selection checks](quickstart.md#readiness-and-independent-closure-us3-us4); US3, SC-002 |
| FR-004 | Independent review can recover the exact contract and code examined. Checked tasks and a clean structural audit leave an unmet issue open. | [Review record](data-model.md), [closure checks](quickstart.md#readiness-and-independent-closure-us3-us4); US4, SC-003 |
| FR-005 | Each real harness loads the issue. A mandatory hook failure stops its stage. A fresh checkout resumes from the issue and reaches independent review. | [Harness contract](contracts/cli-and-stages.md), [harness run](quickstart.md#handoff-and-recovery-us1-us5); US1, US5, SC-004 |

For each evidence run, retain the environment, upstream and code revisions, fixture
identity, elapsed setup and execution times, and the observable effects. Run longer
real-harness and concurrency cases at milestones. Apply timing triage to measured
costs before changing the design for performance.
