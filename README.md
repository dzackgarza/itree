# itree

Deterministic traversal layer over GitHub sub-issue trees.

`itree` treats GitHub issues as nodes in a rooted, ordered tree and gives you a CLI to build, query, and validate that structure.
It is designed around a **decomposition-then-traversal** pattern: break work into a tree, work on leaves, close them, and the tree progressively collapses.

## Quick Start

```bash
# Create a root issue that defines the boundary of a problem domain
itree init owner/repo "Project Alpha"

# Decompose into child issues
itree add owner/repo#1 "Frontend"
itree add owner/repo#1 "Backend"
itree add owner/repo#1 "Docs"

# Further decompose a child
itree add owner/repo#2 "Login page"
itree add owner/repo#2 "Dashboard"

# Find the next piece of work (first open leaf in preorder)
itree next owner/repo#1
# => #3: Login page

# Close a leaf when done
itree close owner/repo#3 --reason completed

# Find the next piece of work
itree next owner/repo#1
# => #4: Dashboard

# Validate the tree structure
itree validate owner/repo#1
```

## Conceptual Model

### Sub-Issue Trees

GitHub has a native feature called **sub-issues** — you can attach an issue as a child of another issue.
`itree` takes this flat parent-child relationship and builds a full **rooted ordered tree** on top of it:

```
#1 Project Alpha (open)          ← root of the traversal domain
├── #2 Frontend (open)           ← child
│   ├── #3 Login page (open)     ← leaf (no open children)
│   └── #4 Dashboard (open)      ← leaf
├── #5 Backend (open)
│   └── #6 API endpoint (open)
└── #7 Docs (closed)             ← leaf, closed
```

### Key Terms

- **Root issue**: The top-level issue that defines the boundary of a problem domain.
- **Sub-issue**: An issue attached as a child of another issue.
- **Open leaf**: An issue with no open children — the smallest undecomposed unit of work.
- **Preorder traversal**: Depth-first, left-to-right traversal of the tree.
  `next` uses this to find the next piece of work.
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
| `next` | Find first open leaf | `itree next owner/repo#1` |
| `path` | Find path to an issue | `itree path owner/repo#5 --root owner/repo#1` |
| `validate` | Check tree invariants | `itree validate owner/repo#1` |

### Terminal Operations

Close issues:

| Command | Description | Example |
| --- | --- | --- |
| `close` | Close an issue | `itree close owner/repo#5 --reason completed` |

## Workflow

The typical workflow follows a **decomposition-then-traversal** pattern:

1. **Decompose**: Break a large problem into a tree of smaller issues using `init` and `add`.
2. **Traverse**: Use `next` to find the next open leaf (smallest undecomposed unit of work).
3. **Work**: Implement the solution for that leaf.
4. **Close**: Mark the leaf as completed with `close`.
5. **Repeat**: Run `next` again to find the next leaf.
   The tree progressively collapses as leaves are closed.
6. **Validate**: Use `validate` to check for structural problems (duplicates, dead-end nodes).

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
  This indicates stalled decomposition.

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
