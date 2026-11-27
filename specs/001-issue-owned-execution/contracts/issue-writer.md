# Issue body writer

The serialization key is the stable GitHub issue node ID. Stage runs may overlap.
During a managed session the same writer applies every change to that body, including
manual corrections and itree operations such as absorption that touch the body.
Transfer or loss of write authority suspends mutation until ownership is resolved.

## Submission and admission

Use a structured GitHub comment as the durable request. Carry the issue ID, request
ID, attempt ID, target section, base fingerprint and proposed value or delta. For a
task edit, also carry the stable task ID and the base value of the affected fields.
Carry the content used to compute the changes, or a retrievable reference to it, so
that the writer can compare base, live and proposed values. Use GitHub's authenticated
comment author and repository permissions for authorization. An original-contract
amendment requires the appropriate maintainer authority. Reject unknown sections,
invalid references, ambiguous boundaries and attempts to modify unrelated fields.

Use a fenced `itree-stage-edit` JSON record for a proposal. The common keys are
`version` (initially 1), `issue_node_id`, `request_id`, `attempt_id`, `section`,
`base_sha256`, `base_text` and `proposed_text`. Text values are exact section
contents, excluding delimiters. A task-field edit additionally supplies `task_id`
and `field`; its text values and digest refer to that field. A repeated request with
an edited payload is rejected. Generated stage commands produce this record. A
maintainer can submit the same record through the issue's comment interface.

When a proposal exceeds GitHub's comment size limit, submit its text in ordered
content comments first. The final request can give ordered comment IDs and digests
in place of each inline text value. Before admission verify the author, order and
digest of every part and the assembled bytes. A partial upload creates no applicable
request. Send large body updates through the existing `gh` stdin transport.

Before admitting the first stage, confirm that existing direct edits have settled and
that all editors have switched to the managed session. If either condition is unknown,
keep admission pending. Persist the proposal and notify the writer. Use one Actions
concurrency group
for all entry points that can mutate the same issue, with cancellation of a running
writer disabled. The writer records session admission and reads the complete pending
request list with pagination. A resume or explicit drain recovers outstanding requests
after a missed wake-up. A newly arrived edit must either be processed during the drain
or remain pending for a later drain. Release the managed session only after all its
attempts and queued edits have a known disposition. Clients submit maintainer edits
through the same path until release is confirmed.

## Apply and confirm

1. Look up the request ID on the issue. Replaying the same request returns its
   confirmed result. The same ID with different content is a conflict.
2. Under exclusive execution, read the current body and referenced comments. Apply
   changes whose target still matches its base. Merge independent plan and task
   changes by preserving untouched byte ranges. Report incompatible edits to the
   same passage as a conflict, identifying the affected section and both versions.
3. Record the intended operation before sending the body update. Include an operation
   identifier in the issue's stage bookkeeping so that a lost reply can be reconciled.
   A no-change round trip leaves the body byte-for-byte identical. A normal stage
   update leaves the original contract byte-for-byte identical.
4. Confirm the new body, affected values, unchanged contract, and any linked comment
   IDs from GitHub. Record the disposition on the issue. A caller receives success
   only after confirmation. Check the reviewed contract and revision in this same
   writer before a completion close; confirm the resulting issue state.
5. If a write has an unknown outcome, stop later dependent effects. On resume, check
   the intended operation against live state and its receipt. A matching result can
   be confirmed. A negative read while the old request could still take effect is
   inconclusive. Keep later writes pending until the old operation is settled.

The writer serializes participating editors. It does not implement a server-side
conditional PATCH. A known bypass or an unexplained change to the managed body must
stop the attempt for reconciliation. Readback cannot promise preservation of an edit
submitted outside the agreed protocol.

## Required observations

In the US2 race, A finishes preparing a plan edit, B commits a task mark through the
writer, and A submits its stale proposal. A's accepted change must preserve B's task
mark. Repeat with B as a maintainer and with two proposals to the same plan passage.
For same-passage conflicts, keep the earlier confirmed text, show the conflict to the
later caller, and leave its stage incomplete. Pause a worker after the remote write
but before its receipt. A new session must discover the effect and confirm it once.
Include concurrent wake-ups, event coalescing, a paginated request list and a rejected
write in the writer's milestone acceptance run.
