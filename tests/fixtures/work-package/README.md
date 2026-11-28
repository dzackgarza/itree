# Live work-package acceptance fixture

`setup.py` creates one private, disposable GitHub repository and records its exact live inputs in a JSON file outside the source checkout. It separates repository creation from cloning, explicitly creates the initial `main` branch, and verifies the remote repository, governing file, and selected issue before returning.

Create a fixture with Python 3.14, `uv`, `git`, `trash`, and an authenticated `gh`:

```sh
export FIXTURE_PARENT="$(mktemp -d /tmp/opencode/itree-work-acceptance.XXXXXX)"
export FIXTURE_ROOT="$FIXTURE_PARENT/acceptance"
uv run tests/fixtures/work-package/setup.py create \
  --root "$FIXTURE_ROOT" \
  --output /tmp/opencode/itree-work-fixture-state.json
export ITREE_WORK_FIXTURE_STATE=/tmp/opencode/itree-work-fixture-state.json
```

The fixture contains a four-section work unit below a native roadmap parent, binding and corrective decisions, an open finding, a separate issue with 101 ordered comments, malformed and legacy issue bodies, a parent-cycle transport case, and a required-comment-read failure case. It also contains full-SHA, branch, tag, same-name/same-commit, and different-commit branch/tag blob sources; binary, non-UTF-8, missing, directory, and unsupported citations; real external Markdown, plain-text, and HTML sources; and inaccessible, failed-redirect, invalid-encoding, binary, and unsupported-media external reads.

Run the public acceptance before implementation. The expected red result is a nonzero response saying that the `work` command is unavailable, without a fixture-construction error:

```sh
uv run --locked pytest tests/test_work.py -x -vv
```

After implementation, run every fixture case and the four entry points through the same tests. Do not use `specs/<feature>/`, `.itree/work/<issue>/`, or `.specify/feature.json` as work-package input. The tests place decoy files at those paths and reject their content in command output.

Record the following evidence on `dzackgarza/itree#65`: fixture slug and issue numbers, fixture commits, implementation commit, Python and `uv` versions, the Spec Kit revision `a4d571229e164e440f8950cac034607bae7b7518`, setup duration, every command duration and exit status, stdout/stderr logs, exact-content comparisons, and any discrepancy. Link the clean analysis comment before recording implementation evidence.

After posting the evidence, read the new issue comment and its referenced records through fresh GitHub API calls and compare them byte-for-byte with the local records. Then delete only the generated repository named by `ITREE_ACCEPTANCE_REPO` and verify that it cannot be viewed. Trash `$FIXTURE_ROOT` and temporary state/logs. Verify that the source checkout, unrelated GitHub repositories, issue #65, its analysis comment, and its persisted acceptance evidence survive.

When `ITREE_WORK_FIXTURE_STATE` is absent, `tests/test_work.py` creates a fresh real fixture and deletes it after the run. The checked-in state file path is not a cache: every invocation of `itree work` still reads live GitHub and external source content.

## Resume an existing fixture

`setup.py resume` requires the existing qualified repository, preserved checkout, and explicit state output path. It never creates a replacement slug or clones another repository. Validate the remote and `.git` directory before using them:

```sh
export ITREE_ACCEPTANCE_REPO=owner/existing-work-package-repo
uv run --script tests/fixtures/work-package/setup.py resume \
  --slug "$ITREE_ACCEPTANCE_REPO" \
  --root "$FIXTURE_ROOT" \
  --output /tmp/opencode/itree-work-fixture-state.json
```

Recovery reads prior validated state with `FixtureState.model_validate_json` and never overwrites its identity with partial state. Issues reconcile by unique stable title and native ID; comments reconcile through all live pages, including after a remote success whose local record was interrupted. A legacy journal without schema-versioned native IDs fails explicitly. `createPullRequest`, `createIssue`, `addComment`, and `addSubIssue` use valid GraphQL mutations.

Every request in `family-journal.jsonl` has one `schema_version`, `family`, `operation_id`, native `object_ids`, `object_count`, aggregate `api_seconds`, and separate `pacing_seconds`. One comment batch has one elapsed record, not one elapsed charge per comment. Concurrent `api_seconds` and `pacing_seconds` are sums per request, not wall-clock time. The raw setup, case, family-cost, and log records are retained outside the disposable tree and read before cleanup. Missing content-family object counts stop cleanup. Failures retain an incomplete timing record. A successful cleanup writes `teardown-timings.json` with setup, case, cleanup, and total elapsed costs.

Comment creation has a default of two workers within a single setup invocation and never a persistent work-package cache. One worker owns all ordered pagination comments in sequential five-alias batches so database ID ordering cannot interleave with another owner. The other worker handles roadmap, selected/cited, and tail families after their dependencies are ready; a task and its parent links finish before dependent citation text is assembled. An operation cannot run concurrently with another mutation involving the same subject. The shared request checkpoint uses `max(1 second, 0.75 seconds per content object)` between mutation starts. Thus at most ten aliases are being created in flight, while added delays target GitHub's documented 80 content-generating requests per minute and 100 concurrent requests. A failure sets the stop checkpoint, cancels pending work, waits for in-flight results, and records all valid native responses without retrying or deleting the incomplete repository.

Recovery verifies that checkout `origin` matches the explicit slug. Only changed head commits are pushed. Annotated tag objects upload with exact tagger/message data and must return the local SHA. A bounded `createRef` mutation publishes missing refs, whose response names include `prefix` and `name`. Final branch and tag IDs must match the actual local objects. `head` always comes from local `main`. The remote default branch is set with `gh repo edit --default-branch main` when necessary and then asserted.

Fresh repository creation establishes the unborn `main` ref with `git symbolic-ref` and publishes it explicitly. Before mutation, live object reconciliation recovers completed objects whose journal write was interrupted. LFS lock verification and the SSH transfer protocol are configured only after `git check-attr` proves no tracked path has `filter=lfs`; repositories with those pointers keep full verification. A parentless attach uses `jq .parent.number // empty`, so a missing native parent is distinct from an existing incorrect parent. These boundaries have syntax, strict-type, import, and all-41-case collection validation. Recovery, a focused subset, and these static checks are not cold acceptance.
