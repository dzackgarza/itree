# Feature specification for issue-owned Spec Kit execution

This is the durable product contract for how itree uses Spec Kit. The roadmap and its
work-unit issues own delivery, active plans, tasks, and progress. The requirements
below describe the intended integration. At the baseline revision, `itree next`
selects a work unit, `doctor` checks declared completion contracts, and `close` sends
the close request directly to GitHub. [Roadmap](https://github.com/dzackgarza/itree/issues/64) · [Baseline CLI](https://github.com/dzackgarza/itree/blob/f129a3e/src/itree/cli.py#L897-L918)

## User scenarios and testing

### US1. Hand unfinished work to a new session

A coordinator selects a ready issue. A contributor opens it by its qualified GitHub
reference and receives the full objective, acceptance conditions, governing documents,
current plan, tasks, decisions, and outstanding findings. Spec Kit can give the contributor
just the files needed for the current stage. Both people see the same work at different
levels of detail. This advances NS-002 by making the handoff recoverable from GitHub.

The first end-to-end demonstration is a round trip on one work unit. Open the issue
through `itree work OWNER/REPO#N`, run a stage that changes its plan, and confirm the
new plan on the issue. Discard the temporary directory. Before any code merges, open the
same issue from a fresh session. It must return the changed plan, the original acceptance
conditions, and the prior task state. If the GitHub write failed, the new session must
recover the last confirmed state and allow the incomplete stage to resume.
[Work-package contract](https://github.com/dzackgarza/itree/issues/65) · [Stage storage](https://github.com/dzackgarza/itree/issues/71)

### US2. Edit the same issue from two sessions

A planner edits the plan while an implementer completes a task. Both read the same
issue body. After the planner's final read, the implementer writes the task change and
receives confirmation. The planner then submits the plan based on its earlier read.

The confirmed task change must survive. The plan change must either appear alongside
it or produce an explicit conflict that leaves the planning stage incomplete. A fresh
read must show the original objective and acceptance conditions byte for byte. Repeat
the case with two incompatible edits to the same plan passage. Silent replacement
fails acceptance. Even a final reread can precede another writer's confirmed edit;
the write itself must preserve that edit or report a conflict. This protects the
handoff promised by NS-002.
[Concurrent-write acceptance case](https://github.com/dzackgarza/itree/issues/71#issuecomment-7078688738)

### US3. Select a work unit, then a task

A maintainer puts a blocked leaf before a ready sibling. `itree next OWNER/REPO` must
select the ready sibling. A blocker on a grouping ancestor must also prevent selection
of its local descendants. Within the selected issue, `itree next --within OWNER/REPO#N`
must respect explicit task prerequisites. For a task that requires two others, both
must be complete before it can be selected. When every task is checked, the selector
reports that no task is ready. The issue remains available for acceptance review.
This advances NS-001 by making each choice explainable. [Issue selection](https://github.com/dzackgarza/itree/issues/40) · [Task selection](https://github.com/dzackgarza/itree/issues/67)

One boundary case uses two deliverables, A and B. A's first task produces an input for
B's first task. B's result is then needed by A's second task. The tasks admit that
execution order. Converting both dependencies into whole-issue blockers would make A
and B block each other. The issue-local selector must identify a prerequisite outside
its task list as an error and leave native blockers untouched. A selector that can
discover the next task across issue boundaries requires a separate scheduling decision.
Changing where the tasks are stored would leave this dependency problem intact.

### US4. Decide whether the original request is complete

A reviewer receives an implementation issue with all tasks checked and a clean
`doctor` report. One original acceptance condition lacks evidence. The completion
check must leave the issue open, identify the missing result, and reconcile it with
existing unfinished tasks. Repeating the review must not create another task for the
same gap. Once independent evidence covers the full contract and its stated
contribution to a north-star goal, the reviewer can record the disposition and close
the issue as completed. The broader goal remains open.

For an example from mathematical work, lean-categories #226 distinguishes a usable
decomposition of the book's foundations from delivery of the mathematics. Its linked
tasks establish where the work belongs. Review of the cited declarations at the
accepted revision establishes whether their hypotheses and conclusions meet the
request. This distinction advances NS-003. [Completion review](https://github.com/dzackgarza/itree/issues/73) · [Foundations request](https://github.com/dzackgarza/lean-categories/issues/226)

### US5. Resume after a mandatory hook fails

On a real work unit, invoke a stage through a supported harness with a mandatory hook
that fails. Observe that the hook ran with the selected issue reference, the stage
remained incomplete, and the next dependent stage did not run. After correcting the
failure, discard local run state and resume in a fresh session. The issue must supply
the original contract and the correct stage. Confirmed stages must retain their results.
Continue through independent review of the work. This exercises recovery under NS-002
and the evidence required by NS-003. [Resume contract](https://github.com/dzackgarza/itree/issues/74) · [Hook and harness acceptance](https://github.com/dzackgarza/itree/issues/75)

## Requirements

| ID | Required behavior | Acceptance |
| --- | --- | --- |
| FR-001 | GitHub owns the active work package. Preserve the original contract and keep current requirements, plan, task IDs, prerequisites, and task state in the issue body. Link research, decisions, findings, and evidence through its comments. | US1, US2 |
| FR-002 | Materialize the selected issue in a temporary directory outside the repository. Reuse the upstream stage there, reconcile the result with live GitHub state, and confirm it before advancing. A fresh session selects work by issue reference. | US1, US2, US5 |
| FR-003 | Use native blockers for whole-work-unit prerequisites and tree preorder among eligible work units. Resolve internal prerequisites within their owning issue. Report a cycle or missing task by identifier. | US3 |
| FR-004 | Compare delivered work with every original acceptance condition and the claimed contribution to the project's goal. Identify the exact contract version, code revision, and evidence reviewed. An independent reviewer must be able to retrieve them after the issue changes. | US4 |
| FR-005 | Reuse the pinned upstream prompts, templates, harness adapters, and workflow engine. Add itree commands through an extension and adapt prompt or template behavior through a preset where needed. Bind mandatory hooks to an executing workflow step. | US1, US5 |
| FR-006 | Correct competing planning instructions at their source. Transfer active obligations to the owning issues with their task identities, order, prerequisites and evidence intact. | US1, US3 |

### Decision on storage and review

Committing a plan beside the code makes their shared revision easy to inspect. It also
makes recovery of an unmerged plan depend on finding its branch. For itree the handoff
must work from the issue alone. Keep current planning in the issue and bind the review
to the exact contract and code examined. A later reader must be able to recover the
reviewed wording even after the editable issue body changes. A link that resolves only
to its latest text cannot establish what the reviewer accepted. [Storage decision](https://github.com/dzackgarza/itree/issues/64)

### Decision on upstream reuse

Use the upstream extension and preset interfaces and run the adapted stages on the
upstream engine. The reuse record identifies which components have been inspected or
executed and which behaviors itree must supply. A mandatory hook registration can
return instructions for an agent. Acceptance requires observation of execution and its
effect on stage progression. The documented interfaces support this design without a
fork. Reconsider that decision only against a specific, demonstrated incompatibility.
[Upstream reuse record](https://github.com/dzackgarza/itree/issues/64#issuecomment-7060614683) · [Spec Kit customization](https://github.com/github/spec-kit/blob/v1.4.4/docs/how-to/customization.md)

## Success criteria

- **SC-001 (US1, US2).** With no earlier checkout or run state, a second contributor can
  recover the original request and every confirmed change from the issue. The late
  concurrent edit survives the exact interleaving in US2.
- **SC-002 (US3).** A maintainer can explain the selected work unit from native blockers
  and preorder, and the selected task from that issue's list. The interleaved case
  produces a diagnostic without changing either issue's blockers.
- **SC-003 (US4).** A reviewer can retrieve the contract and code examined, identify
  the evidence for each accepted obligation, and reproduce the decision to leave a
  partially satisfied implementation issue open.
- **SC-004 (US5).** A recorded run on a real issue identifies the upstream revision,
  adaptations, and harness used. It shows a mandatory hook stopping the stage, a
  successful resume from GitHub, and independent review before closure.

- **SC-005 (US1, US3).** After transfer, a fresh session can recover each open obligation and its previous evidence from the owning issue and select the required next task. Regenerating project instructions sends that session to the same workflow.

## Project adoption

Every project uses itree for work selection and Spec Kit for planning and executing
the selected issue. A vault can hold reusable knowledge. When a vault plan card,
harness instruction, or separate task list directs current work, correct its source. Transfer the original scope, outstanding tasks, completion
evidence and prerequisites to their work-unit owners before retiring a tracker.

The lean-categories order at revision `e40aeed` supplies a migration case. Its
feature task IDs and definition-first ordering carry requirements that the transfer
must preserve.
[M11](https://github.com/dzackgarza/lean-categories/blob/e40aeed54c2289d7a13db8b9014c5da49b2b9b83/.specify/memory/constitution.md#m11-definition-first-spec-kit-execution) · [Execution order](https://github.com/dzackgarza/lean-categories/blob/e40aeed54c2289d7a13db8b9014c5da49b2b9b83/specs/002-foundational-corpus-sweeps/tasks.md#repository-wide-execution-order)

Validate the issue-owned order against the same input state: genuine groundwork,
definition-bearing tasks, then independent proofs and delivery work. Preserve the
specified order within each band. A proof needed to construct a definition runs first;
an unrelated theorem waits. Where the issue-local selector cannot express a genuine
cross-issue task dependency, that is a defect in the common workflow. Repair it at
its owner and demonstrate the required sequence before declaring the transfer complete.
