"""Guard the actual Railway parser restriction observed in native build logs."""

import re
from pathlib import Path


def test_railway_run_mounts_are_supported_cache_mounts() -> None:
    source = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    for flags in re.findall(r"--mount=([^\s\\]+)", source):
        options = dict(part.split("=", 1) for part in flags.split(",") if "=" in part)
        assert options.get("type") == "cache", (
            "Railway rejects all other RUN mount types before build"
        )
