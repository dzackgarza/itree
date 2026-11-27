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
account for a blocked group as well as a blocked leaf. [Readiness contract](https://github.com/dzackgarza/itree/issues/40)

### II. Each request has an owner

Keep the original objective and its acceptance conditions in the work-unit issue. Keep
clarifications, plans, tasks, and verification evidence discoverable from that issue.
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
obligation stays with its owner until the evidence satisfies it. [Review rules](../../AGENTS.md)

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

**Version**: 1.0.0 | **Ratified**: 2026-11-27 | **Last Amended**: 2026-11-27
