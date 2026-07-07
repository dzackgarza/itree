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

# Run the full local quality gate
test:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test

# Run the CI quality gate
test-ci:
    @just -f ~/ai-review-ci/justfiles/python.just -d . test-ci
