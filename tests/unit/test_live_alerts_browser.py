"""Run the actual browser controller against hermetic Node capability mocks."""
import os
import shutil
import subprocess
from pathlib import Path


def test_live_alerts_browser_behavior() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the notification behavior gate"
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, "--test", str(root / "tests/browser/live_alerts.test.cjs")],
        cwd=root,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
