from pathlib import Path

from itree.harness_work import (
    HarnessWorkBoundary,
    InvocationId,
    ItreeDistribution,
    ModelInputMatch,
    PackageChunk,
    PendingPackage,
    ProviderComparisonFailure,
    ProviderInputPayload,
    Sha256,
    Spawned,
    model_context_verified,
)

module_only = ItreeDistribution(
    distribution_name="itree",
    version="1",
    entrypoint_group="console_scripts",
    entrypoint_name="itree",
    entrypoint_value="itree.work:main",
    record_launcher_relpath=Path("module.py"),
    launcher_abspath=Path("/tmp/itree"),
    launcher_sha256=Sha256("0" * 64),
)


def missing_invocation(boundary: HarnessWorkBoundary) -> None:
    boundary.next()


def pending_finish(boundary: HarnessWorkBoundary, pending: PendingPackage) -> None:
    boundary.finish(pending)


def provider_is_not_spawned(payload: ProviderInputPayload) -> None:
    spawned: Spawned = payload


def evidence_without_payload(
    chunks: tuple[PackageChunk, ...],
) -> ModelInputMatch | ProviderComparisonFailure:
    return model_context_verified(chunks)
