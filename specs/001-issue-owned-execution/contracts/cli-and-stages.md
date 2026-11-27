# CLI and harness contracts

These are target interfaces for the issue-owned workflow. Each invocation names an issue
or repository explicitly. The project that owns the issue supplies the governing documents.

| Caller | Input | Observable result |
| --- | --- | --- |
| Coordinator | `itree next OWNER/REPO` | Select the ready work unit in repository preorder using the existing readiness rules, including grouping ancestors. Preserve the existing CLI and JSON behavior. |
| Contributor | `itree work OWNER/REPO#N` | Load the original objective, requirements, goals, plan, ordered tasks, prior decisions, findings and relevant cited source content. Identify missing or inaccessible required inputs. The initial read is non-mutating. The execution loop subsequently continues at the stage confirmed by the issue. |
| Implementation stage | `itree next --within OWNER/REPO#N` | Return the next ready task with its owning issue and task ID, or report no ready task or an invalid dependency. This query never changes an issue or a blocker. |
| Reviewer | `itree audit OWNER/REPO#N` | Compare the actual results with the full original contract and goal contribution. Record each finding and its evidence on the issue. Convergence reconciles each unmet condition to an existing or new task. Repeating it preserves one open obligation per gap. |
| Closer | `itree close OWNER/REPO#N --reason completed` | Verify the independent disposition for the contract and code revision under review. If an obligation is unmet or the review is stale, report the condition and leave the issue open. Record the evidence before closing and confirm the GitHub state. Other closure reasons retain their separate meanings. |
| Diagnostician | `itree doctor OWNER/REPO` | Retain structural and declared-contract diagnostics, including E016–E019. A clean report establishes only the checks doctor actually performs. |

## Harness and stage boundary

Register `speckit.itree.work` through one upstream extension. In Claude Code the
rendered command is `/speckit-itree-work`, in Codex it is `$speckit-itree-work`, and
in OpenCode it is `/speckit.itree.work`. Each accepts the qualified issue reference.
Use the upstream adapter for each harness and preserve its existing settings. The
session-start handler loads context when the issue is known. Invocation with an
explicit reference must resolve that issue even in an already running session.

Pass stage-specific harness and model choices through the pinned workflow's supported
inputs. Independent read or research steps may run in parallel and join at a review
gate. Each resulting edit still goes through the same issue writer. If the repository
has no approved north-star agenda, elicit the maintainer's goals and approval before
assigning an intake item a contribution. Preserve its original request during that step.

Materialize only the selected issue in a private temporary directory outside the
checkout. Pass its absolute path as `SPECIFY_FEATURE_DIRECTORY` and set
`SPECIFY_FEATURE_NO_PERSIST=1`. Adapt the upstream specify prompt's explicit feature
pointer write as well. The stage must leave the project's feature selection alone.
Reconcile the changed sections and record research or large evidence in comments.
Confirm the resulting issue data and its comment references before reporting success.

Dispatch registered mandatory hooks as executing upstream workflow steps with the
qualified issue reference. Capture the command's outcome. A missing executable or
nonzero result stops that stage; an optional hook's failure is recorded and the stage
may proceed. Preserve existing hook conditions and registrations. Dispatch each
applicable hook once per attempt through a single execution path.

## Recovery of external effects

An attempt may have posted a comment before a later body write fails, or GitHub may
have applied a write whose reply was lost. Return the issue, attempt, confirmed effects,
uncertain effect, and outstanding effects. On resume, read the body and relevant
comments by attempt ID. Reuse a confirmed effect. An unresolved result leaves the
stage incomplete. Posting evidence alone cannot advance the stage.

The race and admission conditions for issue-body mutations are specified in
[the research decisions](../research.md). The close operation must likewise revalidate
the reviewed contract under the applicable write boundary. Task marks, hook success
and a checked review form cannot substitute for the review record in the
[data model](../data-model.md).
