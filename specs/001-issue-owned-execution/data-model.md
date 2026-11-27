# Issue-owned execution data model

The GitHub issue is the unit of recovery. Its body holds current state. Its comments
hold decisions and evidence that must remain readable when the current state changes.
The temporary files passed to Spec Kit represent one read of that issue.

| Record | Identity and contents | Rules |
| --- | --- | --- |
| Work unit | GitHub issue node ID and qualified issue reference. Record the current repository and number when resolving the reference. | Reuse itree's issue identity and existing work-unit classification. Resolve a transfer before writing. The caller must see which issue received the change. |
| Original contract | Exact objective and acceptance text, delimited separately from generated sections. Record the source, accepted clarifications or amendments, and the version examined. | Preserve original bytes when editing another section. Record authorized amendments explicitly. A generated requirement cannot narrow the original. Reject malformed, duplicate or ambiguous section boundaries before writing. |
| Current work | Requirements with stable issue-local IDs, clarifications, north-star contributions, plan, ordered tasks and review checklists. | A task has a stable ID, status, prerequisite IDs, requirement references, evidence references and a concrete contribution to a goal. Preserve IDs when reordering. Reviewers own checklist verdicts. |
| Stage attempt | Issue, stage, attempt ID, input contract and section fingerprints, outputs, hook outcomes and disposition. | Store the attempt's recoverable outcome on the issue. A referenced comment can hold the detail. Where a stage changes the body, bind its confirmation to the corresponding body result. A later valid edit supersedes the old result without erasing its receipt. |
| Review | Reviewer, issue, exact contract text examined, contract digest, full code commit ID with repository, evidence per condition, goal contribution and disposition. | Store the reviewed text in a retrievable issue comment or linked evidence comments and identify them by ID. Hash the exact UTF-8 bytes with SHA-256. A later review can retrieve the recorded bytes and check the digest even after a body edit. A missing or edited evidence record that cannot be verified invalidates that evidence. |

## Body and file mapping

Recognize the issue sections by explicit start and end delimiters outside code fences.
Read and replace only the selected range. Keep all other bytes, including user-authored
prose and whitespace. On intake, the existing objective is preserved when sections are
first added. The original contract remains an independent input to analysis and review.

| Working copy | Owning issue data |
| --- | --- |
| `spec.md` | Current requirements and acceptance scenarios. Resolved clarifications also cite their decision comments. |
| `plan.md` | Current implementation plan and links to design decisions. |
| `tasks.md` | Ordered task records, dependencies and evidence. A stable task reference pairs its owning issue with the local task ID. |
| `research.md`, `data-model.md`, `contracts/`, `quickstart.md` | Issue-specific research and design detail in linked comments. The issue body indexes the current result. |
| `checklists/` | Reviewer-owned requirements checks on the issue. A checklist result and an implementation task have different meanings. |

## Readiness and transitions

- For a task to be ready it must be unfinished and every named prerequisite must be
  complete. Choose the first ready task in the issue's declared order. Missing,
  external, duplicate or cyclic IDs must produce a diagnostic naming the affected
  tasks. An empty ready set cannot establish acceptance of the issue.
- A stage starts against a recorded issue version. A failed mandatory hook ends that
  attempt before its dependent step. Issue writes can be confirmed, rejected, or
  indeterminate. An indeterminate attempt requires a live read by attempt ID before a
  retry. A retry must preserve completed effects, including previously posted comments.
- Analysis with missing coverage returns to planning. Convergence or review with an
  unmet condition returns to implementation. Reuse the open task for the same
  obligation. A substantive amendment invalidates decisions that depended on the old
  contract. An earlier decision remains available with the version it examined.
- On resume, reconcile the issue receipts, current contract and outstanding findings.
  Reuse the upstream run if it agrees. With no local run, start the upstream workflow
  at the first unconfirmed applicable stage, using issue receipts to skip confirmed
  effects. The presence of a local file or a printed hook invocation cannot confirm a stage.

[Work-package layout](https://github.com/dzackgarza/itree/issues/65) · [Stage storage](https://github.com/dzackgarza/itree/issues/71) · [Task readiness](https://github.com/dzackgarza/itree/issues/67) · [Recovery](https://github.com/dzackgarza/itree/issues/74)
