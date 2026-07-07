# itree

Deterministic traversal layer over GitHub sub-issue trees.

`itree` treats GitHub issues as nodes in a rooted, ordered tree and gives you a CLI to build, query, and validate that structure.
It is designed around **work-unit traversal**: keep a single ordered issue tree, ask for the next coherent work-unit issue, and track stories, plans, proof obligations, and implementation checklists inside that issue.

## Quick Start

```bash
# Create a root issue that defines the boundary of a problem domain
itree init owner/repo "Project Alpha"

# Add grouping or work-unit issues
itree add owner/repo#1 "Milestone: v1"
itree add owner/repo#2 "Implement editor preview sync"
itree add owner/repo#2 "Add export command proof"

# Find the next work-unit issue
itree next owner/repo#1
# => #3: Implement editor preview sync

# Close the work-unit issue when its acceptance criteria are satisfied
itree close owner/repo#3 --reason completed

# Find the next work-unit issue
itree next owner/repo#1
# => #4: Add export command proof

# Validate the tree structure
itree validate owner/repo#1
```

## Conceptual Model

### Sub-Issue Trees

GitHub has a native feature called **sub-issues** — you can attach an issue as a child of another issue.
`itree` takes this flat parent-child relationship and builds a full **rooted ordered tree** on top of it:

```
#1 Ledger: Project Alpha (open)          ← root of the traversal domain
├── #2 Milestone: v1 (open)              ← grouping issue
│   ├── #3 Editor preview sync (open)    ← work-unit issue
│   └── #4 Export command proof (open)   ← work-unit issue
├── #5 Backlog (open)                    ← grouping issue
│   └── #6 PDF import workflow (open)    ← work-unit issue
└── #7 Old experiment (closed)
```

Issue #3 can contain its own implementation checklist:

```markdown
## Acceptance Criteria
- Preview updates after document edits.
- The proof exercises the real preview boundary.

## Implementation Tasks
- [ ] Wire document change events.
- [ ] Add integration proof.
- [ ] Post the proof result as an issue comment.
```

Ordinary implementation tasks stay inside the work-unit issue body or issue comments.
They are not separate GitHub issues, and the PR is not the planning surface.

### Key Terms

- **Root issue**: The top-level issue that defines the boundary of a problem domain.
- **Grouping issue**: A ledger, milestone, backlog, roadmap, or phase issue used to order work units.
- **Work-unit issue**: A coherent review/proof boundary that can be implemented and reviewed through a PR.
- **Sub-issue**: An issue attached as a child of another issue.
  Use this only for grouping issues or separate work-unit issues, not for ordinary implementation tasks.
- **Preorder traversal**: Depth-first, left-to-right traversal of the tree.
  `next` uses this to find the next work-unit issue.
- **Tree violation**: A structural problem in the tree (e.g., duplicate reachable issues, open internal nodes with no open descendants).

### Reference Format

All commands accept issue references in the format `OWNER/REPO#NUMBER`:

```
owner/project-alpha#42
```

Repository references use `OWNER/REPO`:

```
owner/project-alpha
```

## Commands

### Structural Operations

Build and modify the tree:

| Command | Description | Example |
| --- | --- | --- |
| `init` | Create a root issue | `itree init owner/repo "Title"` |
| `add` | Create a child issue | `itree add owner/repo#1 "Child title"` |
| `attach` | Attach an existing issue | `itree attach owner/repo#1 owner/repo#5` |
| `detach` | Detach from parent | `itree detach owner/repo#1 owner/repo#5` |
| `move` | Reparent an issue | `itree move owner/repo#5 --under owner/repo#3` |

### Query Operations

Read the tree structure:

| Command | Description | Example |
| --- | --- | --- |
| `children` | List children | `itree children owner/repo#1` |
| `tree` | Dump full tree as JSON | `itree tree owner/repo#1` |
| `next` | Find next open work-unit issue | `itree next owner/repo#1` |
| `path` | Find path to an issue | `itree path owner/repo#5 --root owner/repo#1` |
| `validate` | Check tree invariants | `itree validate owner/repo#1` |

### Terminal Operations

Close issues:

| Command | Description | Example |
| --- | --- | --- |
| `close` | Close an issue | `itree close owner/repo#5 --reason completed` |

## Workflow

The typical workflow follows a **work-unit traversal** pattern:

1. **Organize**: Create one root ledger and attach grouping or work-unit issues beneath it.
2. **Scope**: Put acceptance criteria, proof obligations, and implementation checklists inside each work-unit issue.
3. **Traverse**: Use `next` to find the next open work-unit issue in preorder.
4. **Work**: Implement and prove the work-unit issue through its PR.
5. **Close**: Mark the work-unit issue as completed with `close`.
6. **Repeat**: Run `next` again to find the next work unit.
7. **Validate**: Use `validate` to check for structural problems (duplicates, dead-end nodes).

Create child issues only when the child is itself a separate work unit: independently valuable, independently reviewable, and carrying its own acceptance/proof boundary.
Ordinary implementation steps belong in the issue body or comments.

### Ordering Siblings

Use `--before` and `--after` with the `move` command to prioritize siblings:

```bash
# Place issue #5 before issue #3 (higher priority)
itree move owner/repo#5 --under owner/repo#1 --before owner/repo#3

# Place issue #5 after issue #3 (lower priority)
itree move owner/repo#5 --under owner/repo#1 --after owner/repo#3
```

### JSON Output

Most query commands support `--as-json` for machine-readable output:

```bash
itree children owner/repo#1 --as-json
itree next owner/repo#1 --as-json
itree tree owner/repo#1  # always JSON
```

## Validation

`itree validate` checks for:

- **Duplicate reachable issues**: The same issue appearing more than once under the root.
- **Dead open internal nodes**: An open issue with children, but none of its descendants are open.
  This indicates a parent that should likely be closed or have its live work moved.

Example output:

```json
[
  {
    "code": "dead_open_internal_node",
    "message": "open internal issue #5 has no open descendants",
    "issue_number": 5
  }
]
```

## Installation

### As a standalone script

```bash
# Run directly with uv (no install needed)
uv run tools/itree --help

# Or as a module
python -m tools.itree --help
```

### As a package

```bash
cd tools/itree
pip install -e .
itree --help
```

## Development

The project uses:

- **cyclopts** for CLI argument parsing
- **pydantic** for data validation and models
- **GitHub CLI (`gh`)** for API communication (requires `gh` authenticated)

### Running Tests

```bash
cd tools/itree
uv run pytest tests/ -v
```
