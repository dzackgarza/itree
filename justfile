ai_review_ci_schema_version := "1"
ai_review_ci_profile := "python"
ai_review_ci_ref := "main"
ai_review_ci_release_channel := "main"
ai_review_ci_workflow_template_version := "1"
ai_review_ci_local_delegation := "global-justfile"
ai_review_ci_default_branch := "main"

[doc("List available recipes")]
default:
    @just --list

[doc("Build the Python package distribution")]
build:
    @uv build

[doc("Run immediate commit-tier quality checks")]
test-commit:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-commit

[doc("Run the full project suite before pushing")]
test-push:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-push

[doc("Run the CI acceptance gate")]
test-ci:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-ci

[doc("Run the scheduled whole-tree audit (semgrep and debt scans) through the central implementation.")]
ambient:
    @just -f ~/ai-review-ci/justfiles/python.just -d . ambient
