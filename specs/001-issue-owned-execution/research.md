# Research decisions for issue-owned execution

## Select upstream by revision

Use `github/spec-kit@a4d571229e164e440f8950cac034607bae7b7518` for the product
integration. Issue #65 requires the revision examined in the roadmap reuse record.
Its package identifies itself as `1.4.4.dev0`. The repository's authoring scaffold
uses release `1.4.4`, Bash helpers and integration `none`. Keep these roles explicit
in the package manifest, lockfile and evidence when the implementation is added.
Choosing the installed release solely because the version numbers look similar would
invalidate the provenance of the recorded experiments. Changing the runtime pin
requires repeating the affected compatibility checks.
[Reuse evidence](https://github.com/dzackgarza/itree/issues/64#issuecomment-7060614683) · [Pinned source](https://github.com/github/spec-kit/tree/a4d571229e164e440f8950cac034607bae7b7518) · [Checkout configuration](../../.specify/init-options.json)

## Keep work in the selected GitHub issue

Use the qualified issue as input, load current data with `gh`, and materialize only
that issue's data outside the checkout. The pinned helper supports an explicit
feature directory and suppressing local feature selection. A preset must also replace
the specify prompt's explicit pointer write. Store accepted changes in the issue body
and supporting decisions or evidence in linked comments. Keeping feature plans on
implementation branches would require a new contributor to locate an unmerged branch
to recover the current work.
[Stage storage](https://github.com/dzackgarza/itree/issues/71) · [Feature resolution](https://github.com/github/spec-kit/blob/a4d571229e164e440f8950cac034607bae7b7518/scripts/python/common.py)

## Serialize all body edits during a stage

**Editing policy, adopted 2026-11-27.** While a stage is active, contributors,
automation, and maintainers submit edits to the same writer for that issue. A stage
can prepare a proposal while another stage is working. Only the writer performs the
final read, reconciliation, body mutation and confirmation. A direct browser or API
edit bypasses this protocol. Begin an editing session only when every body editor
will participate. GitHub administrators retain their underlying permissions.

The REST endpoint accepts a replacement body. Its documentation and GraphQL's
`UpdateIssueInput` expose no expected body version for this operation. A request ID
or a timestamp comparison before a PATCH cannot exclude a later writer. Repeated
reads, individual checkout locks, and moving current tasks to Git would each leave
one of the specified requirements unmet.
[REST update](https://docs.github.com/en/rest/issues/issues#update-an-issue) · [GraphQL input](https://docs.github.com/en/graphql/reference/input-objects#updateissueinput) · [Late-write case](https://github.com/dzackgarza/itree/issues/71#issuecomment-7078688738)

Use a repository-owned GitHub Actions writer with one concurrency group per issue
node ID. Persist proposals as issue comments before waking the writer. Use issue
comment events and explicit workflow dispatch for wake-up and recovery. Each run
drains pending authorized requests; its request queue lives in GitHub comments.
This matters because Actions can replace a pending run. A lost or coalesced wake-up
must leave the proposal available for the next drain. Give the runner the issue write
permission it needs. Validate the submitting actor's authority and parse each proposal
as data. Preserve normal issue-body editing between managed sessions, with entry and
release coordinated through the writer. The full mutation contract defines retry and
conflict behavior. [Actions concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency) · [Mutation contract](contracts/issue-writer.md)

## Reuse execution machinery and enforce mandatory hooks

Package an upstream extension for itree's commands and a preset for narrow prompt or
template substitutions. Use the same definitions to render Claude Code, Codex and
OpenCode entry points. Extend the pinned workflow using its existing steps, gates and
loops for analysis and convergence. Execute mandatory hooks in a step that can fail.
The upstream hook executor returns invocation information and delegates execution.
The bundled workflow goes from tasks to implementation; its step list omits the
required analysis and convergence checks.

The reuse record reports a scratch run with a loop, gate and local resume. It does
not establish that a real harness receives an issue or that a new checkout can recover
the stage. Record both through the production interfaces. A separate scheduler, fork
or hand-authored command per harness adds maintenance without resolving these gaps.
[Extension API](https://github.com/github/spec-kit/blob/a4d571229e164e440f8950cac034607bae7b7518/docs/extensions/api.md) · [Workflow engine](https://github.com/github/spec-kit/blob/a4d571229e164e440f8950cac034607bae7b7518/src/specify_cli/workflows/engine.py) · [Harness acceptance](https://github.com/dzackgarza/itree/issues/68)

## Bind closure to retrievable evidence

Store the reviewed contract text, digest and full code commit ID in the independent
review record. Link the exact evidence for each condition and state how the delivered
work contributes to its north-star goal. Retrieve the stored text and verify its
digest before relying on an earlier decision. GitHub permits edits to issues and
comments, and authorized users can remove edit history. A digest alone cannot recover
text; a link to a mutable issue body cannot identify the reviewed version.
Evidence loss invalidates that disposition. Preserve the original intent when a
later amendment needs another review.
[GitHub edit history](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/editing-an-issue) · [Completion review](https://github.com/dzackgarza/itree/issues/73)

## Keep issue selection and task selection separate

Keep `first_ready_work_unit` and its native-blocker and preorder rules. Put the new
local prerequisite graph in the work unit; Pydantic and NetworkX are already project
dependencies. Use stable task IDs and declared order among ready tasks. Promoting an
interleaved dependency between tasks of two issues to a whole-issue blocker can
create a cycle. An external prerequisite is outside the issue-local selector's domain.
Adoption must preserve the definition-first order required by lean-categories in the shared workflow.
[Readiness implementation](../../src/itree/readiness.py) · [Issue-local scheduling](https://github.com/dzackgarza/itree/issues/67) · [Project adoption](spec.md#project-adoption)

## Complete upstream reuse record

The runtime is `specify-cli==1.4.4.dev0` from
`github/spec-kit@a4d571229e164e440f8950cac034607bae7b7518`. Declare it through
`[tool.uv.sources]`, not a PEP 508 direct reference, so Hatchling needs no
allow-direct-references setting. The executable is `specify`; Python 3.11 or
later is supported. The authoring scaffold remains release 1.4.4. The complete
MIT notice, including `Copyright GitHub, Inc.`, is `.specify/LICENSE`.

Each row records the inspected source at that revision, the selected reuse mode,
the precise itree adaptation, and the reason that adaptation is required. Work
unit identifiers show where runtime behavior is delivered; they do not create a
second reuse registry.

| Capability | Upstream source or API | Reuse mode | Exact itree change and reason |
| --- | --- | --- | --- |
| Reproducible runtime | `pyproject.toml`; `specify` entry point | Direct | Pin the exact Git revision in `pyproject.toml` and `uv.lock`; future agents execute the behavior examined by the roadmap instead of a different release. |
| Existing governing constitution | `specify init`; `.specify/memory/constitution.md`; command constitution reads | Wrapped | Keep the ratified constitution and add its opening full-read list. Upstream would create another constitution; the wrapper exposes the existing authority. |
| Command prompts | `templates/commands/{specify,clarify,plan,tasks,checklist,analyze,converge,implement,constitution}.md` | Direct with presets | Change only issue ownership, goal alignment, storage, and convergence inputs required by #64 while retaining the remaining literal prompt control flow. Owners: #69, #70, #66, #73, #74. |
| Templates and prerequisite scripts | `templates/`; `scripts/python/` | Direct | Reuse template resolution and helper behavior for issue-materialized stages instead of maintaining parallel scripts. Owners: #69, #70, #71. |
| Temporary feature storage | `scripts/python/common.py:get_feature_paths`; `SPECIFY_FEATURE_DIRECTORY`; `SPECIFY_FEATURE_NO_PERSIST=1` | Direct | Materialize only the selected issue in scratch storage, suppress the upstream active-feature pointer, reconcile accepted results to GitHub, and discard scratch state. Owner: #71. |
| Claude Code integration | `specify init --integration claude` and its renderers | Direct | Reuse generated skills and settings without overwriting `AGENTS.md` or `CLAUDE.md`. Owner: #68. |
| Codex integration | `specify integration install codex` and its renderers | Direct | Reuse the native skills, hooks, and configuration surface. Owner: #68. |
| OpenCode integration | `specify integration install opencode` and its renderers | Direct | Reuse native commands, runtime plugins, and configuration. Owner: #68. |
| itree commands in every harness | `extension.yml`; `specify extension add --dev`; `specify integration use` | Wrapped installer | Reuse extension rendition for `speckit.<extension>.<command>` while selecting each harness as the default in turn because extension commands render only for the default. Owner: #68. |
| Scoped prompt changes | `specify preset add --dev`; `replace`, `prepend`, `append`, `wrap`, `replace-section` | Direct | Apply exact, uniquely selected replacements such as the original-contract source in convergence. Owners: #66, #69, #70, #73, #74. |
| Session-start context | `src/specify_cli/events/`; `.specify/events.py`; `events:` in `extension.yml` | Direct | Reuse supported Claude Code, Codex, and OpenCode events to load the selected issue at session start without editing instruction files. Owner: #68. |
| Agent-context extension | `extensions/agent-context/` | Not used | Reuse the session event instead because this extension edits managed blocks in `AGENTS.md` or `CLAUDE.md`; #64 prohibits that route for harness context. Owner: #68. |
| Workflow backend | `src/specify_cli/workflows/engine.py`; `specify workflow run/resume` | Direct | Retain upstream steps, loops, gates, and resume behavior while defining the issue-owned analysis and convergence stages omitted by the bundled Speckit workflow. Owner: #74. |
| Extension hook registration | `ExtensionManifest`, `ExtensionManager`, `.specify/extensions.yml`, `before_*` and `after_*` | Direct | Keep upstream registration and condition data so stages expose the same extension surface. Owner: #75. |
| Mandatory hook execution | `HookExecutor.execute_hook`; workflow `shell` and `command` steps; event scripts | Wrapped | Execute hooks in a status-bearing step because upstream returns invocation information for an agent rather than running the hook. Owners: #74, #75. |
| GitHub body and comment persistence | Upstream feature-directory file reads and writes | New adapter at the upstream boundary | Materialize from the selected live issue and write accepted stage results back to its four sections and comments; upstream has no GitHub issue store. Owners: #65, #71. |
| Original contract as analysis intent | `templates/commands/analyze.md` and `converge.md` | Direct with preset substitution | Supply the immutable original-contract section alongside spec, plan, and tasks; upstream has no input for that separate contract. Owners: #66, #73. |
| Convergence without duplicate tasks | `templates/commands/converge.md` | Direct with new reconciliation behavior | Match findings to unfinished tasks before appending; upstream appends a fresh convergence phase on every run. Owner: #73. |
| Issue-local task prerequisites | `templates/commands/tasks.md`; `templates/tasks-template.md` | New issue-local scheduler | Retain stable task records but compute prerequisites inside the issue because upstream task lines and native whole-issue blockers do not express them. Owner: #67. |
| North-star alignment | Constitution prompts, lifecycle stages, and issue-owned requirement records | New domain behavior within upstream stages | Require a causal goal relation for every issue and task, with approved provenance; upstream supplies no user-governed north-star model. Owners: #70, #66, #73. |
| Intake and semantic completion | `itree new`, native sub-issues/blockers, `itree close` | New domain behavior at existing itree commands | Keep one issue per independent work unit and require full original-contract evidence for close; upstream has no matching authority model. Owners: #72, #73. |
| Live work-package loading | GitHub CLI REST/GraphQL APIs; upstream feature materialization contract | New read adapter | Read the live issue, comments, native ancestors, repository documents, and one-hop cited content; do not read an upstream feature directory or add a local work registry. Owner: #65. |

The optional `extensions/github` issue exporter, `extensions/git` feature
branches, upstream feature counters, and `.specify/feature.json` stay outside
this model because they create competing work identity or storage. Workflow run
state stays local to one execution and is not a work registry.
