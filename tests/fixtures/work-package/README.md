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
