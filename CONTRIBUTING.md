# Contributing to itree

Start with the work-unit issue and the [itree constitution](.specify/memory/constitution.md).
The original issue gives the acceptance conditions. The constitution gives the goals
that the contribution must serve.

## Using Spec Kit on an issue

**SPEC001 — Keep the current work in its issue.** Use the issue reference wherever a
Spec Kit template asks for the feature identity. Fill in the templates from the live
issue. Use the issue body for the current version and link to comments with detailed
evidence. Before writing back, reread the issue and reconcile intervening changes.
[Storage contract](https://github.com/dzackgarza/itree/issues/64)

| Stage | What belongs in the work-unit issue |
| --- | --- |
| Specify and clarify | User stories, acceptance scenarios, and the goal each story serves. Record answers and their source alongside the original request. |
| Plan | The proposed change, the decisions it requires, and how each acceptance condition will be verified. Cite research in a linked comment. |
| Tasks and checklists | Give each task an ID, prerequisites, and the requirement and goal it advances. Use a separate checklist to review the quality of the requirements. |
| Analyze | Compare the plan and tasks against every original acceptance condition. Record and resolve missing coverage before implementation. |
| Implement, converge, and review | Link evidence from the actual change. Reopen the relevant task when an obligation is unmet. Close the issue after review of the full contract. |

The checkout contains the shared Spec Kit templates and Bash tools. To read a template
without selecting a feature, run this from the repository root. Use `plan-template`,
`tasks-template`, or `checklist-template` in place of `spec-template` for later stages.

```sh
.specify/scripts/bash/resolve-template.sh spec-template
```

When a stage needs files, copy the relevant issue sections into a temporary directory
as `spec.md`, `plan.md`, and `tasks.md`. Set `SPECIFY_FEATURE_DIRECTORY` to its absolute
path and `SPECIFY_FEATURE_NO_PERSIST=1` for the stage. With those variables set, run
the prerequisite check from the repository root:

```sh
.specify/scripts/bash/check-prerequisites.sh --json --require-spec --require-tasks --include-tasks
```

On the project workstation, `~/ai/scripts/speckit-prerequisites` runs the same check
from the installed Specify CLI with the same arguments.

The checker confirms file availability. Read the files against the issue to verify
their content. Transfer results back to the issue and confirm the update before
discarding the working copy.

## Proving a change

**TEST001 — Exercise the user story.** For a readiness change, put a blocked leaf
before a ready sibling. Run `itree next` and assert which issue it names. Close the
blocker and assert which issue the same command now names. Setting a readiness flag
inside the test would skip the behavior under review. [Readiness contract](https://github.com/dzackgarza/itree/issues/40)

**GH001 — Verify the GitHub effect.** A successful write can leave a different
relationship than the one requested. For example, when a transferred issue has a
new parent, check the resulting parent relationship on GitHub using the qualified
issue reference. An exit status alone cannot establish that relationship.
[Reparenting after transfer](https://github.com/dzackgarza/itree/issues/56)

**DOC001 — Exercise a documented repair.** Start from the state that produced the
finding. For example, given a native milestone with no ledger, the W041 repair must
adopt that milestone and attach its ledger. Verify the original milestone number and
the attached ledger. [Milestone adoption](https://github.com/dzackgarza/itree/issues/52)

Use `just --list` for the repository checks and build commands.
