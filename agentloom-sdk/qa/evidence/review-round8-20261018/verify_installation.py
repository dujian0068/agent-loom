"""Rebuild this checkout offline and exercise installed SPI packages in fresh processes."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
REPOSITORY = EVIDENCE.parents[3]
SDK = REPOSITORY / "agentloom-sdk"
SUMMARY = {"commands": [], "cases": []}


def run(command, *, cwd, env=None, expected=0):
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=90
    )
    entry = {
        "command": list(map(str, command)),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    SUMMARY["commands"].append(entry)
    assert completed.returncode == expected, entry
    return completed


def main():
    with tempfile.TemporaryDirectory(prefix="agentloom-review8-wheel-") as temporary:
        root = Path(temporary)
        sdk_source = root / "sdk"
        sdk_source.mkdir()
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(SDK / name, sdk_source / name)
        shutil.copytree(
            SDK / "src/agentloom",
            sdk_source / "src/agentloom",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        provider = root / "provider"
        shutil.copytree(
            SDK / "tests/support/workspace_provider",
            provider,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"),
        )
        wheels, installed = root / "wheels", root / "installed"
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                "-w",
                str(wheels),
                str(sdk_source),
                str(provider),
            ],
            cwd=root,
        )
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--target",
                str(installed),
                *map(str, sorted(wheels.glob("*.whl"))),
            ],
            cwd=root,
        )
        hashes = {}
        for source in sorted((SDK / "src/agentloom").rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts:
                continue
            relative = source.relative_to(SDK / "src")
            actual = installed / relative
            assert actual.read_bytes() == source.read_bytes(), relative
            hashes[str(relative)] = hashlib.sha256(actual.read_bytes()).hexdigest()
        SUMMARY["installed_files"] = hashes
        env = {**os.environ, "PYTHONPATH": str(installed)}
        identity = run(
            [
                sys.executable,
                "-c",
                (
                    "import json,sys,agentloom;from pathlib import Path;"
                    "from importlib import metadata;"
                    "assert Path(agentloom.__file__).is_relative_to(Path(sys.argv[1]));"
                    "eps=metadata.entry_points(group='agentloom.workspace_backends');"
                    "assert sum(e.name=='local' for e in eps)==1;"
                    "assert sum(e.name=='test-files' for e in eps)==1;"
                    "print(json.dumps({'imported':agentloom.__file__,"
                    "'entry_points':[e.name for e in eps]}))"
                ),
                str(installed),
            ],
            cwd=root,
            env=env,
        )
        SUMMARY["identity"] = json.loads(identity.stdout)
        worker = SDK / "tests/support/workspace_spi_process.py"
        for mode in ("react", "plan"):
            complete = root / (mode + "-all")
            complete.mkdir()
            result = run(
                [sys.executable, str(worker), str(complete), mode, "all"], cwd=root, env=env
            )
            assert json.loads(result.stdout)["status"] == "succeeded"
            SUMMARY["cases"].append({"mode": mode, "phase": "all-11-operations", "passed": True})

            recovered = root / (mode + "-recovered")
            recovered.mkdir()
            run(
                [sys.executable, str(worker), str(recovered), mode, "cut"],
                cwd=root,
                env=env,
                expected=75,
            )
            saved = (recovered / "calls.jsonl").read_bytes()
            assert sum(json.loads(line)["method"] == "write" for line in saved.splitlines()) == 1
            SUMMARY["cases"].append({"mode": mode, "phase": "crash-after-write", "passed": True})
            time.sleep(0.35)
            result = run(
                [sys.executable, str(worker), str(recovered), mode, "change"], cwd=root, env=env
            )
            assert json.loads(result.stdout) == {"error": "RECOVERY_BINDING_MISMATCH"}
            assert (recovered / "calls.jsonl").read_bytes() == saved
            SUMMARY["cases"].append(
                {"mode": mode, "phase": "reject-changed-options", "passed": True}
            )
            result = run(
                [sys.executable, str(worker), str(recovered), mode, "resume"], cwd=root, env=env
            )
            assert json.loads(result.stdout)["status"] == "succeeded"
            calls = [
                json.loads(line) for line in (recovered / "calls.jsonl").read_text().splitlines()
            ]
            writes = [item for item in calls if item["method"] == "write"]
            assert len(writes) == len({item["operation_id"] for item in writes}) == 3
            SUMMARY["cases"].append(
                {"mode": mode, "phase": "resume-without-duplicate-write", "passed": True}
            )
        SUMMARY["passed"] = True


try:
    main()
finally:
    (EVIDENCE / "installation.json").write_text(
        json.dumps(SUMMARY, ensure_ascii=False, indent=2) + "\n"
    )
print(
    json.dumps(
        {
            "passed": SUMMARY.get("passed", False),
            "source_files": len(SUMMARY.get("installed_files", {})),
            "cases": len(SUMMARY["cases"]),
        }
    )
)
