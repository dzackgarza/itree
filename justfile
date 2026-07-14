# itree - deterministic traversal over GitHub issue work-unit trees.
#
# The package and CLI live in pyproject.toml and src/itree.
# Quality control delegates to the central ai-review-ci Python gate.

# ai-review-ci contract variables consumed by doctor and workflow installers.
ai_review_ci_schema_version := "1"
ai_review_ci_profile := "python"
ai_review_ci_ref := "main"
ai_review_ci_release_channel := "main"
ai_review_ci_workflow_template_version := "1"
ai_review_ci_local_delegation := "global-justfile"
ai_review_ci_default_branch := "main"

# Show available recipes
default:
    @just --list

# Build the Python package distribution
build:
    @uv build

# Run the full local quality gate
test:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test

# Run the CI quality gate
test-ci:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-ci
