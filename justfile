# itree - deterministic traversal over GitHub issue work-unit trees.
#
# The package and CLI live in pyproject.toml and src/itree.
# Quality control delegates to the central ai-review-ci Python gate.

# Show available recipes
default:
    @just --list

# Build the Python package distribution
build:
    @uv build

# Run immediate commit-tier quality checks
test-commit:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-commit

# Run the full project suite before pushing
test-push:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-push

# Run the CI acceptance gate
test-ci:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-ci
