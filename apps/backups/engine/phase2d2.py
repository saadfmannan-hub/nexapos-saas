"""Phase 2D-2 package publication and successful plaintext staging cleanup."""

from __future__ import annotations

import uuid

from .canonical_manifest import CanonicalManifestProvider
from .context import BackupExecutionContext
from .contracts import (
    CanonicalManifestResult,
    ComponentExportReference,
    ComponentExportResult,
    MediaCaptureReference,
    ManifestReference,
    MediaCaptureResult,
    PackageBuildRequest,
    PackageBuildResult,
    Phase2D1Result,
    Phase2D2Request,
    Phase2D2Result,
)
from .deterministic_package import DeterministicPackageProvider
from .package_exceptions import (
    Phase2D2CoordinationError,
    Phase2D2EngineError,
    SuccessfulStagingCleanupError,
)
from .logical_export import SQLiteLogicalComponentExporter
from .media_capture import LocalFilesystemMediaCaptureProvider
from .workspace import WorkspaceReference


class Phase2D2Coordinator:
    """Publish a deterministic package, then clean only its successful inputs."""

    def __init__(
        self,
        *,
        component_exporter,
        media_capture_provider,
        manifest_provider,
        package_provider,
        failure_hook=None,
    ):
        if (
            type(component_exporter) is not SQLiteLogicalComponentExporter
            or type(media_capture_provider) is not LocalFilesystemMediaCaptureProvider
            or type(manifest_provider) is not CanonicalManifestProvider
            or type(package_provider) is not DeterministicPackageProvider
            or package_provider.component_exporter is not component_exporter
            or package_provider.media_capture_provider is not media_capture_provider
            or package_provider.manifest_provider is not manifest_provider
            or component_exporter.workspace_manager.root
            != media_capture_provider.workspace_manager.root
            or component_exporter.workspace_manager.root
            != manifest_provider.workspace_manager.root
            or component_exporter.workspace_manager.root
            != package_provider.workspace_manager.root
        ):
            raise Phase2D2CoordinationError()
        self.component_exporter = component_exporter
        self.media_capture_provider = media_capture_provider
        self.manifest_provider = manifest_provider
        self.package_provider = package_provider
        self.failure_hook = failure_hook

    def _run_hook(self, stage):
        if self.failure_hook is not None:
            self.failure_hook(stage)

    @staticmethod
    def _validate_request(request):
        if type(request) is not Phase2D2Request:
            raise Phase2D2CoordinationError()
        context = request.context
        phase2d1_result = request.phase2d1_result
        if (
            type(context) is not BackupExecutionContext
            or type(context.workspace_reference) is not WorkspaceReference
            or type(context.workspace_reference.identifier) is not uuid.UUID
            or type(phase2d1_result) is not Phase2D1Result
            or type(phase2d1_result.component_exports) is not tuple
            or not phase2d1_result.component_exports
            or type(phase2d1_result.media_captures) is not tuple
            or type(phase2d1_result.manifest) is not CanonicalManifestResult
            or type(phase2d1_result.manifest.reference) is not ManifestReference
            or type(phase2d1_result.manifest.reference.identifier) is not uuid.UUID
        ):
            raise Phase2D2CoordinationError()
        component_references = set()
        for item in phase2d1_result.component_exports:
            if (
                type(item) is not ComponentExportResult
                or type(item.reference) is not ComponentExportReference
                or type(item.reference.identifier) is not uuid.UUID
                or item.reference.identifier in component_references
            ):
                raise Phase2D2CoordinationError()
            component_references.add(item.reference.identifier)
        media_references = set()
        for item in phase2d1_result.media_captures:
            if (
                type(item) is not MediaCaptureResult
                or type(item.reference) is not MediaCaptureReference
                or type(item.reference.identifier) is not uuid.UUID
                or item.reference.identifier in media_references
            ):
                raise Phase2D2CoordinationError()
            media_references.add(item.reference.identifier)
        return context, phase2d1_result

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, Phase2D2EngineError):
            return exc
        return Phase2D2CoordinationError()

    def build(self, request: Phase2D2Request) -> Phase2D2Result:
        package_result = None
        result = None
        safe_error = None
        abort_error = None
        abort_traceback = None
        cleanup_abort = None
        cleanup_abort_traceback = None
        cleanup_incomplete = False
        package_publication_confirmed = False
        context = None
        phase2d1_result = None

        def record_hook_failure(stage):
            nonlocal safe_error
            nonlocal cleanup_abort
            nonlocal cleanup_abort_traceback
            try:
                self._run_hook(stage)
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                if cleanup_abort is None:
                    cleanup_abort = exc
                    cleanup_abort_traceback = exc.__traceback__
            except BaseException as exc:
                if safe_error is None:
                    safe_error = self._safe_error(exc)

        try:
            context, phase2d1_result = self._validate_request(request)
            package_result = self.package_provider.build_package(
                PackageBuildRequest(
                    context=context,
                    phase2d1_result=phase2d1_result,
                )
            )
            if type(package_result) is not PackageBuildResult:
                raise Phase2D2CoordinationError()
            self.package_provider.validate_package_evidence(
                context=context,
                result=package_result,
            )
            package_publication_confirmed = True
            self._run_hook("after_package_publication")
        except BaseException as exc:
            if isinstance(exc, Exception):
                safe_error = self._safe_error(exc)
            else:
                abort_error = exc
                abort_traceback = exc.__traceback__
        finally:
            # A failed package build leaves every Phase 2D-1 input intact. Once
            # publication succeeds, cleanup is attempted exhaustively in reverse
            # ownership order even when a later hook or interruption fires.
            if (
                package_publication_confirmed
                and package_result is not None
                and context is not None
                and phase2d1_result is not None
            ):

                def attempt(action):
                    nonlocal cleanup_abort
                    nonlocal cleanup_abort_traceback
                    nonlocal cleanup_incomplete
                    try:
                        if action() is not True:
                            cleanup_incomplete = True
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        cleanup_incomplete = True
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
                    except BaseException:
                        cleanup_incomplete = True

                attempt(
                    lambda: self.manifest_provider.cleanup_manifest(
                        context=context,
                        reference=phase2d1_result.manifest.reference,
                    )
                )
                record_hook_failure("after_manifest_staging_cleanup")

                for captured in reversed(phase2d1_result.media_captures):
                    attempt(
                        lambda captured=captured: self.media_capture_provider.cleanup_media_capture(
                            context=context,
                            reference=captured.reference,
                        )
                    )
                record_hook_failure("after_media_staging_cleanup")

                for component in reversed(phase2d1_result.component_exports):
                    attempt(
                        lambda component=component: self.component_exporter.cleanup_component_export(
                            context=context,
                            reference=component.reference,
                            require_exact_evidence=True,
                        )
                    )
                record_hook_failure("after_component_staging_cleanup")

                if not cleanup_incomplete:
                    try:
                        self.package_provider.validate_package_evidence(
                            context=context,
                            result=package_result,
                        )
                        self._run_hook("before_phase2d2_result_return")
                    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                        if cleanup_abort is None:
                            cleanup_abort = exc
                            cleanup_abort_traceback = exc.__traceback__
                    except BaseException as exc:
                        if safe_error is None:
                            safe_error = self._safe_error(exc)
                    else:
                        if safe_error is None and abort_error is None and cleanup_abort is None:
                            result = Phase2D2Result(package=package_result)

        if abort_error is not None:
            try:
                abort_error.cleanup_incomplete = bool(
                    cleanup_incomplete
                    or getattr(abort_error, "cleanup_incomplete", False)
                )
            except Exception:
                pass
            raise abort_error.with_traceback(abort_traceback)
        if cleanup_abort is not None:
            try:
                cleanup_abort.cleanup_incomplete = cleanup_incomplete
            except Exception:
                pass
            raise cleanup_abort.with_traceback(cleanup_abort_traceback)
        if cleanup_incomplete:
            raise SuccessfulStagingCleanupError(cleanup_incomplete=True)
        if safe_error is not None:
            safe_error.cleanup_incomplete = bool(
                getattr(safe_error, "cleanup_incomplete", False)
            )
            safe_error.__cause__ = None
            safe_error.__context__ = None
            raise safe_error.with_traceback(None) from None
        if result is None:
            raise Phase2D2CoordinationError()
        return result


__all__ = ["Phase2D2Coordinator"]
