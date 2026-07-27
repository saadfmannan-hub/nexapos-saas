"""Deliberately disabled package boundary for Phase 2A."""

from .availability import PHASE_2A_DISABLED_REASON
from .contracts import PackageBuilder
from .exceptions import BackupEngineDisabled


class DisabledPackageBuilder(PackageBuilder):
    def build_package(self, request):
        raise BackupEngineDisabled(PHASE_2A_DISABLED_REASON)
