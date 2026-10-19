"""Architecture: a pack reaches the kernel through app.models and app.core, nothing above them."""
from __future__ import annotations

from arch import find_governed_files
from arch.import_allowlist import find_disallowed_imports

_KERNEL_A_PACK_MAY_IMPORT = {"app.models", "app.core", "app.packs"}


def test_a_pack_imports_only_models_core_and_packs() -> None:
    offenders = find_disallowed_imports(
        find_governed_files(__file__), roots={"app"}, allow=_KERNEL_A_PACK_MAY_IMPORT)
    assert not offenders, (
        "a pack hands the kernel bytes and declarations; it never drives a run, a "
        "service or a page, so it imports only app.models, app.core and its own "
        "package (absolute imports: a relative one is refused too):\n  "
        + "\n  ".join(offenders)
    )
