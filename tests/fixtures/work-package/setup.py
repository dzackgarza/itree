#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

PIN = "0e80c9679e6f6bfd55e13cbcf193d297c5207117"

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)


def run(
    command: list[str], *, cwd: Path | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    result = subprocess.run(
        command, cwd=cwd, input=input_text, text=True, capture_output=True, check=False
    )
    elapsed = time.perf_counter() - started
    print(
        f"FIXTURE-STEP seconds={elapsed:.6f} exit={result.returncode} command={json.dumps(command)}",
        flush=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed after {elapsed:.3f}s: {command!r}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def issue(slug: str, title: str, body: str, root: Path) -> int:
    body_path = root / "issue-body.md"
    body_path.write_text(body, encoding="utf-8")
    url = run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            slug,
            "--title",
            title,
            "--body-file",
            str(body_path),
        ]
    ).stdout.strip()
    return int(url.rsplit("/", 1)[1])


def comment(slug: str, number: int, body: str) -> tuple[int, str]:
    response = json.loads(
        run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{slug}/issues/{number}/comments",
                "--input",
                "-",
            ],
            input_text=json.dumps({"body": body}),
        ).stdout
    )
    return int(response["id"]), str(response["html_url"])


def issue_id(slug: str, number: int) -> int:
    return int(
        run(["gh", "api", f"repos/{slug}/issues/{number}", "--jq", ".id"]).stdout
    )


def add_ordered_comments(
    slug: str, number: int, bodies: list[str]
) -> tuple[list[str], list[int]]:
    subject_id = run(
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
    fields = [
        f"m{index:03d}:addComment(input:{{subjectId:{json.dumps(subject_id)},body:{json.dumps(body)}}}){{commentEdge{{node{{databaseId url body}}}}}}"
        for index, body in enumerate(bodies)
    ]
    query = "mutation WorkPackageComments {" + " ".join(fields) + "}"
    response = json.loads(run(["gh", "api", "graphql", "-f", f"query={query}"]).stdout)
    assert "errors" not in response and isinstance(response.get("data"), dict), response
    ids: list[int] = []
    for index, expected_body in enumerate(bodies):
        comment_node = response["data"][f"m{index:03d}"]["commentEdge"]["node"]
        assert comment_node["body"] == expected_body, (index, comment_node)
        ids.append(int(comment_node["databaseId"]))
    return bodies, ids


def attach(slug: str, parent: int, child: int, root: Path) -> None:
    run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{slug}/issues/{parent}/sub_issues",
            "-F",
            f"sub_issue_id={issue_id(slug, child)}",
        ]
    )
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

    head = run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
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
    pull_url = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            slug,
            "--base",
            "main",
            "--head",
            "pull-request-source",
            "--title",
            "Fixture pull request",
            "--body",
            "Real pull request for source classification.",
        ],
        cwd=checkout,
    ).stdout.strip()
    pull_number = int(pull_url.rsplit("/", 1)[1])
    run(["git", "checkout", "main"], cwd=checkout)

    roadmap_body = "ROADMAP-BODY-CONTRACT-EXACT\n"
    roadmap = issue(slug, "Roadmap: Work package acceptance", roadmap_body, root)
    roadmap_comment_id, roadmap_comment_url = comment(
        slug, roadmap, "ROADMAP-COMMENT-DECISION-EXACT\n"
    )
    recursive_url = f"https://github.com/{slug}/blob/{pinned}/docs/recursive.md"
    comment(slug, roadmap, f"Ancestor-only citation: {recursive_url}\n")

    selected_body = managed(
        original=f"ORIGINAL-CONTRACT-EXACT\nPinned primary: https://github.com/{slug}/blob/{pinned}/docs/primary.md"
    )
    selected = issue(slug, "Work package acceptance unit", selected_body, root)
    selected_body += f"Selected issue self-citation: https://github.com/{slug}/issues/{selected}\nParent citation: https://github.com/{slug}/issues/{roadmap}\n"
    roadmap_body += (
        f"Roadmap self-citation: https://github.com/{slug}/issues/{roadmap}\n"
    )
    run(["gh", "issue", "edit", str(selected), "--repo", slug, "--body", selected_body])
    run(["gh", "issue", "edit", str(roadmap), "--repo", slug, "--body", roadmap_body])
    attach(slug, roadmap, selected, root)
    binding_id, binding_url = comment(slug, selected, "BINDING-PRIOR-DECISION-EXACT\n")
    comment(
        slug,
        selected,
        "CORRECTIVE-DECISION-EXACT; the binding comment remains binding history.\n",
    )
    comment(slug, selected, "OPEN-VERIFICATION-FINDING-EXACT\n")

    cited = issue(slug, "Cited issue source", "CITED-ISSUE-BODY-EXACT\n", root)
    cited_comment_id, cited_comment_url = comment(
        slug, cited, "CITED-COMMENT-BODY-EXACT\n"
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
    comment(slug, selected, "\n\n".join(citation_lines) + "\n")

    pagination = issue(
        slug, "Pagination acceptance", "LEGACY-PAGINATION-BODY-EXACT\n", root
    )
    pagination_comments, pagination_ids = add_ordered_comments(
        slug,
        pagination,
        [f"COMMENT-{index:03d}-BODY-EXACT\n" for index in range(101)],
    )
    comment(slug, selected, f"Bare sibling reference: #{pagination}\n")

    malformed_bodies = {
        "duplicate": managed()
        + "<!-- itree:tasks:start -->\nDUPLICATE\n<!-- itree:tasks:end -->\n",
        "nested": "<!-- itree:original-contract:start -->\n<!-- itree:requirements:start -->\nNESTED\n<!-- itree:original-contract:end -->\n<!-- itree:requirements:end -->\n<!-- itree:plan:start -->\nP\n<!-- itree:plan:end -->\n<!-- itree:tasks:start -->\nT\n<!-- itree:tasks:end -->\n",
        "unknown": "<!-- itree:unknown:start -->\nUNKNOWN\n<!-- itree:unknown:end -->\n",
        "partial": "<!-- itree:original-contract:start -->\nO\n<!-- itree:original-contract:end -->\n<!-- itree:requirements:start -->\nR\n<!-- itree:requirements:end -->\n",
        "reordered": "<!-- itree:requirements:start -->\nR\n<!-- itree:requirements:end -->\n<!-- itree:original-contract:start -->\nO\n<!-- itree:original-contract:end -->\n<!-- itree:plan:start -->\nP\n<!-- itree:plan:end -->\n<!-- itree:tasks:start -->\nT\n<!-- itree:tasks:end -->\n",
        "unterminated": "<!-- itree:original-contract:start -->\nUNTERMINATED\n",
    }
    malformed: dict[str, int] = {}
    malformed_text: dict[str, str] = {}
    for name, body in malformed_bodies.items():
        distinctive = f"MALFORMED-{name.upper()}-SELECTED-BODY\n" + body
        malformed[name] = issue(slug, f"Malformed {name}", distinctive, root)
        malformed_text[name] = distinctive
        comment(slug, malformed[name], f"MALFORMED-{name.upper()}-COMMENT\n")
    fenced_body = "LEGACY-FENCED-BODY\n```text\n" + managed() + "```\n"
    fenced = issue(slug, "Fence-shielded markers", fenced_body, root)
    comment(slug, fenced, "FENCED-COMMENT-EXACT\n")
    legacy_body = "LEGACY-ZERO-MARKER-BODY-EXACT\n"
    legacy = issue(slug, "Legacy zero-marker body", legacy_body, root)
    comment(slug, legacy, "LEGACY-COMMENT-EXACT\n")

    cycle_a_body = "CYCLE-A-READABLE-BODY\n"
    cycle_b_body = "CYCLE-B-READABLE-BODY\n"
    cycle_a = issue(slug, "Cycle A", cycle_a_body, root)
    cycle_b = issue(slug, "Cycle B", cycle_b_body, root)
    attach(slug, cycle_b, cycle_a, root)
    comment(slug, cycle_a, "CYCLE-A-READABLE-COMMENT\n")
    comment(slug, cycle_b, "CYCLE-B-READABLE-COMMENT\n")

    real_gh = shutil.which("gh")
    assert real_gh is not None
    cycle_gh = root / "cycle-gh.py"
    cycle_gh.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"if 'graphql' in sys.argv and '-F' in sys.argv and 'number={cycle_b}' in sys.argv:\n"
        f"    print(json.dumps({{'data': {{'repository': {{'issue': {{'parent': {{'number': {cycle_a}, 'repository': {{'nameWithOwner': '{slug}'}}}}}}}}}}}}))\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    cycle_gh.chmod(0o755)
    fail_comments_gh = root / "fail-comments-gh.py"
    fail_comments_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        f"if any('issues/{selected}/comments?' in argument for argument in sys.argv):\n"
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
        f"if any(argument.endswith('issues/{selected}') for argument in sys.argv if argument.startswith('repos/')):\n"
        "    print('{not-json')\n"
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    malformed_issue_gh.chmod(0o755)
    governing_shape_gh = root / "governing-shape-gh.py"
    governing_shape_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "if any('/contents/AGENTS.md' in argument for argument in sys.argv):\n"
        '    print(\'{"type": "file", "path": "AGENTS.md"}\')\n'
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    governing_shape_gh.chmod(0o755)
    base64_error_gh = root / "base64-error-gh.py"
    base64_error_gh.write_text(
        "#!/usr/bin/env python3\nimport os, sys\n"
        "if any('/contents/AGENTS.md' in argument for argument in sys.argv):\n"
        '    print(\'{"type": "file", "path": "AGENTS.md", "encoding": "base64", "content": "%%%not-base64%%%", "sha": "0000000000000000000000000000000000000000"}\')\n'
        "else:\n"
        f"    os.execv({real_gh!r}, [{real_gh!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    base64_error_gh.chmod(0o755)
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

    state: dict[str, JsonValue] = {
        "repo": slug,
        "root": str(root),
        "checkout": str(checkout),
        "selected": selected,
        "selected_body": selected_body,
        "roadmap": roadmap,
        "roadmap_body": roadmap_body,
        "roadmap_comment_id": roadmap_comment_id,
        "roadmap_comment_url": roadmap_comment_url,
        "binding_comment_id": binding_id,
        "binding_comment_url": binding_url,
        "cited": cited,
        "cited_comment_id": cited_comment_id,
        "citation_edge": citation_edge,
        "citation_runtime_gh": str(citation_runtime_gh),
        "citation_shape_gh": str(citation_shape_gh),
        "content_shape_gh": str(content_shape_gh),
        "malformed_issue_gh": str(malformed_issue_gh),
        "governing_shape_gh": str(governing_shape_gh),
        "base64_error_gh": str(base64_error_gh),
        "trace_gh": str(trace_gh),
        "large_text_size": len(large_text.encode("utf-8")),
        "pull_number": pull_number,
        "pagination": pagination,
        "pagination_comments": pagination_comments,
        "pagination_ids": pagination_ids,
        "malformed": malformed,
        "malformed_text": malformed_text,
        "fenced": fenced,
        "fenced_body": fenced_body,
        "legacy": legacy,
        "legacy_body": legacy_body,
        "cycle_a": cycle_a,
        "cycle_b": cycle_b,
        "cycle_gh": str(cycle_gh),
        "fail_comments_gh": str(fail_comments_gh),
        "assertion_gh": str(assertion_gh),
        "pinned": pinned,
        "head": head,
        "source_urls": source_urls,
        "external_text": external_text,
        "setup_elapsed_seconds": time.perf_counter() - started,
        "python": run(["python", "--version"]).stdout.strip(),
    }
    output.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
    print(str(output))


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    create_parser = subcommands.add_parser("create")
    create_parser.add_argument("--root", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "create":
        create(arguments.root, arguments.output)


if __name__ == "__main__":
    main()
