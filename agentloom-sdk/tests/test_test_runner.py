"""Exercise the repository launcher against small, isolated real pytest suites."""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "scripts/test_all.py"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "test-project"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(LAUNCHER, root / "scripts/test_all.py")
    for suite in ("tests", "qa"):
        (root / "agentloom-sdk" / suite).mkdir(parents=True)
    # Both configuration and environment filters must be ignored by the full runner.
    (root / "agentloom-sdk/pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-k missing_selection"\n', encoding="utf-8"
    )
    (root / "observed").mkdir()
    return root


def runner_environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST_")}
    env["PYTEST_ADDOPTS"] = "-k another_missing_selection --maxfail=1"
    return env


def invoke(project, *args):
    return subprocess.run(
        [sys.executable, str(project / "scripts/test_all.py"), *args],
        cwd=project,
        env=runner_environment(),
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("workers", ["1", "2", "auto"])
def test_runner_parallel_and_serial_preserve_all_evidence(project, workers):
    (project / "agentloom-sdk/tests/test_probe.py").write_text(
        """import os
import time
from pathlib import Path
import pytest

@pytest.mark.parametrize('item', range(8))
def test_pass(item):
    root = Path(__file__).parents[2]
    (root / 'observed' / str(item)).write_text(str(os.getpid()))
    time.sleep(0.02)

def test_fail():
    assert False, 'intentional fixture failure'

@pytest.mark.skip(reason='intentional fixture skip')
def test_skip():
    pass
""",
        encoding="utf-8",
    )
    (project / "agentloom-sdk/qa/test_qa.py").write_text(
        "def test_qa():\n    assert True\n", encoding="utf-8"
    )
    # Exercise the CLI default as well as explicit serial and parallel overrides.
    args = () if workers == "auto" else ("--workers", workers)
    result = invoke(project, *args)
    assert result.returncode == 1, result.stdout + result.stderr
    summary = json.loads((project / ".agentloom/test-results/latest.json").read_text())
    directory = Path(summary["directory"])
    assert summary == json.loads((directory / "summary.json").read_text())
    assert summary["counts"] == {"tests": 11, "passed": 9, "failed": 1, "errors": 0, "skipped": 1}
    assert summary["completed"] and summary["sources_unchanged"]
    expected = min(4, os.cpu_count() or 1) if workers == "auto" else int(workers)
    assert summary["execution"] == {
        "mode": "parallel" if expected > 1 else "serial",
        "workers": expected,
        "distribution": "worksteal" if expected > 1 else None,
    }
    pids = {p.read_text() for p in (project / "observed").iterdir()}
    assert len(pids) == expected
    assert "intentional fixture failure" in (directory / "pytest.log").read_text()
    assert (directory / "junit.xml").is_file()
    # A failing but complete run remains reusable evidence, and --check runs no tests.
    before = (project / ".agentloom/test-results/latest.json").read_bytes()
    checked = invoke(project, "--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert (project / ".agentloom/test-results/latest.json").read_bytes() == before
    assert len(list((project / ".agentloom/test-results").iterdir())) == 2


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid"])
def test_invalid_worker_count_cannot_overwrite_previous_results(project, value):
    result = invoke(project, "--workers", value)
    assert result.returncode == 2
    assert "并发数必须" in result.stderr
    assert not (project / ".agentloom/test-results").exists()


def test_missing_parallel_dependency_preserves_previous_result(project, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "launcher_under_test", project / "scripts/test_all.py"
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    launcher.RESULTS.mkdir(parents=True)
    latest = launcher.RESULTS / "latest.json"
    latest.write_text('{"previous":true}', encoding="utf-8")
    monkeypatch.setattr(launcher, "environment", lambda: {"packages": {"pytest": "8.4.2"}})
    assert launcher.run_all(workers=2) == 2
    assert latest.read_text() == '{"previous":true}'
    assert "pytest-xdist" in capsys.readouterr().err
    assert len(list(launcher.RESULTS.iterdir())) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group interruption")
def test_interrupt_stops_parallel_workers_and_marks_evidence_incomplete(project):
    (project / "agentloom-sdk/tests/test_probe.py").write_text(
        """import json
import os
import time
from pathlib import Path
import pytest

@pytest.mark.parametrize('item', range(8))
def test_wait(item):
    root = Path(__file__).parents[2]
    (root / 'observed' / str(item)).write_text(json.dumps({'pid': os.getpid(), 'group': os.getpgrp()}))
    time.sleep(60)
""",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(project / "scripts/test_all.py"), "--workers", "2"],
        cwd=project,
        env=runner_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    groups = set()
    try:
        deadline = time.monotonic() + 15
        while len(list((project / "observed").iterdir())) < 2:
            assert process.poll() is None, process.communicate()[0]
            assert time.monotonic() < deadline, "workers did not start"
            time.sleep(0.05)
        workers = [json.loads(p.read_text()) for p in (project / "observed").iterdir()]
        groups = {w["group"] for w in workers}
        assert len(groups) == 1 and os.getpgrp() not in groups
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=15)
        assert process.returncode == 130, output
        summary = json.loads((project / ".agentloom/test-results/latest.json").read_text())
        assert not summary["completed"] and summary["pytest_exit_code"] == 130
        assert invoke(project, "--check").returncode == 2
        for worker in workers:
            with pytest.raises(ProcessLookupError):
                os.kill(worker["pid"], 0)
    finally:
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.communicate()
