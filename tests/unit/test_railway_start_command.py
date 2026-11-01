"""Reproduce Railway's exec-style startup override, including shell/PID behavior."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("port", ["8123", None])
def test_docker_override_expands_port_and_executes_server(tmp_path, port):
    command = json.loads((Path(__file__).resolve().parents[2] / "railway.json").read_text())[
        "deploy"
    ]["startCommand"]
    fake = tmp_path / "uvicorn"
    fake.write_text(
        f'#!{sys.executable}\nimport os,sys,json\nprint(json.dumps({{"args":sys.argv[1:],"pid":os.getpid()}}))\n'
    )
    fake.chmod(0o700)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    if port is None:
        env.pop("PORT", None)
    else:
        env["PORT"] = port
    process = subprocess.Popen(shlex.split(command), env=env, stdout=subprocess.PIPE, text=True)
    stdout, _ = process.communicate(timeout=5)
    assert process.returncode == 0
    result = json.loads(stdout)
    assert result["args"][result["args"].index("--port") + 1] == (port or "8000")
    assert result["args"][0] == "webapp.main:app"
    assert result["pid"] == process.pid
