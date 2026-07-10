# itree organization model

`itree` maintains one deterministic GitHub issue tree per repository and tells an agent the single next thing to do.
This file is the doctrine source; it ships as package data and `itree help model` prints it verbatim.

## Ontology (one screen)

```
root ledger        The single parentless issue anchoring the repo's work tree.
                   Titled `Ledger: ...`. A grouping issue, never a work unit.

grouping issue     A ledger, milestone, backlog, roadmap, or phase issue.
(milestone ledger) Orders work units. Not itself a unit of work.

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
MALFORMED         other E-findings (cycles, dup      itree doctor OWNER/REPO --explain CODE
                  roots, closed parent hiding work)
CLEAN_WITH_WORK   doctor OK, an open work unit       itree next OWNER/REPO  ->  work  ->  itree close
DONE              doctor OK, no open work units       stop, or itree new for genuinely new work
```

A repo can only advance to `next` once it is CLEAN. Fix structure first.

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
