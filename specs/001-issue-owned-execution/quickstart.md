# Validate issue-owned execution

Use a disposable GitHub repository and a real open work-unit leaf under its root.
The issue needs an original contract with independently checkable acceptance conditions,
a north-star contribution, a plan and tasks. Give one task two prerequisites. Keep one
original condition deliberately unmet for the closure check. Use the implementation
revision under review with Python 3.14, uv, an authenticated GitHub CLI, the pinned
Spec Kit runtime, and the generated itree extension, preset and writer workflow.
The commands below exercise the planned integration when those components have landed.

## Record the starting point

Set `ITREE_ACCEPTANCE_REPO` to the repository slug, `ITREE_ACCEPTANCE_NUMBER` to
the issue number and `ITREE_SOURCE` to the absolute path of the itree implementation
checkout. Run these blocks from the fixture checkout in one shell. This gives the
command the fixture's governing documents while loading itree from the revision under
review. The fixture owner enrolls editors in the writer for the duration of the stage.

```sh
issue_ref="${ITREE_ACCEPTANCE_REPO}#${ITREE_ACCEPTANCE_NUMBER}"
itree_dev() { uv run --locked --project "$ITREE_SOURCE" itree "$@"; }
git -C "$ITREE_SOURCE" rev-parse HEAD
gh issue view "$ITREE_ACCEPTANCE_NUMBER" --repo "$ITREE_ACCEPTANCE_REPO" --json id,body,state,url
gh api --paginate "repos/$ITREE_ACCEPTANCE_REPO/issues/$ITREE_ACCEPTANCE_NUMBER/comments" --jq '.[].html_url'
itree_dev next "$ITREE_ACCEPTANCE_REPO"
itree_dev work "$issue_ref"
```

Record the baseline original-contract bytes, code commit, runtime pin and fixture
identity. The work package must include the live contract, governing goals, relevant
cited source content, decisions, tasks and findings. Compare actual content with its
source. A correct URL alone is insufficient for an input the agent must read.

## Handoff and recovery (US1, US5)

Use the pinned `specify` executable in a fixture initialized with its Claude
integration. Point
`ITREE_EXTENSION_DIR` and `ITREE_PRESET_DIR` to the packaged artifact directories.
These upstream commands install the resources and render the commands for each
harness. The product installer must also place the issue writer in the fixture's
default branch. Invoke each generated command with the actual issue reference.

```sh
specify integration install codex
specify integration install opencode
specify extension add "$ITREE_EXTENSION_DIR" --dev
specify preset add --dev "$ITREE_PRESET_DIR"
for harness in claude codex opencode; do
  specify integration use "$harness"
done
```

| Harness | Enter at the harness prompt |
| --- | --- |
| Claude Code | `/speckit-itree-work OWNER/REPO#N` |
| Codex | `$speckit-itree-work OWNER/REPO#N` |
| OpenCode | `/speckit.itree.work OWNER/REPO#N` |

1. Run a planning stage with a recognizable edit to a plan passage. Confirm it by
   reading the issue body with the command above. Before merging code, repeat
   `itree work` in a fresh checkout with no scratch files or workflow run state.
   Compare the plan, original contract, and task marks.
2. On another stage attempt, configure a mandatory hook whose command reports the
   selected issue and exits nonzero. Invoke the generated command. Observe actual
   execution with the issue reference, an incomplete attempt on GitHub, and no
   dependent-stage effects.
3. Correct the failing hook in the fixture. Resume the issue from a fresh session.
   Check the confirmed effects and attempt IDs in the issue and comments. Only the
   incomplete work should execute. Also exercise local engine resume with its previous
   run ID. Capture the harness, upstream revision, exit results, timestamps and GitHub
   evidence links. Accounts of generated files alone do not meet harness acceptance.

## Two writers (US2)

Use two sessions for the same issue. Let A prepare a plan edit from the baseline and
hold its proposal. Have B submit a task completion and wait for the writer's confirmation.
Submit A's original proposal without reloading the base. Read the live plan and task
from a third session. Compare the original contract byte for byte. Repeat with the
maintainer supplying B's edit through the comment interface. To submit a prepared
request or wake the writer explicitly, use:

```sh
gh issue comment "$ITREE_ACCEPTANCE_NUMBER" --repo "$ITREE_ACCEPTANCE_REPO" --body-file "$ITREE_PROPOSAL_FILE"
gh workflow run itree-issue-writer.yml --repo "$ITREE_ACCEPTANCE_REPO" -f issue_number="$ITREE_ACCEPTANCE_NUMBER"
gh issue view "$ITREE_ACCEPTANCE_NUMBER" --repo "$ITREE_ACCEPTANCE_REPO" --json body,comments
```

Set `ITREE_PROPOSAL_FILE` to the Markdown file containing the complete proposal
described in the [writer contract](contracts/issue-writer.md). The result must
include B's confirmed mark and either A's compatible plan change or an explicit
conflict with A's attempt still incomplete. Next have A and B replace the same passage
with different text. Confirm that the earlier accepted text survives. Replay a
confirmed request and check that it produces no second effect.

Exercise the tighter read/write race through the public CLI and a controlled GitHub
transport: pause a writer immediately after its final GET, start the other writer,
and observe that the second PATCH cannot pass it. Release the first writer and check
both accepted edits in a fresh read. Also interrupt after GitHub applies a PATCH but
before local confirmation; resume and check that the operation is recovered once.
Keep this deterministic transport evidence alongside the real two-session run.

## Readiness and independent closure (US3, US4)

```sh
itree_dev next "$ITREE_ACCEPTANCE_REPO"
itree_dev next --within "$issue_ref"
itree_dev audit "$issue_ref"
itree_dev close "$issue_ref" --reason completed
gh issue view "$ITREE_ACCEPTANCE_NUMBER" --repo "$ITREE_ACCEPTANCE_REPO" --json state,body,comments
```

Use a blocked local leaf followed by a ready sibling; repeat with a blocked grouping
ancestor. In the chosen issue, finish one prerequisite, then the other. The dependent
task becomes eligible only after both. Try a cycle and a task ID owned by another
issue. Each must name the offending tasks and leave native blockers intact.

With all tasks checked and the original condition still unmet, the close request must
fail and GitHub must still report `OPEN`. Run audit twice and inspect its references
to the same unfinished obligation. Complete the actual missing work and obtain
independent review at its full commit SHA. Repeat audit and close, then verify
`CLOSED` and retrieve the exact contract and evidence from the disposition. Edit an
unrelated body passage in the fixture and confirm the earlier review is still
reproducible. Publish acceptance evidence on the owning work unit during implementation.
