---
description: "Load one exact live itree work package by qualified issue reference"
scripts:
  sh: .specify/extensions/itree/scripts/python/work_loader.py
argument-hint: "OWNER/REPO#N"
---

# Load the live issue package

Use exactly one live `$ARGUMENTS` value in the form `OWNER/REPO#N`. Reject any other argument before running a command. Do not use a feature pointer, a local `specs/` directory, session-start output, a prior invocation, or a cached package.

1. Run `{SCRIPT} prepare "$ARGUMENTS" --deadline-ms 600000` as one tool call. Record the returned opaque `invocation_id`. A nonzero result is a failure: stop without summarizing, retrying, selecting another issue, or finishing. An exit-2 partial package is readable diagnostic content, not a complete package. An exit-3 read failure has no package.
2. For that exact ID, run `{SCRIPT} next INVOCATION_ID` exactly once per returned chunk. Substitute only the ID from step 1. Read `chunk_count` from each response and stop requesting chunks after `chunk_count` successful responses. Treat each returned `text` as opaque Markdown. Do not summarize, reflow, compress, cite instead of include, or omit any body, comment, ancestor, governing document, present source, or unresolved citation. Preserve the sequence and verify each index, range and digest. Stop on every error, missing or reordered chunk, premature end, altered content, or size mismatch. Do not retry a failed child silently.
3. After the final chunk of a `full` package, run `{SCRIPT} finish INVOCATION_ID` with the same exact ID. A partial, unfinished, timed out, interrupted, changed, replayed, or cross-invocation stream has no receipt and must stop.
4. The returned receipt is `emission-only`. It establishes only that the helper emitted the complete exact package. Do not claim model-context acceptance, stage completion, issue completion, or provider receipt from this command or receipt. Separate actual post-truncation provider-input comparison is required for model-context acceptance.
