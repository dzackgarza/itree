#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "cyclopts>=2.0",
#   "networkx>=3.0",
#   "pydantic>=2.0",
# ]
# ///
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    __package__ = "itree"
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from itree.models import FixtureState

PIN = "0e80c9679e6f6bfd55e13cbcf193d297c5207117"

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)
last_mutation_at = 0.0
last_pacing_seconds = 0.0


class FixtureCommandError(RuntimeError):
    def __init__(
        self,
        command: list[str],
        result: subprocess.CompletedProcess[str],
        elapsed: float,
    ) -> None:
        self.result = result
        super().__init__(
            f"command failed after {elapsed:.3f}s: {command!r}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )


def _is_mutation(command: list[str]) -> bool:
    if command[0] == "git" and command[1] == "push":
        return True
    if (
        command[0] == "gh"
        and command[1] == "issue"
        and command[2] in ("create", "edit")
    ):
        return True
    if command[0] == "gh" and command[1] == "api" and command[2] == "graphql":
        return any("mutation " in argument for argument in command[3:])
    return (
        command[:3] == ["gh", "api", "-X"]
        and len(command) > 3
        and command[3] in ("POST", "PATCH", "PUT", "DELETE")
    )


def _content_units(command: list[str]) -> int:
    if command[:3] != ["gh", "api", "graphql"]:
        return 1
    text = "\n".join(command[3:])
    return max(
        1,
        text.count(":createIssue(input:")
        + text.count(":addComment(input:")
        + text.count(":addSubIssue(input:"),
    )


def run(
    command: list[str], *, cwd: Path | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    global last_mutation_at, last_pacing_seconds
    mutates = _is_mutation(command)
    pace_started = time.perf_counter()
    if mutates:
        interval = max(1.0, 0.75 * _content_units(command))
        remaining = interval - (time.perf_counter() - last_mutation_at)
        if remaining > 0 and last_mutation_at != 0.0:
            time.sleep(remaining)
    pace_seconds = time.perf_counter() - pace_started
    started = time.perf_counter()
    result = subprocess.run(
        command, cwd=cwd, input=input_text, text=True, capture_output=True, check=False
    )
    elapsed = time.perf_counter() - started
    if mutates:
        last_mutation_at = time.perf_counter()
        last_pacing_seconds = pace_seconds
    print(
        f"FIXTURE-STEP seconds={elapsed:.6f} pacing_seconds={pace_seconds:.6f} exit={result.returncode} command={json.dumps(command)}",
        flush=True,
    )
    if result.returncode != 0:
        raise FixtureCommandError(command, result, elapsed)
    return result


type Family = Literal[
    "createIssue",
    "createPullRequest",
    "addComment",
    "addSubIssue",
    "readIssueCatalog",
    "readCommentCatalog",
    "readPullRequestCatalog",
]
type JsonObject = dict[str, JsonValue]


class FamilyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    objects: dict[str, JsonObject]


class FamilyRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1]
    family: Family
    operation_id: str = Field(min_length=1)
    object_ids: dict[str, str]
    object_count: int = Field(ge=0)
    result: FamilyResult
    api_seconds: float = Field(gt=0)
    pacing_seconds: float = Field(ge=0)
    written_at: float


def comments(
    slug: str, number: int, bodies: list[str], root: Path
) -> list[tuple[int, str]]:
    _, identifiers = add_ordered_comments(slug, number, bodies, root)
    return [
        (
            identifier,
            f"https://github.com/{slug}/issues/{number}#issuecomment-{identifier}",
        )
        for identifier in identifiers
    ]


def add_comment_tail(
    slug: str, root: Path, specifications: list[tuple[int, str]]
) -> None:
    assert specifications
    completed = _completed(root)
    operation_prefix = "addComment:fixture-tail-v1"
    family_records = [
        record for key, record in completed.items() if key.startswith(operation_prefix)
    ]
    missing: list[tuple[str, int, str]] = []
    for index, (number, body) in enumerate(specifications):
        label = f"{number}:{index}"
        live = _matching_live_comment(slug, number, body, root)
        saved_versions = [
            objects[label]
            for record in family_records
            if label in (objects := _objects(record))
        ]
        if live is None and saved_versions:
            raise FixtureRecoveryError(
                f"journaled tail comment is absent from the live family: {label}"
            )
        if live is not None and any(
            saved["id"] != live["id"] or saved["databaseId"] != live["databaseId"]
            for saved in saved_versions
        ):
            raise FixtureRecoveryError(
                f"journaled tail comment identity changed: {label}"
            )
        if live is None:
            missing.append((label, number, body))
    batch_serial = len(family_records)
    for offset in range(0, len(missing), 15):
        batch = missing[offset : offset + 15]
        operation_id = f"{operation_prefix}-{batch_serial:03d}"
        assert operation_id not in completed
        batch_serial += 1
        fields = [
            f"c{index:03d}:addComment(input:{{subjectId:{json.dumps(str(_live_issue(slug, number, root)['id']))},body:{json.dumps(body)}}}){{commentEdge{{node{{databaseId id url body}}}}}}"
            for index, (_label, number, body) in enumerate(batch)
        ]
        query = "mutation WorkPackageCommentTail {" + " ".join(fields) + "}"
        assert len(fields) * 3 <= 500_000
        assert len(query.encode("utf-8")) < 120_000
        started = time.perf_counter()
        response = _graphql_response(query)
        elapsed = time.perf_counter() - started
        created = _record_family_response(
            root,
            "addComment",
            operation_id,
            response,
            {
                f"c{index:03d}": label
                for index, (label, _number, _body) in enumerate(batch)
            },
            {
                f"c{index:03d}": body
                for index, (_label, _number, body) in enumerate(batch)
            },
            elapsed,
        )
        for (_label, number, body), record in zip(batch, created.values(), strict=True):
            assert record["body"] == body
            _live_comments(slug, number, root).append(record)


class FixtureRecoveryError(RuntimeError):
    pass


def _journal_path(root: Path) -> Path:
    if root.name != "acceptance":
        return root / "family-journal.jsonl"
    return root.parent.parent / f"{root.parent.name}-family-journal.jsonl"


def _records(root: Path) -> list[FamilyRecord]:
    path = _journal_path(root)
    if not path.exists():
        return []
    try:
        return [
            FamilyRecord.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
    except ValueError as error:
        raise FixtureRecoveryError(
            f"recovery requires schema_version=1 native-ID request records in {path}; "
            "the preserved legacy journal is not sufficient for no-duplicate resume"
        ) from error


def _completed(root: Path) -> dict[str, FamilyRecord]:
    creations = [
        record
        for record in _records(root)
        if record.family
        in ("createIssue", "createPullRequest", "addComment", "addSubIssue")
    ]
    records: dict[str, FamilyRecord] = {}
    for record in creations:
        if record.operation_id in records:
            raise FixtureRecoveryError(
                f"duplicate completed operation id: {record.operation_id}"
            )
        records[record.operation_id] = record
    return records


def _remember(
    root: Path,
    family: Family,
    operation_id: str,
    objects: dict[str, JsonObject],
    api_seconds: float,
    pacing_seconds: float,
) -> None:
    object_ids: dict[str, str] = {}
    for alias, result in objects.items():
        identifier = result.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise FixtureRecoveryError(
                f"operation {operation_id} has no native GraphQL id for {alias}: {result}"
            )
        object_ids[alias] = identifier
    record = FamilyRecord(
        schema_version=1,
        family=family,
        operation_id=operation_id,
        object_ids=object_ids,
        object_count=len(object_ids),
        result=FamilyResult(objects=objects),
        api_seconds=api_seconds,
        pacing_seconds=pacing_seconds,
        written_at=time.time(),
    )
    path = _journal_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as journal:
        journal.write(
            json.dumps(
                record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            )
            + "\n"
        )
        journal.flush()
        os.fsync(journal.fileno())


def _objects(record: FamilyRecord) -> dict[str, JsonObject]:
    return record.result.objects


def _json_objects(raw: str) -> list[JsonObject]:
    return TypeAdapter(list[JsonObject]).validate_python(json.loads(raw))


def _json_object(raw: str) -> JsonObject:
    return TypeAdapter(JsonObject).validate_python(json.loads(raw))


def _record_family_response(
    root: Path,
    family: Literal["createIssue", "addComment"],
    operation_id: str,
    response: JsonObject,
    aliases: dict[str, str],
    expected_bodies: dict[str, str],
    api_seconds: float,
) -> dict[str, JsonObject]:
    created: dict[str, JsonObject] = {}
    data = response.get("data")
    for alias, key in aliases.items():
        if not isinstance(data, dict):
            break
        node = data.get(alias)
        if not isinstance(node, dict):
            continue
        try:
            result = _response_for_family(family, node)
        except AssertionError, KeyError, TypeError:
            continue
        if result.get("body") != expected_bodies[alias]:
            continue
        identifier = result.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        created[key] = result
    if created:
        _remember(root, family, operation_id, created, api_seconds, last_pacing_seconds)
    if len(created) != len(aliases) or response.get("errors"):
        raise FixtureRecoveryError(
            f"mutation {operation_id} returned failed or invalid aliases; "
            f"valid native IDs were persisted before this error: {response}"
        )
    return created


def _node_id(slug: str, number: int) -> str:
    return run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            slug,
            "--json",
            "id",
            "--jq",
            ".id",
        ]
    ).stdout.strip()


def _repository_id(slug: str) -> str:
    return run(["gh", "api", f"repos/{slug}", "--jq", ".node_id"]).stdout.strip()


def _response_for_family(
    family: str, node: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    if family == "addComment":
        candidate = node["commentEdge"]
        assert isinstance(candidate, dict), node
        value = candidate["node"]
        assert isinstance(value, dict), node
        return value
    if family == "createIssue":
        candidate = node["issue"]
        assert isinstance(candidate, dict), node
        return candidate
    if family == "createPullRequest":
        candidate = node["pullRequest"]
        assert isinstance(candidate, dict), node
        return candidate
    assert family == "addSubIssue", family
    candidate = node["subIssue"]
    assert isinstance(candidate, dict), node
    return candidate


def _number(value: JsonValue) -> int:
    assert isinstance(value, int) and not isinstance(value, bool), value
    return value


def _graphql_response(query: str) -> JsonObject:
    try:
        return _json_object(
            run(["gh", "api", "graphql", "-f", f"query={query}"]).stdout
        )
    except FixtureCommandError as error:
        try:
            document = _json_object(error.result.stdout)
        except UnicodeDecodeError, json.JSONDecodeError, ValueError:
            raise error
        if "data" not in document and "errors" not in document:
            raise error
        return document


def _batch_graphql(
    slug: str,
    root: Path,
    family: Literal["createIssue", "createPullRequest", "addComment", "addSubIssue"],
    input_value: JsonObject,
    key: str,
    expected_body: str | None = None,
) -> JsonObject:
    if family == "createIssue":
        result_selection = "issue{number id body title url}"
    elif family == "createPullRequest":
        result_selection = "pullRequest{number id url body}"
    elif family == "addComment":
        result_selection = "commentEdge{node{databaseId id url body}}"
    else:
        result_selection = "subIssue{number id}"
    arguments = ",".join(
        f"{key}:{json.dumps(value)}" for key, value in input_value.items()
    )
    alias = "m000"
    query = f"mutation WorkPackageFamily {{{alias}:{family}(input:{{{arguments}}}){{{result_selection}}}}}"
    started = time.perf_counter()
    response = _graphql_response(query)
    elapsed = time.perf_counter() - started
    assert "errors" not in response and isinstance(response.get("data"), dict), response
    data = response["data"]
    assert isinstance(data, dict), response
    node = data[alias]
    assert isinstance(node, dict), response
    result = _response_for_family(family, node)
    if expected_body is not None:
        assert result.get("body") == expected_body, {
            "operation": family,
            "result": result,
        }
    _remember(root, family, key, {"m000": result}, elapsed, last_pacing_seconds)
    return result


_issue_catalogs: dict[tuple[str, Path], list[JsonObject]] = {}
_comment_catalogs: dict[tuple[str, Path, int], list[JsonObject]] = {}


def _live_issues(slug: str, root: Path) -> list[JsonObject]:
    cache_key = (slug, root)
    cached = _issue_catalogs.get(cache_key)
    if cached is not None:
        return cached
    started = time.perf_counter()
    records = _json_objects(
        run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                slug,
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number,id,title,body,url",
            ]
        ).stdout
    )
    elapsed = time.perf_counter() - started
    objects = {str(record["title"]): record for record in records}
    if len(objects) != len(records):
        raise FixtureRecoveryError("fixture issue titles are not unique")
    _remember(
        root,
        "readIssueCatalog",
        f"readIssueCatalog:{time.time_ns()}",
        objects,
        elapsed,
        0.0,
    )
    _issue_catalogs[cache_key] = records
    return records


def _live_issue(slug: str, number: int, root: Path) -> JsonObject:
    return run_json_view(slug, number, root)


def run_json_view(slug: str, number: int, root: Path) -> JsonObject:
    for record in _live_issues(slug, root):
        if record["number"] == number:
            return record
    raise FixtureRecoveryError(f"journaled issue #{number} is absent from {slug}")


def issue(
    slug: str, title: str, body: str, root: Path, *, self_referential: bool = False
) -> int:
    operation_id = f"createIssue:{title}"
    existing = next(
        (record for record in _live_issues(slug, root) if record["title"] == title),
        None,
    )
    if existing is not None:
        number = _number(existing["number"])
        if not self_referential and existing["body"] != body:
            raise FixtureRecoveryError(
                f"immutable issue body differs for {title}: {existing['body']!r}"
            )
        completed = _completed(root).get(operation_id)
        if completed is not None:
            recorded = _objects(completed)["m000"]
            if recorded["id"] != existing["id"] or recorded["number"] != number:
                raise FixtureRecoveryError(
                    f"journaled issue identity changed for {title}: "
                    f"journal={recorded}, live={existing}"
                )
        return number
    if operation_id in _completed(root):
        raise FixtureRecoveryError(
            f"journaled issue is absent before mutation: {operation_id}"
        )
    result = _batch_graphql(
        slug,
        root,
        "createIssue",
        {"repositoryId": _repository_id(slug), "title": title, "body": body},
        operation_id,
        body,
    )
    _live_issues(slug, root).append(result)
    return _number(result["number"])


def ensure_issue_body(slug: str, number: int, body: str, root: Path) -> None:
    existing = _live_issue(slug, number, root)
    if existing["body"] == body:
        return
    run(["gh", "issue", "edit", str(number), "--repo", slug, "--body", body])
    existing["body"] = body


def create_issue_family(
    slug: str, root: Path, specifications: list[tuple[str, str]]
) -> None:
    live = _live_issues(slug, root)
    by_title = {str(record["title"]): record for record in live}
    operation_id = "createIssue:fixture-family-v1"
    completed = _completed(root)
    family_records = [
        completed[key]
        for key in completed
        if key == operation_id or key.startswith(f"{operation_id}-recovery-")
    ]
    for title, _body in specifications:
        existing = by_title.get(title)
        saved_versions = [
            objects[title]
            for record in family_records
            if title in (objects := _objects(record))
        ]
        if existing is None and saved_versions:
            raise FixtureRecoveryError(
                f"journaled issue is absent from the live family: {title}"
            )
        if existing is not None and any(
            saved["id"] != existing["id"] or saved["number"] != existing["number"]
            for saved in saved_versions
        ):
            raise FixtureRecoveryError(
                f"journaled issue family identity changed at {title}"
            )
    missing = [(title, body) for title, body in specifications if title not in by_title]
    for title, body in specifications:
        existing = by_title.get(title)
        if (
            existing is not None
            and title
            not in (
                "Roadmap: Work package acceptance",
                "Work package acceptance unit",
            )
            and existing["body"] != body
        ):
            raise FixtureRecoveryError(
                f"immutable issue body differs for {title}: {existing['body']!r}"
            )
    if not missing:
        return
    if family_records:
        operation_id = f"{operation_id}-recovery-{len(family_records)}"
    assert operation_id not in completed
    repository_id = _repository_id(slug)
    fields = [
        f"i{index:03d}:createIssue(input:{{repositoryId:{json.dumps(repository_id)},title:{json.dumps(title)},body:{json.dumps(body)}}}){{issue{{number id body title url}}}}"
        for index, (title, body) in enumerate(missing)
    ]
    query = "mutation WorkPackageIssueFamily {" + " ".join(fields) + "}"
    assert len(fields) * 2 <= 500_000
    assert len(query.encode("utf-8")) < 120_000
    started = time.perf_counter()
    response = _graphql_response(query)
    elapsed = time.perf_counter() - started
    created = _record_family_response(
        root,
        "createIssue",
        operation_id,
        response,
        {f"i{index:03d}": title for index, (title, _body) in enumerate(missing)},
        {f"i{index:03d}": body for index, (_title, body) in enumerate(missing)},
        elapsed,
    )
    live.extend(created.values())
    for result in created.values():
        _comment_catalogs.setdefault((slug, root, _number(result["number"])), [])


def create_pull_request(slug: str, root: Path) -> tuple[int, str]:
    operation_id = "createPullRequest:fixture-pull-request"
    started = time.perf_counter()
    records = _json_objects(
        run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                slug,
                "--state",
                "all",
                "--head",
                "pull-request-source",
                "--json",
                "number,id,body,title,url,headRefName,baseRefName",
            ]
        ).stdout
    )
    elapsed = time.perf_counter() - started
    _remember(
        root,
        "readPullRequestCatalog",
        f"readPullRequestCatalog:{time.time_ns()}",
        {str(record["number"]): record for record in records},
        elapsed,
        0.0,
    )
    matches = [
        record
        for record in records
        if record["headRefName"] == "pull-request-source"
        and record["baseRefName"] == "main"
        and record["title"] == "Fixture pull request"
    ]
    if len(matches) > 1:
        raise FixtureRecoveryError(f"duplicate fixture pull requests: {matches}")
    if matches:
        existing = matches[0]
        if existing["body"] != "Real pull request for source classification.":
            raise FixtureRecoveryError(f"fixture pull request body changed: {existing}")
        completed = _completed(root).get(operation_id)
        if (
            completed is not None
            and _objects(completed)["m000"]["id"] != existing["id"]
        ):
            raise FixtureRecoveryError("journaled pull request identity changed")
        return _number(existing["number"]), str(existing["url"])
    if operation_id in _completed(root):
        raise FixtureRecoveryError("journaled pull request is absent before mutation")
    result = _batch_graphql(
        slug,
        root,
        "createPullRequest",
        {
            "repositoryId": _repository_id(slug),
            "baseRefName": "main",
            "headRefName": "pull-request-source",
            "title": "Fixture pull request",
            "body": "Real pull request for source classification.",
        },
        operation_id,
        "Real pull request for source classification.",
    )
    return _number(result["number"]), str(result["url"])


def _live_comments(slug: str, number: int, root: Path) -> list[JsonObject]:
    cache_key = (slug, root, number)
    cached = _comment_catalogs.get(cache_key)
    if cached is not None:
        return cached
    owner, name = slug.split("/", 1)
    comments: list[JsonObject] = []
    objects: dict[str, JsonObject] = {}
    cursor: str | None = None
    api_seconds = 0.0
    while True:
        started = time.perf_counter()
        response = _json_object(
            run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    "query=query WorkPackageComments($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){issue(number:$number){comments(first:100,after:$after){nodes{id databaseId body url}pageInfo{hasNextPage endCursor}}}}}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"number={number}",
                    *(["-f", f"after={cursor}"] if cursor is not None else []),
                ]
            ).stdout
        )
        api_seconds += time.perf_counter() - started
        data = response["data"]
        assert isinstance(data, dict), response
        repository = data["repository"]
        assert isinstance(repository, dict), response
        issue_record = repository["issue"]
        assert isinstance(issue_record, dict), response
        connection = issue_record["comments"]
        assert isinstance(connection, dict), response
        nodes = TypeAdapter(list[JsonObject]).validate_python(connection["nodes"])
        comments.extend(nodes)
        for comment_record in nodes:
            objects[str(comment_record["databaseId"])] = comment_record
        page_info = connection["pageInfo"]
        assert isinstance(page_info, dict), response
        if page_info["hasNextPage"] is not True:
            break
        next_cursor = page_info["endCursor"]
        assert isinstance(next_cursor, str) and next_cursor, response
        cursor = next_cursor
    _remember(
        root,
        "readCommentCatalog",
        f"readCommentCatalog:{number}:{time.time_ns()}",
        objects,
        api_seconds,
        0.0,
    )
    _comment_catalogs[cache_key] = comments
    return comments


def _matching_live_comment(
    slug: str, number: int, body: str, root: Path
) -> JsonObject | None:
    matches = [
        record
        for record in _live_comments(slug, number, root)
        if record["body"] == body
    ]
    if len(matches) > 1:
        raise FixtureRecoveryError(
            f"duplicate live comments for issue #{number} and expected body"
        )
    return matches[0] if matches else None


def _validate_completed_comment(
    record: FamilyRecord, live: JsonObject | None
) -> JsonObject:
    if live is None:
        raise FixtureRecoveryError(
            f"journaled comment is absent before mutation: {record.operation_id}"
        )
    for saved in _objects(record).values():
        if saved["id"] != live["id"] or saved["databaseId"] != live["databaseId"]:
            raise FixtureRecoveryError(
                f"journaled comment identity changed: saved={saved}, live={live}"
            )
    return live


def comment(slug: str, number: int, body: str, root: Path) -> tuple[int, str]:
    operation_id = f"addComment:{number}:{body}"
    completed = _completed(root).get(operation_id)
    live = _matching_live_comment(slug, number, body, root)
    if completed is not None:
        live = _validate_completed_comment(completed, live)
    if live is not None:
        return _number(live["databaseId"]), str(live["url"])
    result = _batch_graphql(
        slug,
        root,
        "addComment",
        {"subjectId": str(_live_issue(slug, number, root)["id"]), "body": body},
        operation_id,
        body,
    )
    _live_comments(slug, number, root).append(result)
    return _number(result["databaseId"]), str(result["url"])


def add_ordered_comments(
    slug: str, number: int, bodies: list[str], root: Path
) -> tuple[list[str], list[int]]:
    assert bodies
    subject_id = str(_live_issue(slug, number, root)["id"])
    records_by_index: dict[int, JsonObject] = {}
    completed = _completed(root)
    for index, body in enumerate(bodies):
        live = _matching_live_comment(slug, number, body, root)
        records = [
            record
            for record in completed.values()
            if record.family == "addComment"
            and record.operation_id.startswith(f"addComment:{number}:batch-")
            and str(index) in record.object_ids
        ]
        if len(records) > 1:
            raise FixtureRecoveryError(
                f"duplicate completed batch ownership for comment {index}"
            )
        if records:
            record = records[0]
            saved = _objects(record)[str(index)]
            if live is None:
                raise FixtureRecoveryError(
                    f"journaled ordered comment {index} is absent: {record.operation_id}"
                )
            if saved["body"] != body:
                raise FixtureRecoveryError(
                    f"journaled ordered comment body changed: {saved}, {body}"
                )
            if record.object_ids[str(index)] != live["id"] or saved["id"] != live["id"]:
                raise FixtureRecoveryError(
                    f"journaled ordered comment identity changed: {saved}, {live}"
                )
        if live is not None:
            records_by_index[index] = live
    batch_size = min(15, len(bodies))
    for offset in range(0, len(bodies), batch_size):
        missing = [
            index
            for index in range(offset, min(offset + batch_size, len(bodies)))
            if index not in records_by_index
        ]
        if not missing:
            continue
        operation_id = f"addComment:{number}:batch-{offset:03d}"
        recovery = 0
        while operation_id in completed:
            recovery += 1
            operation_id = f"addComment:{number}:batch-{offset:03d}-recovery-{recovery}"
        fields = [
            f"m{index:03d}:addComment(input:{{subjectId:{json.dumps(subject_id)},body:{json.dumps(bodies[index])}}}){{commentEdge{{node{{databaseId id url body}}}}}}"
            for index in missing
        ]
        query = "mutation WorkPackageComments {" + " ".join(fields) + "}"
        assert len(fields) * 3 <= 500_000
        assert len(query.encode("utf-8")) < 120_000
        started = time.perf_counter()
        response = _graphql_response(query)
        elapsed = time.perf_counter() - started
        created = _record_family_response(
            root,
            "addComment",
            operation_id,
            response,
            {f"m{index:03d}": str(index) for index in missing},
            {f"m{index:03d}": bodies[index] for index in missing},
            elapsed,
        )
        for raw_index, created_record in created.items():
            records_by_index[int(raw_index)] = created_record
        _live_comments(slug, number, root).extend(created.values())
    ids = [
        _number(records_by_index[index]["databaseId"]) for index in range(len(bodies))
    ]
    assert ids == sorted(ids), f"comment database IDs are not ordered: {ids}"
    return bodies, ids


def _assert_origin(slug: str, checkout: Path) -> None:
    origin = run(["git", "remote", "get-url", "origin"], cwd=checkout).stdout.strip()
    if origin.startswith("git@"):
        origin_slug = origin.split(":", 1)[1].removesuffix(".git")
    else:
        origin_slug = (
            urllib.parse.urlsplit(origin).path.removeprefix("/").removesuffix(".git")
        )
    if origin_slug.casefold() != slug.casefold():
        raise FixtureRecoveryError(
            f"recovery slug does not match origin: expected={slug}, found={origin_slug}"
        )


def _assert_default_main(slug: str, checkout: Path) -> str:
    remote = _json_object(
        run(
            ["gh", "repo", "view", slug, "--json", "nameWithOwner,defaultBranchRef"]
        ).stdout
    )
    assert remote["nameWithOwner"] == slug, remote
    default_branch = remote["defaultBranchRef"]
    assert isinstance(default_branch, dict), remote
    if default_branch["name"] != "main":
        run(["gh", "repo", "edit", slug, "--default-branch", "main"])
        remote = _json_object(
            run(
                [
                    "gh",
                    "repo",
                    "view",
                    slug,
                    "--json",
                    "nameWithOwner,defaultBranchRef",
                ]
            ).stdout
        )
        default_branch = remote["defaultBranchRef"]
        assert isinstance(default_branch, dict), remote
    if default_branch["name"] != "main":
        raise FixtureRecoveryError(f"remote default branch is not main: {remote}")
    local_main = run(
        ["git", "rev-parse", "refs/heads/main"], cwd=checkout
    ).stdout.strip()
    remote_main = run(
        ["gh", "api", f"repos/{slug}/git/ref/heads/main", "--jq", ".object.sha"]
    ).stdout.strip()
    if remote_main != local_main:
        raise FixtureRecoveryError(
            f"main SHA differs: local={local_main}, remote={remote_main}"
        )
    return local_main


def _refs(output: str) -> dict[str, str]:
    return {
        parts[1]: parts[0]
        for line in output.splitlines()
        if len(parts := line.split("\t", 1)) == 2
    }


def _verify_required_refs(slug: str, checkout: Path) -> None:
    _assert_origin(slug, checkout)
    local = _refs(
        run(
            [
                "git",
                "for-each-ref",
                "--format=%(objectname)%09%(refname)",
                "refs/heads",
                "refs/tags",
            ],
            cwd=checkout,
        ).stdout
    )
    expected = {
        "refs/heads/main",
        "refs/heads/feature/work-package",
        "refs/heads/shared/work-package-ref",
        "refs/heads/ambiguous/work-package-ref",
        "refs/heads/shared/annotated-work-package-ref",
        "refs/heads/ambiguous/annotated-work-package-ref",
        "refs/heads/pull-request-source",
        "refs/tags/work-package-v1",
        "refs/tags/shared/work-package-ref",
        "refs/tags/ambiguous/work-package-ref",
        "refs/tags/shared/annotated-work-package-ref",
        "refs/tags/ambiguous/annotated-work-package-ref",
        "refs/tags/lightweight-tree-tag",
        "refs/tags/annotated-tree-tag",
    }
    if set(local) != expected:
        raise FixtureRecoveryError(
            f"required local refs differ: missing={sorted(expected - set(local))}, "
            f"unexpected={sorted(set(local) - expected)}"
        )
    remote = _refs(
        run(
            ["git", "ls-remote", "--refs", "--heads", "--tags", "origin"], cwd=checkout
        ).stdout
    )
    unexpected_remote = set(remote) - expected
    if unexpected_remote:
        raise FixtureRecoveryError(
            f"unexpected remote refs: {sorted(unexpected_remote)}"
        )
    changed = {
        name for name in expected if name in remote and remote[name] != local[name]
    }
    if changed:
        raise FixtureRecoveryError(
            f"remote ref object IDs differ from preserved local refs: {sorted(changed)}"
        )
    missing = sorted(expected - set(remote))
    if missing:
        run(["git", "push", "origin", *missing], cwd=checkout)
        remote = _refs(
            run(
                ["git", "ls-remote", "--refs", "--heads", "--tags", "origin"],
                cwd=checkout,
            ).stdout
        )
    for name in sorted(expected):
        if remote.get(name) != local[name]:
            raise FixtureRecoveryError(
                f"required ref is unpublished or changed: {name}; "
                f"local={local[name]}, remote={remote.get(name)}"
            )


def _parent_number(slug: str, number: int) -> int | None:
    value = run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            slug,
            "--json",
            "parent",
            "--jq",
            ".parent.number // empty",
        ]
    ).stdout.strip()
    return None if value == "" else int(value)


def attach(slug: str, parent: int, child: int, root: Path) -> None:
    operation_id = f"addSubIssue:{parent}->{child}"
    completed = _completed(root).get(operation_id)
    if _parent_number(slug, child) == parent:
        if completed is not None:
            saved = _objects(completed)["m000"]
            if saved["id"] != _node_id(slug, child) or saved["number"] != child:
                raise FixtureRecoveryError(
                    f"journaled parent link identity changed: {operation_id}"
                )
        return
    if completed is not None:
        raise FixtureRecoveryError(
            f"journaled parent link is absent or different: {operation_id}"
        )
    result = _batch_graphql(
        slug,
        root,
        "addSubIssue",
        {
            "issueId": str(_live_issue(slug, parent, root)["id"]),
            "subIssueId": str(_live_issue(slug, child, root)["id"]),
        },
        operation_id,
    )
    assert result["number"] == child, {
        "parent": parent,
        "child": child,
        "result": result,
    }
    root.joinpath("attached.txt").write_text(f"{parent} -> {child}\n", encoding="utf-8")


def managed(
    original: str = "ORIGINAL-CONTRACT-EXACT",
    requirements: str = "REQUIREMENTS-EXACT",
    plan: str = "PLAN-EXACT",
    tasks: str = "TASKS-EXACT",
) -> str:
    return (
        "<!-- itree:original-contract:start -->\n"
        f"{original}\n"
        "<!-- itree:original-contract:end -->\n"
        "<!-- itree:requirements:start -->\n"
        f"{requirements}\n"
        "<!-- itree:requirements:end -->\n"
        "<!-- itree:plan:start -->\n"
        f"{plan}\n"
        "<!-- itree:plan:end -->\n"
        "<!-- itree:tasks:start -->\n"
        f"{tasks}\n"
        "<!-- itree:tasks:end -->\n"
    )


def create(root: Path, output: Path) -> None:
    started = time.perf_counter()
    root.mkdir(parents=True, exist_ok=False)
    checkout = root / "repo"
    owner = run(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    slug = f"{owner}/itree-work-package-{os.getpid()}-{time.time_ns()}"
    run(["gh", "repo", "create", slug, "--private"])
    run(["gh", "repo", "clone", slug, str(checkout)])
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=checkout)
    run(["git", "config", "user.name", "itree acceptance"], cwd=checkout)
    run(["git", "config", "user.email", "dzackgarza@gmail.com"], cwd=checkout)
    for relative in (".specify/memory", "src/itree", "docs"):
        checkout.joinpath(relative).mkdir(parents=True, exist_ok=True)
    files = {
        "AGENTS.md": "Fixture agent rules: ORIGINAL-RULE-TEXT.\n",
        "INTENT.md": "Fixture intent: INTENT-GOAL-TEXT.\n",
        "CONTRIBUTING.md": "Fixture contribution rules: CONTRIBUTION-RULE-TEXT.\n",
        ".specify/memory/constitution.md": "Fixture constitution: CONSTITUTION-GOAL-TEXT.\n",
        "src/itree/WORKFLOWS.md": "Fixture workflow: ORGANIZATION-MODEL-TEXT.\n",
        "docs/primary.md": "Pinned source content: BEFORE-BRANCH-EDIT.\n",
        "docs/recursive.md": "ANCESTOR-ONLY-CITATION-MUST-NOT-BE-FETCHED\n",
        "docs/nested.md": "Fetched source includes one nested pointer: https://httpbin.org/status/418\n",
        "docs/missing-utf8.txt": "placeholder\n",
        "justfile": "test-commit:\n    @git diff --check --cached\n\ntest-push:\n    @git diff --check\n",
    }
    for relative, text in files.items():
        checkout.joinpath(relative).write_text(text, encoding="utf-8")
    large_text = "".join(
        f"LARGE-TEXT-{index:06d}-ABCDEFGHIJKLMNOPQRSTUVWXYZ\n" for index in range(40000)
    )
    assert len(large_text.encode("utf-8")) > 1_048_576
    checkout.joinpath("docs/large.md").write_text(large_text, encoding="utf-8")
    checkout.joinpath("docs/binary.dat").write_bytes(bytes((0, 1, 2, 255, 0, 128)))
    checkout.joinpath("docs/invalid.txt").write_bytes(b"invalid-text:\xff\xfe\n")
    checkout.joinpath("docs/image.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    run(["git", "add", "."], cwd=checkout)
    run(["git", "commit", "-m", "Create work-package fixture"], cwd=checkout)
    run(["git", "push", "-u", "origin", "main"], cwd=checkout)
    pinned = run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    checkout.joinpath("docs/primary.md").write_text(
        "Changed source content: AFTER-BRANCH-EDIT.\n", encoding="utf-8"
    )
    run(["git", "add", "docs/primary.md"], cwd=checkout)
    run(["git", "commit", "-m", "Change the unpinned source"], cwd=checkout)

    run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "https://github.com/github/spec-kit",
            "vendor/submodule",
        ],
        cwd=checkout,
    )
    run(
        [
            "git",
            "-C",
            "vendor/submodule",
            "checkout",
            "a4d571229e164e440f8950cac034607bae7b7518",
        ],
        cwd=checkout,
    )
    run(["git", "add", ".gitmodules", "vendor/submodule"], cwd=checkout)
    run(["git", "commit", "-m", "Add a real submodule content case"], cwd=checkout)

    run(["git", "checkout", "-b", "feature/work-package"], cwd=checkout)
    run(
        [
            "git",
            "commit",
            "--allow-empty",
            "-m",
            "Create slash-containing source branch",
        ],
        cwd=checkout,
    )

    run(["git", "tag", "work-package-v1"], cwd=checkout)

    run(["git", "checkout", "main"], cwd=checkout)
    run(["git", "branch", "shared/work-package-ref"], cwd=checkout)
    run(["git", "tag", "shared/work-package-ref"], cwd=checkout)

    run(["git", "branch", "ambiguous/work-package-ref", pinned], cwd=checkout)
    run(["git", "tag", "ambiguous/work-package-ref", "HEAD"], cwd=checkout)

    run(["git", "branch", "shared/annotated-work-package-ref"], cwd=checkout)
    run(
        [
            "git",
            "tag",
            "-a",
            "shared/annotated-work-package-ref",
            "-m",
            "Same commit annotated tag",
        ],
        cwd=checkout,
    )

    run(["git", "branch", "ambiguous/annotated-work-package-ref", pinned], cwd=checkout)
    run(
        [
            "git",
            "tag",
            "-a",
            "ambiguous/annotated-work-package-ref",
            "HEAD",
            "-m",
            "Different commit annotated tag",
        ],
        cwd=checkout,
    )

    tree_sha = run(["git", "rev-parse", "HEAD^{tree}"], cwd=checkout).stdout.strip()
    run(["git", "tag", "lightweight-tree-tag", tree_sha], cwd=checkout)
    run(
        [
            "git",
            "tag",
            "-a",
            "annotated-tree-tag",
            tree_sha,
            "-m",
            "Annotated tree tag",
        ],
        cwd=checkout,
    )

    run(["git", "checkout", "-b", "pull-request-source"], cwd=checkout)
    checkout.joinpath("docs/pull-request.md").write_text(
        "Real pull request content.\n", encoding="utf-8"
    )
    run(["git", "add", "docs/pull-request.md"], cwd=checkout)
    run(["git", "commit", "-m", "Create a real pull request"], cwd=checkout)
    run(
        [
            "git",
            "push",
            "origin",
            "refs/heads/*:refs/heads/*",
            "refs/tags/*:refs/tags/*",
        ],
        cwd=checkout,
    )
    run(["git", "checkout", "main"], cwd=checkout)
    _verify_required_refs(slug, checkout)
    head = _assert_default_main(slug, checkout)
    _issue_catalogs[(slug, root)] = []
    state = complete_fixture_objects(
        slug, root, checkout, pinned, head, large_text, started
    )
    output.write_text(
        json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def resume(slug: str, root: Path, output: Path) -> None:
    started = time.perf_counter()
    root.mkdir(parents=True, exist_ok=True)
    checkout = root / "repo"
    assert checkout.joinpath(".git").exists(), (
        f"preserved checkout is not a git repository: {checkout}"
    )
    remote = json.loads(
        run(["gh", "repo", "view", slug, "--json", "nameWithOwner,createdAt"]).stdout
    )
    assert remote["nameWithOwner"].casefold() == slug.casefold(), {
        "expected": slug,
        "found": remote,
    }
    pinned = run(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=checkout
    ).stdout.strip()
    run(["git", "checkout", "main"], cwd=checkout)
    _verify_required_refs(slug, checkout)
    head = _assert_default_main(slug, checkout)
    previous = (
        FixtureState.model_validate_json(output.read_text(encoding="utf-8"))
        if output.exists()
        else None
    )
    recovered = complete_fixture_objects(
        slug,
        root,
        checkout,
        pinned,
        head,
        checkout.joinpath("docs/large.md").read_text(encoding="utf-8"),
        started,
    )
    if previous is not None:
        previous_values = previous.model_dump(mode="json")
        recovered_values = recovered.model_dump(mode="json")
        previous_values.pop("setup_elapsed_seconds")
        recovered_values.pop("setup_elapsed_seconds")
        if previous_values != recovered_values:
            raise FixtureRecoveryError(
                "recovered live state differs from persisted fixture identity or content; "
                "inspect the difference instead of overwriting persisted object IDs"
            )
    state = recovered.model_copy(
        update={"setup_elapsed_seconds": time.perf_counter() - started}
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    selected = state.selected
    run(
        [
            "gh",
            "issue",
            "view",
            str(selected),
            "--repo",
            slug,
            "--json",
            "number,body,title",
        ]
    )
    run(
        [
            "gh",
            "issue",
            "view",
            str(state.pagination),
            "--repo",
            slug,
            "--json",
            "number,body,title",
        ]
    )
    print(str(output))


def complete_fixture_objects(
    slug: str,
    root: Path,
    checkout: Path,
    pinned: str,
    head: str,
    large_text: str,
    started: float,
) -> FixtureState:
    pull_number, _pull_url = create_pull_request(slug, root)
    run(["git", "checkout", "main"], cwd=checkout)

    roadmap_body = "ROADMAP-BODY-CONTRACT-EXACT\n"
    selected_body = managed(
        original=f"ORIGINAL-CONTRACT-EXACT\nPinned primary: https://github.com/{slug}/blob/{pinned}/docs/primary.md"
    )
    malformed_bodies = {
        "duplicate": managed()
        + "<!-- itree:tasks:start -->\nDUPLICATE\n<!-- itree:tasks:end -->\n",
        "nested": "<!-- itree:original-contract:start -->\n<!-- itree:requirements:start -->\nNESTED\n<!-- itree:original-contract:end -->\n<!-- itree:requirements:end -->\n<!-- itree:plan:start -->\nP\n<!-- itree:plan:end -->\n<!-- itree:tasks:start -->\nT\n<!-- itree:tasks:end -->\n",
        "unknown": "<!-- itree:unknown:start -->\nUNKNOWN\n<!-- itree:unknown:end -->\n",
        "partial": "<!-- itree:original-contract:start -->\nO\n<!-- itree:original-contract:end -->\n<!-- itree:requirements:start -->\nR\n<!-- itree:requirements:end -->\n",
        "reordered": "<!-- itree:requirements:start -->\nR\n<!-- itree:requirements:end -->\n<!-- itree:original-contract:start -->\nO\n<!-- itree:original-contract:end -->\n<!-- itree:plan:start -->\nP\n<!-- itree:plan:end -->\n<!-- itree:tasks:start -->\nT\n<!-- itree:tasks:end -->\n",
        "unterminated": "<!-- itree:original-contract:start -->\nUNTERMINATED\n",
    }
    fenced_body = "LEGACY-FENCED-BODY\n```text\n" + managed() + "```\n"
    legacy_body = "LEGACY-ZERO-MARKER-BODY-EXACT\n"
    cycle_a_body = "CYCLE-A-READABLE-BODY\n"
    cycle_b_body = "CYCLE-B-READABLE-BODY\n"
    specifications = [
        ("Roadmap: Work package acceptance", roadmap_body),
        ("Work package acceptance unit", selected_body),
        ("Cited issue source", "CITED-ISSUE-BODY-EXACT\n"),
        (
            "Citation transport edges",
            f"Shared source: https://github.com/{slug}/blob/shared/work-package-ref/docs/primary.md\n",
        ),
        ("Pagination acceptance", "LEGACY-PAGINATION-BODY-EXACT\n"),
        *(
            (f"Malformed {name}", f"MALFORMED-{name.upper()}-SELECTED-BODY\n" + body)
            for name, body in malformed_bodies.items()
        ),
        ("Fence-shielded markers", fenced_body),
        ("Legacy zero-marker body", legacy_body),
        ("Cycle A", cycle_a_body),
        ("Cycle B", cycle_b_body),
    ]
    create_issue_family(slug, root, specifications)
    assert len(specifications) == 15

    roadmap = issue(
        slug,
        "Roadmap: Work package acceptance",
        roadmap_body,
        root,
        self_referential=True,
    )
    recursive_url = f"https://github.com/{slug}/blob/{pinned}/docs/recursive.md"
    (roadmap_comment_id, roadmap_comment_url), _ = comments(
        slug,
        roadmap,
        [
            "ROADMAP-COMMENT-DECISION-EXACT\n",
            f"Ancestor-only citation: {recursive_url}\n",
        ],
        root,
    )

    selected = issue(
        slug,
        "Work package acceptance unit",
        selected_body,
        root,
        self_referential=True,
    )
    selected_body += f"Selected issue self-citation: https://github.com/{slug}/issues/{selected}\nParent citation: https://github.com/{slug}/issues/{roadmap}\n"
    roadmap_body += (
        f"Roadmap self-citation: https://github.com/{slug}/issues/{roadmap}\n"
    )
    ensure_issue_body(slug, selected, selected_body, root)
    ensure_issue_body(slug, roadmap, roadmap_body, root)
    attach(slug, roadmap, selected, root)
    (binding_id, binding_url), _, _ = comments(
        slug,
        selected,
        [
            "BINDING-PRIOR-DECISION-EXACT\n",
            "CORRECTIVE-DECISION-EXACT; the binding comment remains binding history.\n",
            "OPEN-VERIFICATION-FINDING-EXACT\n",
        ],
        root,
    )

    cited = issue(slug, "Cited issue source", "CITED-ISSUE-BODY-EXACT\n", root)
    cited_comment_id, cited_comment_url = comment(
        slug, cited, "CITED-COMMENT-BODY-EXACT\n", root
    )
    citation_edge = issue(
        slug,
        "Citation transport edges",
        f"Shared source: https://github.com/{slug}/blob/shared/work-package-ref/docs/primary.md\n",
        root,
    )

    markdown_url = f"https://raw.githubusercontent.com/dzackgarza/itree/{PIN}/README.md"
    plain_url = (
        f"https://raw.githubusercontent.com/dzackgarza/itree/{PIN}/pyproject.toml"
    )
    html_url = "https://httpbin.org/html"
    external_text: dict[str, str] = {}
    for name, url in (
        ("markdown", markdown_url),
        ("plain", plain_url),
        ("html", html_url),
    ):
        with urllib.request.urlopen(url, timeout=60) as response:
            external_text[name] = response.read().decode("utf-8")

    missing_external = "https://httpbin.org/status/404"
    redirect_url = "https://httpbin.org/redirect-to?" + urllib.parse.urlencode(
        {"url": missing_external, "status_code": "302"}
    )
    invalid_external = "https://httpbin.org/response-headers?" + urllib.parse.urlencode(
        {"Content-Type": "text/plain; charset=iso-8859-1"}
    )
    source_urls = {
        "markdown": markdown_url,
        "markdown_alias": markdown_url.replace(
            "raw.githubusercontent.com", "RAW.GITHUBUSERCONTENT.COM:443"
        ),
        "plain": plain_url,
        "plain_query": plain_url + "?download=1",
        "plain_fragment": plain_url + "?download=1#part-a",
        "plain_other_fragment": plain_url + "?download=1#part-b",
        "html": html_url,
        "inaccessible_external": missing_external,
        "redirect_failed": redirect_url,
        "invalid_external": invalid_external,
        "binary_external": "https://httpbin.org/bytes/32?seed=6501",
        "media_external": "https://httpbin.org/image/png",
        "pinned_blob": f"https://github.com/{slug}/blob/{pinned}/docs/primary.md",
        "main_blob": f"https://github.com/{slug}/blob/main/docs/primary.md",
        "slash_branch": f"https://github.com/{slug}/blob/feature/work-package/docs/primary.md",
        "slash_tag": f"https://github.com/{slug}/blob/work-package-v1/docs/primary.md",
        "shared": f"https://github.com/{slug}/blob/shared/work-package-ref/docs/primary.md",
        "shared_annotated": f"https://github.com/{slug}/blob/shared/annotated-work-package-ref/docs/primary.md",
        "lightweight_tree": f"https://github.com/{slug}/blob/lightweight-tree-tag/docs/primary.md",
        "annotated_tree": f"https://github.com/{slug}/blob/annotated-tree-tag/docs/primary.md",
        "ambiguous_annotated": f"https://github.com/{slug}/blob/ambiguous/annotated-work-package-ref/docs/primary.md",
        "ambiguous": f"https://github.com/{slug}/blob/ambiguous/work-package-ref/docs/primary.md",
        "unknown": f"https://github.com/{slug}/blob/unknown/work-package-ref/docs/primary.md",
        "not_found": f"https://github.com/{slug}/blob/{pinned}/docs/does-not-exist.md",
        "missing_commit": f"https://github.com/{slug}/blob/{'0' * 40}/docs/primary.md",
        "binary_blob": f"https://github.com/{slug}/blob/{pinned}/docs/binary.dat",
        "invalid_blob": f"https://github.com/{slug}/blob/{pinned}/docs/invalid.txt",
        "large_text": f"https://github.com/{slug}/blob/{head}/docs/large.md",
        "nested_source": f"https://github.com/{slug}/blob/{head}/docs/nested.md",
        "nested_target": "https://httpbin.org/status/418",
        "submodule": f"https://github.com/{slug}/blob/{head}/vendor/submodule",
        "directory": f"https://github.com/{slug}/tree/{pinned}/docs",
        "blob_directory": f"https://github.com/{slug}/blob/{pinned}/docs",
        "pull": f"https://github.com/{slug}/pull/999",
        "pull_issue": f"https://github.com/{slug}/issues/{pull_number}",
        "cited_issue": f"https://github.com/{slug}/issues/{cited}",
        "cited_issue_empty_query": f"https://github.com/{slug}/issues/{cited}?",
        "cited_issue_empty_fragment": f"https://github.com/{slug}/issues/{cited}#",
        "selected_issue": f"https://github.com/{slug}/issues/{selected}",
        "roadmap_issue": f"https://github.com/{slug}/issues/{roadmap}",
        "binding_self": binding_url,
        "roadmap_comment": roadmap_comment_url,
        "cited_comment": cited_comment_url,
        "cited_comment_subanchor": cited_comment_url + "#work-package-type-model",
        "malformed_comment_anchor": f"https://github.com/{slug}/issues/{cited}#issuecomment-not-a-number",
        "recursive": recursive_url,
    }
    citation_lines = [
        f"External Markdown: [{markdown_url}]({markdown_url}). Alias: {source_urls['markdown_alias']}",
        f"External plain: {source_urls['plain_query']} and {source_urls['plain_fragment']} plus {source_urls['plain_other_fragment']}",
        f"External HTML: <{html_url}>",
        f"External inaccessible: {missing_external}",
        f"Failed redirect: {redirect_url}",
        f"External invalid encoding: {invalid_external}",
        f"External binary: {source_urls['binary_external']}",
        f"External media: {source_urls['media_external']}",
        f"Main blob: {source_urls['main_blob']}",
        f"Slash branch: {source_urls['slash_branch']}",
        f"Slash tag: {source_urls['slash_tag']}",
        f"Shared same commit: {source_urls['shared']}",
        f"Shared annotated same commit: {source_urls['shared_annotated']}",
        f"Ambiguous annotated different commit: {source_urls['ambiguous_annotated']}",
        f"Ambiguous different commit: {source_urls['ambiguous']}",
        f"Unknown ref: {source_urls['unknown']}",
        f"Missing file: {source_urls['not_found']}",
        f"Missing full-SHA commit: {source_urls['missing_commit']}",
        f"Binary blob: {source_urls['binary_blob']}",
        f"Invalid blob: {source_urls['invalid_blob']}",
        f"Large encoding-none text: {source_urls['large_text']}",
        f"Nested source: {source_urls['nested_source']}",
        f"Submodule: {source_urls['submodule']}",
        f"Directory: {source_urls['directory']}",
        f"Blob directory: {source_urls['blob_directory']}",
        f"Pull request: {source_urls['pull']}",
        f"Pull request through issues: {source_urls['pull_issue']}",
        f"Cited issue: {source_urls['cited_issue']}",
        f"Cited comment: {cited_comment_url}",
        f"Cited comment with subanchor: {source_urls['cited_comment_subanchor']}",
        f"Malformed issuecomment anchor: {source_urls['malformed_comment_anchor']}",
        f"Empty query: {source_urls['cited_issue_empty_query']}",
        f"Empty fragment: {source_urls['cited_issue_empty_fragment']}",
        f"Selected comment self: {source_urls['binding_self']}",
        f"Roadmap comment citation: {source_urls['roadmap_comment']}",
        f"Lightweight tree tag: {source_urls['lightweight_tree']}",
        f"Annotated tree tag: {source_urls['annotated_tree']}",
    ]
    tail_comments = [(selected, "\n\n".join(citation_lines) + "\n")]

    pagination = issue(
        slug, "Pagination acceptance", "LEGACY-PAGINATION-BODY-EXACT\n", root
    )
    pagination_comments, pagination_ids = add_ordered_comments(
        slug,
        pagination,
        [f"COMMENT-{index:03d}-BODY-EXACT\n" for index in range(101)],
        root,
    )
    tail_comments.append((selected, f"Bare sibling reference: #{pagination}\n"))

    malformed: dict[str, int] = {}
    malformed_text: dict[str, str] = {}
    for name, body in malformed_bodies.items():
        distinctive = f"MALFORMED-{name.upper()}-SELECTED-BODY\n" + body
        malformed[name] = issue(slug, f"Malformed {name}", distinctive, root)
        malformed_text[name] = distinctive
        tail_comments.append((malformed[name], f"MALFORMED-{name.upper()}-COMMENT\n"))
    fenced_body = "LEGACY-FENCED-BODY\n```text\n" + managed() + "```\n"
    fenced = issue(slug, "Fence-shielded markers", fenced_body, root)
    tail_comments.append((fenced, "FENCED-COMMENT-EXACT\n"))
    legacy_body = "LEGACY-ZERO-MARKER-BODY-EXACT\n"
    legacy = issue(slug, "Legacy zero-marker body", legacy_body, root)
    tail_comments.append((legacy, "LEGACY-COMMENT-EXACT\n"))

    cycle_a_body = "CYCLE-A-READABLE-BODY\n"
    cycle_b_body = "CYCLE-B-READABLE-BODY\n"
    cycle_a = issue(slug, "Cycle A", cycle_a_body, root)
    cycle_b = issue(slug, "Cycle B", cycle_b_body, root)
    attach(slug, cycle_b, cycle_a, root)
    tail_comments.extend(
        (
            (cycle_a, "CYCLE-A-READABLE-COMMENT\n"),
            (cycle_b, "CYCLE-B-READABLE-COMMENT\n"),
        )
    )
    add_comment_tail(slug, root, tail_comments)

    real_gh = shutil.which("gh")
    assert real_gh is not None
    cycle_gh = root / "cycle-gh.py"
    cycle_family_response = {
        "data": {
            "repository": {
                "issue": {
                    "number": cycle_b,
                    "databaseId": cycle_b,
                    "title": "Cycle B",
                    "state": "OPEN",
                    "stateReason": None,
                    "body": "CYCLE-B-READABLE-BODY\n",
                    "url": f"https://github.com/{slug}/issues/{cycle_b}",
                    "milestone": None,
                    "labels": {"nodes": []},
                    "parent": {
                        "number": cycle_a,
                        "repository": {"nameWithOwner": slug},
                    },
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": 1,
                                "body": "CYCLE-B-READABLE-COMMENT\n",
                                "author": {"login": "itree-fixture"},
                                "createdAt": "2026-01-01T00:00:00Z",
                                "updatedAt": "2026-01-01T00:00:00Z",
                                "url": f"https://github.com/{slug}/issues/{cycle_b}#issuecomment-1",
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }
    cycle_gh.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"if 'graphql' in sys.argv and '-F' in sys.argv and 'number={cycle_b}' in sys.argv:\n"
        f"    print({json.dumps(json.dumps(cycle_family_response))})\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    cycle_gh.chmod(0o755)
    fail_comments_gh = root / "fail-comments-gh.py"
    fail_comments_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        f"if 'graphql' in sys.argv and 'number={selected}' in sys.argv and any('comments(first:100' in argument for argument in sys.argv):\n"
        "    sys.stderr.write('FIXTURE-REQUIRED-COMMENT-READ-FAILURE\\n')\n"
        "    sys.exit(1)\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    fail_comments_gh.chmod(0o755)
    assertion_gh = root / "assertion-gh.py"
    assertion_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "if 'graphql' in sys.argv:\n"
        '    print(\'{"data": null, "errors": [{"message": "FIXTURE-GRAPHQL-INVARIANT"}]}\')\n'
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    assertion_gh.chmod(0o755)
    citation_runtime_gh = root / "citation-runtime-gh.py"
    citation_runtime_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys, time\n"
        "if any('git/matching-refs' in argument for argument in sys.argv):\n"
        "    time.sleep(35)\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    citation_runtime_gh.chmod(0o755)
    citation_shape_gh = root / "citation-shape-gh.py"
    citation_shape_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "if any('git/matching-refs' in argument for argument in sys.argv):\n"
        '    print(\'{"message": "CITATION-MALFORMED-REF-PAGE"}\')\n'
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    citation_shape_gh.chmod(0o755)
    content_shape_gh = root / "content-shape-gh.py"
    content_shape_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "if any('/contents/docs/large.md' in argument for argument in sys.argv):\n"
        '    print(\'{"type": "file", "path": "docs/large.md", "sha": "0000000000000000000000000000000000000000"}\')\n'
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    content_shape_gh.chmod(0o755)
    malformed_issue_gh = root / "malformed-issue-gh.py"
    malformed_issue_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        f"if 'graphql' in sys.argv and ('number={selected}' in sys.argv or any(argument.startswith(('after=', 'endCursor=')) and argument not in {{'after=', 'endCursor='}} for argument in sys.argv)):\n"
        "    print('{not-json')\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    malformed_issue_gh.chmod(0o755)
    governing_shape_gh = root / "governing-shape-gh.py"
    malformed_governing_response = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": head}},
                "d000": {
                    "__typename": "Blob",
                    "oid": head,
                    "isTruncated": False,
                },
            }
        }
    }
    governing_shape_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "if any('WorkPackageGoverningDocuments' in argument for argument in sys.argv):\n"
        f"    print({json.dumps(json.dumps(malformed_governing_response))})\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    governing_shape_gh.chmod(0o755)
    base64_error_gh = root / "base64-error-gh.py"
    invalid_governing_text_response = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": head}},
                "d000": {
                    "__typename": "Blob",
                    "oid": head,
                    "text": None,
                    "isTruncated": False,
                },
            }
        }
    }
    base64_error_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "if any('WorkPackageGoverningDocuments' in argument for argument in sys.argv):\n"
        f"    print({json.dumps(json.dumps(invalid_governing_text_response))})\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    base64_error_gh.chmod(0o755)
    live_page_edit_gh = root / "live-page-edit-gh.py"
    live_page_edit_gh.write_text(
        "#!/usr/bin/env python3\nimport json, os, subprocess, sys\n"
        f"real_gh = {real_gh!r}\n"
        f"expected_number = {pagination}\n"
        "is_family = any('WorkPackageIssueFamily' in argument for argument in sys.argv)\n"
        "after = any(argument.startswith(('after=', 'endCursor=')) and argument not in {'after=', 'endCursor='} for argument in sys.argv)\n"
        "if is_family and not after:\n"
        "    first = subprocess.run([real_gh, *sys.argv[1:]], text=True, capture_output=True, check=False)\n"
        "    if first.returncode != 0:\n"
        "        sys.stdout.write(first.stdout)\n"
        "        sys.stderr.write(first.stderr)\n"
        "        sys.exit(first.returncode)\n"
        "    label = subprocess.run([real_gh, 'api', '-X', 'POST', "
        f"'repos/{slug}/labels', '-f', 'name=live-comment-page', '-f', 'color=0366d6'], text=True, capture_output=True, check=False)\n"
        "    if label.returncode != 0:\n"
        "        sys.stderr.write(label.stdout + label.stderr)\n"
        "        sys.exit(label.returncode)\n"
        "    edit = subprocess.run([real_gh, 'issue', 'edit', str(expected_number), '--repo', "
        f"{slug!r}, '--body', 'LIVE-BODY-EDIT-AFTER-FIRST-COMMENT-PAGE\\n', '--add-label', 'live-comment-page'], text=True, capture_output=True, check=False)\n"
        "    if edit.returncode != 0:\n"
        "        sys.stderr.write(edit.stdout + edit.stderr)\n"
        "        sys.exit(edit.returncode)\n"
        "    sys.stdout.write(first.stdout)\n"
        "    sys.stderr.write(first.stderr)\n"
        "    sys.exit(0)\n"
        "os.execv(real_gh, [real_gh, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    live_page_edit_gh.chmod(0o755)
    trace_gh = root / "trace-gh.py"
    trace_gh.write_text(
        "#!/usr/bin/env python3\nimport json, os, subprocess, sys\n"
        "input_text = sys.stdin.read() if '--input' in sys.argv else None\n"
        f"result = subprocess.run([{real_gh!r}, *sys.argv[1:]], input=input_text, text=True, capture_output=True, env={{**os.environ, 'GH_DEBUG': 'api'}})\n"
        "http = [line for line in result.stderr.splitlines() if line.startswith(('* Request', '> GET ', '> POST ', '> PATCH ', '> PUT ', '> DELETE '))]\n"
        "with open(os.environ['ITREE_GH_TRACE_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps({'argv': sys.argv[1:], 'http': http, 'exit': result.returncode}) + '\\n')\n"
        "sys.stdout.write(result.stdout)\n"
        "sys.stderr.write(result.stderr)\n"
        "sys.exit(result.returncode)\n",
        encoding="utf-8",
    )
    trace_gh.chmod(0o755)
    state = FixtureState(
        repo=slug,
        root=root,
        checkout=checkout,
        selected=selected,
        selected_body=selected_body,
        roadmap=roadmap,
        roadmap_body=roadmap_body,
        roadmap_comment_id=roadmap_comment_id,
        roadmap_comment_url=roadmap_comment_url,
        binding_comment_id=binding_id,
        binding_comment_url=binding_url,
        cited=cited,
        cited_comment_id=cited_comment_id,
        citation_edge=citation_edge,
        citation_runtime_gh=citation_runtime_gh,
        citation_shape_gh=citation_shape_gh,
        content_shape_gh=content_shape_gh,
        malformed_issue_gh=malformed_issue_gh,
        governing_shape_gh=governing_shape_gh,
        base64_error_gh=base64_error_gh,
        trace_gh=trace_gh,
        large_text_size=len(large_text.encode("utf-8")),
        pull_number=pull_number,
        pagination=pagination,
        pagination_comments=pagination_comments,
        pagination_ids=pagination_ids,
        malformed=malformed,
        malformed_text=malformed_text,
        fenced=fenced,
        fenced_body=fenced_body,
        legacy=legacy,
        legacy_body=legacy_body,
        cycle_a=cycle_a,
        cycle_b=cycle_b,
        cycle_gh=cycle_gh,
        fail_comments_gh=fail_comments_gh,
        assertion_gh=assertion_gh,
        pinned=pinned,
        head=head,
        source_urls=source_urls,
        external_text=external_text,
        setup_elapsed_seconds=time.perf_counter() - started,
        python=run(["python", "--version"]).stdout.strip(),
    )
    run(["gh", "repo", "view", slug, "--json", "nameWithOwner,defaultBranchRef"])
    run(["gh", "api", f"repos/{slug}/contents/AGENTS.md?ref={head}", "--jq", ".sha"])
    run(
        [
            "gh",
            "issue",
            "view",
            str(selected),
            "--repo",
            slug,
            "--json",
            "number,body,title",
        ]
    )
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    create_parser = subcommands.add_parser("create")
    create_parser.add_argument("--root", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    resume_parser = subcommands.add_parser("resume")
    resume_parser.add_argument("--slug", required=True)
    resume_parser.add_argument("--root", type=Path, required=True)
    resume_parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "create":
        create(arguments.root, arguments.output)
    if arguments.command == "resume":
        resume(arguments.slug, arguments.root, arguments.output)


if __name__ == "__main__":
    main()
