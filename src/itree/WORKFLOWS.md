# itree organization model

`itree` maintains one deterministic GitHub issue tree per repository and tells an agent the single next thing to do.
This file is the doctrine source; it ships as package data and `itree help model` prints it verbatim.

## Ontology (one screen)

```
root ledger        The single parentless issue anchoring the repo's work tree.
                   Titled `Ledger: ...`. A grouping issue, never a work unit.

grouping issue     A ledger, milestone, backlog, roadmap, or phase issue.
(milestone ledger) Orders work units. Not itself a unit of work.

milestone ledger   A `Milestone: TITLE` grouping issue that is a DIRECT CHILD
                   of the root ledger. Its descendants are release-scoped and
                   carry native GitHub milestone TITLE.

backlog ledger     A sibling DIRECT CHILD branch of the root ledger. Its
                   descendants are unscoped and have no native GitHub milestone.

work unit          A coherent review/proof boundary that deserves one PR.
                   ALWAYS A LEAF. Acceptance criteria, proof obligations,
                   implementation checklists, and status all live in its
                   body or comments -- never in child issues.

sub-issue          A GitHub issue attached as a child of another. Legal only
                   under a grouping issue, only for a separate PR-sized unit.

preorder           Depth-first, left-to-right walk of the tree.
next               First open work-unit issue in preorder. The one next task.
```

## Repo state machine

`itree doctor OWNER/REPO` classifies the repo.
Route by state:

```
STATE             DETECTED BY                        ACTION
---------------   --------------------------------   ------------------------
NO_TREE           no root ledger (E001)              itree init OWNER/REPO "Ledger: ..."
FOREST            open issues unreachable from root  itree triage OWNER/REPO
                  (E010/E011)
ERROR             E-findings                          dispatch issue-itree-maintenance synchronously;
                                                      wait for its evidence before dependent work
WARNING           W-findings                          dispatch issue-itree-maintenance asynchronously;
                                                      continue substantive work
CLEAN_WITH_WORK   no E/W finding, open work unit      itree next OWNER/REPO  ->  work  ->  itree close
DONE              no E/W finding, no work units       stop, or itree new for genuinely new work
```

Severity controls maintenance timing, not whether work is possible.
An error requires a synchronous maintenance handoff before *dependent* work continues; a warning lets substantive work continue while maintenance runs asynchronously.
The maintenance agent is always the escape hatch.
It rereads live state, records the remediation ledger entry, repairs or routes the issue, and returns an evidence-backed disposition without substituting tree repair for substantive work.

## The four rails

Each rail is a refusal that keeps agents from the four ways issue trees rot.

### Rail 1 -- File, don't invent (`new`)

`new` without a placement creates nothing.
It shows where the work already fits.

```
$ itree new owner/repo "Cache the preview render"
Nothing was created. Fit the new item into existing work FIRST.

Open work units (2 = 2 pending PRs):
  #3 Editor preview sync
  #4 Export command proof

Grouping issues:
  #2 Milestone: v1

Less than one PR of work -> absorb it into a work unit:
  itree absorb --into owner/repo#3 --title "Cache the preview render" --body "..."
A full PR-sized unit (independently valuable, reviewable, own
acceptance criteria) -> create it under a grouping issue:
  itree new owner/repo "Cache the preview render" --under owner/repo#2 --body "..."
```

### Rail 2 -- Work units are leaves (`new --under` a work unit)

You cannot decompose a work unit into child issues.
Sub-tasks are body content.

```
$ itree new owner/repo "Wire change events" --under owner/repo#3
Refusing: #3 "Editor preview sync" is a work unit, and work units are leaves.
Implementation tasks belong in the work-unit issue body or comments.
If this item is part of that work unit, absorb it instead:
  itree absorb --into owner/repo#3 --title "Wire change events" --body "..."
```

### Rail 3 -- Absorb, don't fragment (`absorb`)

Sub-PR content merges into a work unit verbatim -- nothing summarized, nothing lost.
A source issue is cross-linked, detached, and closed as duplicate.

```
$ itree absorb owner/repo#31 --into owner/repo#14
Absorbed owner/repo#31 -> owner/repo#14
Next: itree doctor owner/repo
```

### Rail 4 -- Traverse, don't re-plan (`next` -> work -> `close`)

`next` names one unit and the standing instruction.
Do it, close it, ask again.

```
$ itree next owner/repo
Next work unit:
  #3 Editor preview sync

Instruction:
  Work from issue #3; keep planning state on that issue.
  Open the PR when implementation starts; synthesize its body from the issue.
  Keep implementation tasks in the issue body or issue comments.

$ itree close owner/repo#3 --reason completed
owner/repo#3
```

## Milestone orchestration

A release has two owners: a native GitHub Milestone owns release grouping, while its `Milestone: TITLE` grouping issue owns traversal order.
Create both through one command:

```
$ itree milestone owner/repo "v1" \
    --under owner/repo#1 \
    --issues owner/repo#8 owner/repo#13
owner/repo#21 milestone=4
```

Placement is mandatory.
Without `--under`, the command creates nothing, lists existing milestone ledgers and the root-ledger target, prints an exact command with placement, and exits nonzero.

With placement supplied, the command fetches the full tree and all open or closed GitHub Milestones before writing.
It accepts only the unique reachable root ledger as `--under`. A generated `Milestone: TITLE` ledger is its direct child; Backlog is a sibling branch, not a valid parent.
It rejects every other parent before any GitHub mutation, along with malformed tree state, an exact milestone or `Milestone: TITLE` collision, and duplicate or invalid work-unit leaves.
A successful preflight fixes this effect order:

1. Create the GitHub Milestone.
2. Create `Milestone: TITLE`, attach it beneath `PARENT`, and assign the milestone.
3. In CLI order, attach each parentless supplied work unit or replace the parent of each parented unit, then assign the milestone.

GitHub has no cross-resource transaction for these operations.
After mutation starts, `itree` stops at the first explicit rejection or indeterminate timeout, interruption, or unusable response.
It does not roll back, compensate, retry, silently reuse an object, or attempt the remaining suffix.
The error identifies the confirmed prefix, current outcome, untouched suffix, and each work unit's preflight-recorded parent, sibling position, and milestone assignment.
Recovery always begins with a live GitHub and tree reread.

## Diagnostic maintenance

Every error and warning in `itree doctor` protects an explicit ideal tree model, reports the observed deviation, and supplies a repair route.
Use `itree doctor OWNER/REPO --explain CODE` for that policy and dispatch the shipped `issue-itree-maintenance` prompt.
Its remediation ledger entry records the code, affected live objects, selected repair, dispatch timing, before/after evidence, and the substantive work unit that must remain in progress.

## Proportionality doctrine

The tree records PR-sized decisions, not task lists.
Keep it proportional:

- One work unit = one PR = one review/proof boundary.
  If a candidate is smaller than a PR, it is body content of an existing unit -- absorb it (Rail 3).
- If it is genuinely a separate, independently reviewable PR, it is a new work unit under a grouping issue (Rail 1), never a child of another work unit.
- `itree doctor` emits advisory `Q…` structure questions (e.g. a tree that is large relative to the codebase, or a unit with no acceptance criteria).
  They are prompts to reconsider proportion; they never change exit status.

The tree should be as small as the work is.
When in doubt, absorb.

## Deferred groupings (long-horizon shelves)

A milestone or backlog ledger may legitimately hold no work units yet, on purpose: far-future capabilities are often best left un-broken-down until prerequisite work lands, so premature breakdown does not churn.

Label such a grouping `deferred` (configurable via `deferral_label` in `~/.config/itree/config.toml`). `itree doctor` then reports it as informational `I010` ("deferred, awaiting breakdown") instead of warning `W030` (dead shelf).
An untagged empty grouping still warns as a stale shelf.
Because a deferred grouping has no open work units, `next` skips past it until its work is filed in.
