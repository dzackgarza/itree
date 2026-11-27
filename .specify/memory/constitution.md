# itree Constitution

This constitution governs changes to itree. The organization model defines the public
meaning of its commands. The contributor guide applies these principles to work in this
repository. [Organization model](../../src/itree/WORKFLOWS.md) · [Contributor guide](../../CONTRIBUTING.md)

## North-star goals

| ID | Long-term outcome | Basis for adoption |
| --- | --- | --- |
| NS-001 | A maintainer returning to a project can find the next unit of work they can start and explain its priority from the issue tree and its dependencies. | [Root ledger](https://github.com/dzackgarza/itree/issues/1) and [readiness contract](https://github.com/dzackgarza/itree/issues/40) |
| NS-002 | A contributor taking over an issue can recover the original request, decisions, and unfinished work from GitHub, even when the previous session is gone. | [Issue-owned workflow](https://github.com/dzackgarza/itree/issues/64) |
| NS-003 | A reviewer can trace each promised outcome to evidence, including work transferred to another repository, and tell when later changes require a new check. | [Completion contracts](https://github.com/dzackgarza/itree/issues/41) and [review rules](../../AGENTS.md) |

These goals date from the first edition. The maintainer delegated their initial wording.
Retain each identifier across revisions. Record approved amendments with their date and
source. Closing an issue changes the status of that work unit alone.

## Core Principles

### I. GitHub defines the order of work

Keep one ordered tree per repository. Grouping issues organize work. Each work unit is
a leaf with its own acceptance criteria. Use native blocking relationships to express
prerequisites. Use preorder to choose among eligible leaves. A change to readiness must
account for a blocked group as well as a blocked leaf. Task selection within a work unit
uses the prerequisites of that issue's tasks. A native issue blocker is justified only
when the dependency applies to the whole work unit. [Readiness contract](https://github.com/dzackgarza/itree/issues/40) · [Task selection](https://github.com/dzackgarza/itree/issues/67)

### II. Each request has an owner

The GitHub work-unit issue owns the original objective, acceptance conditions, current
requirements, plan, internal tasks, and their execution state. Keep clarifications,
research, and verification evidence discoverable from its body and comments. A new
session must recover unfinished work from the issue before any implementation has merged.
When work moves, record its origin and destination so a reviewer can follow the
obligation. Keep issue identity qualified by repository across transfers and links.
[Organization model](../../src/itree/WORKFLOWS.md)

### III. Structural changes must be observable

A command that needs placement must resolve it before a write. For a change that needs
several GitHub writes, specify what the caller can establish after each failure. For
example, if a milestone exists and its ledger could not be attached, the maintainer
must be able to identify the milestone and determine where to resume from live state.
[Milestone contract](https://github.com/dzackgarza/itree/issues/22)

### IV. Completion requires evidence for the original request

Map each acceptance condition to an observation at the boundary where the user sees the
result. A task checkbox records progress. A completion claim must identify the command
output, GitHub state, or other artifact that establishes the promised behavior. An open
obligation stays with its owner until the evidence satisfies it. Record the contract
version and code revision examined so another reviewer can reproduce the decision.
[Review rules](../../AGENTS.md)

### V. Use Spec Kit within the selected work unit

Use upstream Spec Kit prompts, templates, harness integrations, and workflow execution
for stages of the selected issue. itree supplies the issue context and records each
accepted result on GitHub. A stage that loses a concurrent edit or whose result cannot
be confirmed remains incomplete.

A repository with its own work-selection policy retains authority over that policy.
Adopting a different task order requires a decision in the repository that owns it.
The integration specification defines the boundary and its acceptance cases.
[Issue-owned execution](../../specs/001-issue-owned-execution/spec.md)

## Specification and review

For each work unit, state which goal it serves and how its result advances that goal.
Carry that explanation into the plan and each task. Specify the initial state, user
action, and observable result for each story. Before implementation, check that every
acceptance condition has a task and a way to verify it. At review, compare the actual
result with the original issue. Record outstanding work on that issue.
[Spec Kit workflow and storage](https://github.com/dzackgarza/itree/issues/64)

## Tests and measurements

Use the public command in tests of CLI behavior. For a reported defect, capture the
failure through the reported workflow, commit the failing regression, then verify the
same assertion after the fix. Tier 1 covers the common workflow and the branches that
change its result. Keep that feedback loop under five minutes. Use milestone runs and
CI for larger graphs and extended combinations.

Keep diagnostic logs and elapsed times for test and computation runs. Include setup,
fixtures, revision, environment, and input size so that runs can be compared. For a
timing problem, record cost per call, call count, total cost, priority, proposed change,
and measured cost after the change before selecting another optimization. Record
GitHub operations and their outcomes without exposing credentials.

## Governance

The maintainer approves changes to the north-star agenda. For an amendment, identify
the affected principle or goal, its source, and the effect on existing obligations.
Update the contributor guidance if the procedure changes. Review proposed changes
against this constitution and the repository review rules.

Increase the major version when an existing principle changes meaning or is removed.
Increase the minor version when adding a goal or principle. Use a patch version for
clarifications that preserve the obligation.

**Version**: 1.1.0 | **Ratified**: 2026-11-27 | **Last Amended**: 2026-11-27
