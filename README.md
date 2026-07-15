# itree

Deterministic traversal layer over GitHub sub-issue trees.

`itree` keeps one ordered issue tree per repository and tells an agent the single next work unit to do.
You keep stories, plans, proof obligations, and checklists *inside* each work-unit issue; `itree` handles structure and traversal.

The full organization model, repo state machine, guard rails, and proportionality doctrine live in [`src/itree/WORKFLOWS.md`](src/itree/WORKFLOWS.md) and print verbatim from `itree help model`.

## Install / invoke

Canonical invocation is via `uvx` straight from GitHub — no install step:

```bash
uvx --from git+https://github.com/dzackgarza/itree itree --help
uvx --from git+https://github.com/dzackgarza/itree itree next owner/repo
```

`itree` shells out to the GitHub CLI (`gh`), so `gh` must be installed and authenticated.
From a local checkout, use `uv run --with-editable . itree ...`.

## The loop

`itree` is built around one rails-guarded loop.
`doctor` classifies the repo, and each state routes to one command:

| State | Detected by | Do |
| --- | --- | --- |
| `NO_TREE` | no root ledger (E001) | `itree init owner/repo "Ledger: ..."` |
| `FOREST` | open issues unreachable from root (E010/E011) | `itree triage owner/repo` |
| `MALFORMED` | other E-findings (cycles, dup roots, hidden work) | `itree doctor owner/repo --explain CODE` |
| `CLEAN_WITH_WORK` | doctor OK, open work unit exists | `itree next` → work → `itree close` |
| `DONE` | doctor OK, no open work units | stop, or `itree new` for new work |

The four guard rails keep the tree proportional as you go:

1. **File, don't invent** — `new` without a placement creates nothing; it shows where the item already fits and prints the exact absorb/under commands.
2. **Work units are leaves** — `new --under` a work unit is refused; sub-tasks are body content, not child issues.
3. **Absorb, don't fragment** — sub-PR content merges into a work unit verbatim via `absorb`; nothing is summarized or lost.
4. **Traverse, don't re-plan** — `next` names one unit and the standing instruction; do it, `close` it, ask again.

See `itree help model` for each rail as a full transcript.

## Quick start

```bash
alias itree='uvx --from git+https://github.com/dzackgarza/itree itree'

# One root ledger defines the boundary of a problem domain.
itree init owner/repo "Ledger: Project Alpha"

# One command creates the native GitHub Milestone and its grouping ledger.
itree milestone owner/repo "v1" --under owner/repo#1
# => owner/repo#2 milestone=1

# Work units are PR-sized leaves under grouping issues.
itree new owner/repo "Editor preview sync" --under owner/repo#2
itree new owner/repo "Export command proof" --under owner/repo#2

# Ask for the next work unit; implement and prove it through its PR.
itree next owner/repo
# => #3 Editor preview sync

# Close it when its acceptance criteria are satisfied, then repeat.
itree close owner/repo#3 --reason completed
itree next owner/repo
# => #4 Export command proof
```

## Conceptual model

GitHub has a native **sub-issue** feature: you attach an issue as a child of another.
`itree` reads that flat parent-child relation and builds a **rooted ordered tree** on top of it:

```
#1 Ledger: Project Alpha (open)          ← root ledger, anchors the domain
├── #2 Milestone: v1 (open)              ← grouping issue
│   ├── #3 Editor preview sync (open)    ← work-unit issue (a leaf)
│   └── #4 Export command proof (open)   ← work-unit issue (a leaf)
├── #5 Backlog (open)                    ← grouping issue
│   └── #6 PDF import workflow (open)    ← work-unit issue
└── #7 Old experiment (closed)
```

A work-unit issue carries its own plan; sub-tasks never become child issues:

```markdown
## Acceptance Criteria
- Preview updates after document edits.
- The proof exercises the real preview boundary.

## Implementation Tasks
- [ ] Wire document change events.
- [ ] Add integration proof.
- [ ] Post the proof result as an issue comment.
```

### Key terms

- **Root ledger**: the single parentless issue anchoring the repo's work tree (titled `Ledger: ...`). A grouping issue, not a work unit.
- **Grouping issue**: a ledger, milestone, backlog, roadmap, or phase issue used to order work units.
- **Work-unit issue**: a coherent review/proof boundary implemented through one PR. Always a leaf.
- **Sub-issue**: an issue attached under another — legal only under a grouping issue, only for a separate PR-sized work unit.
- **Preorder traversal**: depth-first, left-to-right; `next` uses it to find the next open work unit.

### Reference format

Issue references are `OWNER/REPO#NUMBER`; repository references are `OWNER/REPO`:

```
owner/project-alpha#42
owner/project-alpha
```

## Commands

### Structural

| Command | Description | Example |
| --- | --- | --- |
| `init` | Create the root ledger issue | `itree init owner/repo "Ledger: ..."` |
| `new` | File an issue with guided placement | `itree new owner/repo "Title" --under owner/repo#2` |
| `milestone` | Create a GitHub Milestone and matching tree ledger | `itree milestone owner/repo "v1" --under owner/repo#1` |
| `absorb` | Merge an issue into a work unit, verbatim | `itree absorb owner/repo#31 --into owner/repo#14` |
| `attach` | Attach an existing issue | `itree attach owner/repo#1 owner/repo#5` |
| `detach` | Detach from parent | `itree detach owner/repo#1 owner/repo#5` |
| `move` | Reparent / reorder an issue | `itree move owner/repo#5 --under owner/repo#3` |

### Milestone orchestration

`milestone` keeps GitHub's release grouping and `itree` traversal grouping in sync:

```bash
itree milestone owner/repo "v1" \
  --under owner/repo#1 \
  --body "Ship v1." \
  --issues owner/repo#8 owner/repo#13
# owner/repo#21 milestone=4
```

`--under` is mandatory before any write.
Without it, the command creates nothing, lists existing milestone ledgers and the root-ledger target, prints an exact placed invocation, and exits nonzero.

`Milestone: TITLE` is stricter than an ordinary grouping child: it must be a direct child of the root ledger.
Backlog is its sibling branch.
Milestone-ledger descendants are release-scoped and use native GitHub milestone `TITLE`; Backlog descendants are unscoped and have no native GitHub milestone.
Use `itree help milestone` for the command-specific model.

With placement supplied, one preflight rejects malformed tree state, every non-root parent, exact milestone or ledger title collisions, and duplicate or invalid leaves.
Only then does the command create the GitHub Milestone, create and attach `Milestone: v1`, assign its milestone, and move each supplied work unit beneath it in argument order with the same assignment.
Parented work units use replace-parent semantics; parentless work units use attach semantics.

GitHub does not offer a cross-resource transaction.
After writes begin, `milestone` stops on the first rejected or indeterminate operation, performs no rollback or later operation, and reports the confirmed prefix, current outcome, untouched suffix, and preflight-recorded prior work-unit state.
Recovery starts by rereading live GitHub state.

### Query

| Command | Description | Example |
| --- | --- | --- |
| `children` | List children | `itree children owner/repo#1` |
| `tree` | Render the ordered tree (ASCII; `--json`) | `itree tree owner/repo` |
| `next` | Find the next open work-unit issue | `itree next owner/repo` |
| `path` | Find the path from root to an issue | `itree path owner/repo#5` |

### Diagnostic

| Command | Description | Example |
| --- | --- | --- |
| `triage` | Repair orphans one at a time | `itree triage owner/repo` |
| `doctor` | Check tree health and invariants | `itree doctor owner/repo` |
| `scan` | Account-wide health, one line per repo | `itree scan owner` |

### Terminal

| Command | Description | Example |
| --- | --- | --- |
| `close` | Close an issue | `itree close owner/repo#5 --reason completed` |

### Ordering siblings

Use `--before` and `--after` with `move` to prioritize siblings:

```bash
itree move owner/repo#5 --under owner/repo#1 --before owner/repo#3   # higher priority
itree move owner/repo#5 --under owner/repo#1 --after  owner/repo#3   # lower priority
```

### JSON output

Query and diagnostic commands accept `--json` for machine-readable output:

```bash
itree children owner/repo#1 --json
itree next owner/repo --json
itree doctor owner/repo --json
itree tree owner/repo --json
```

## Validation

`itree doctor` is the single validator.
It reports findings against a diagnostic catalog (`E…` errors, `W…` warnings, advisory `Q…` structure questions) covering: missing/multiple roots, a root not titled `Ledger:`, cycles, unreachable or parentless open issues, closed parents hiding open descendants, duplicate reachable issues, dependency edges, depth near GitHub's 8-level cap, work units decomposed into child issues, dead open grouping issues, milestone mismatches, missing acceptance criteria, and explicit completion-contract violations.

When GitHub parentage and native blockers cannot express a semantic implementation transfer, put a strict TOML `itree-contract` fence in the issue body:

```toml
kind = "implementation"
evidence = "routes"
owner = "#42"
requires = ["#7"]
revalidate_on = ["#84"]
completion = "completed"
```

The contract surface is intentionally small.
`kind` is one of `implementation`, `proof`, `research`, `audit`, or `coordination`; `evidence` is one of `routes`, `records`, `narrows`, or `discharges`. Implementation routes require an `owner`. Discharge evidence requires an `origin`. Unknown fields, invalid enum values, malformed TOML, or invalid refs are `E018` findings rather than silent skips.

Use `itree doctor owner/repo --explain CODE` for the meaning and repair routes of any finding code.
Warnings dispatch `issue-itree-maintenance` asynchronously while substantive work continues.
Errors dispatch it synchronously before dependent work continues; the maintenance agent is the escape hatch, not an absolute work stop.
For each handled finding, it appends one evidence-backed `## itree maintenance ledger` comment to the root ledger issue; `itree help maintenance` supplies the exact command and fields.
The shipped prompt is available through `itree help maintenance`.

## Development

The project uses **cyclopts** for CLI parsing, **pydantic** for models, and the **GitHub CLI (`gh`)** for API access.
Run the local quality gate with:

```bash
just test
```
