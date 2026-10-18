#!/usr/bin/env python3
"""Run the complete SDK/QA suite and save versioned, reusable local evidence."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "agentloom-sdk"
RESULTS = ROOT / ".agentloom/test-results"
VENV_PYTHON = (
    ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
)
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.is_file() else sys.executable


def save(path, value):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def snapshot():
    paths = {Path(__file__).resolve(), SDK / "pyproject.toml"}
    for directory in (SDK / "src", SDK / "tests", SDK / "qa"):
        for path in directory.rglob("*"):
            if not path.is_file() or any(
                part in {"__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
                or part.endswith(".egg-info")
                for part in path.relative_to(directory).parts
            ):
                continue
            if directory == SDK / "qa" and path.suffix != ".py":
                continue
            paths.add(path)
    for directory in (ROOT, SDK):
        for name in (
            "pyproject.toml",
            "pytest.ini",
            ".pytest.ini",
            "pytest.toml",
            ".pytest.toml",
            "setup.cfg",
            "tox.ini",
            "conftest.py",
        ):
            if (directory / name).is_file():
                paths.add(directory / name)
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def environment():
    code = (
        "import importlib.metadata as m,json,platform,sys;"
        "print(json.dumps({'python':sys.version,'executable':sys.executable,"
        "'platform':platform.platform(),"
        "'packages':dict(sorted((d.metadata['Name'],d.version) for d in m.distributions()))}))"
    )
    value = json.loads(
        subprocess.check_output([PYTHON, "-c", code], cwd=ROOT, text=True)
    )
    value["pytest_environment"] = {
        key: os.environ.get(key)
        for key in (
            "PYTEST_PLUGINS",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTHONPATH",
            "PYTHONHASHSEED",
        )
    }
    return value


def git_state():
    def read(*args):
        try:
            result = subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except OSError:
            return None

    return {"head": read("rev-parse", "HEAD"), "status": read("status", "--short")}


def counts(path):
    if not path.exists():
        return None
    try:
        cases = ET.parse(path).getroot().findall(".//testcase")
    except ET.ParseError:
        return None
    result = {"tests": len(cases)}
    for key, tag in (
        ("failed", "failure"),
        ("errors", "error"),
        ("skipped", "skipped"),
    ):
        result[key] = sum(case.find(tag) is not None for case in cases)
    result["passed"] = result["tests"] - sum(
        result[key] for key in ("failed", "errors", "skipped")
    )
    return result


def check_latest():
    latest = RESULTS / "latest.json"
    if not latest.exists():
        print("尚无测试结果。请先运行 scripts/test_all.py。")
        return 2
    try:
        summary = json.loads(latest.read_text(encoding="utf-8"))
        if not summary.get("completed"):
            print("最近一次测试未完成，需要重新运行。")
            return 2
        directory = Path(summary["directory"])
        saved = json.loads(
            (directory / "source-after.json").read_text(encoding="utf-8")
        )
        valid = (
            summary["sources_unchanged"]
            and saved == snapshot()
            and summary["environment"] == environment()
            and (directory / "pytest.log").is_file()
            and counts(directory / "junit.xml") == summary["counts"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        print("最近一次测试证据缺失或损坏，需要重新运行。")
        return 2
    print(
        "最近一次测试结果与当前源码、测试和环境匹配。"
        if valid
        else "最近一次测试结果已过期或不完整，需要重新运行。"
    )
    print(
        f"结果：{directory}\n统计：{json.dumps(summary['counts'], ensure_ascii=False)}"
    )
    return 0 if valid else 2


def run_all():
    started = datetime.now(timezone.utc)
    directory = RESULTS / started.strftime("%Y%m%d-%H%M%S-%fZ")
    directory.mkdir(parents=True)
    before = snapshot()
    save(directory / "source-before.json", before)
    command = [
        PYTHON,
        "-u",
        "-m",
        "pytest",
        "-q",
        "-ra",
        "--color=no",
        "-o",
        "addopts=",
        str(SDK / "tests"),
        str(SDK / "qa"),
        f"--junitxml={directory / 'junit.xml'}",
    ]
    summary = {
        "schema_version": 1,
        "directory": str(directory),
        "started_at": started.isoformat(),
        "command": command,
        "scope": ["agentloom-sdk/tests", "agentloom-sdk/qa"],
        "git_before": git_state(),
        "environment": environment(),
        "completed": False,
        "pytest_addopts_ignored": True,
    }
    save(directory / "summary.json", summary)
    # An interrupted run must not leave an older result marked as the latest run.
    save(RESULTS / "latest.json", summary)
    env = {**os.environ, "PYTEST_ADDOPTS": ""}
    print(f"运行全部 SDK 与 QA 测试，结果目录：{directory}", flush=True)
    clock = time.monotonic()
    with (directory / "pytest.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        def output(text):
            log.write(text)
            log.flush()
            print(text, end="", flush=True)

        try:
            for line in process.stdout:
                output(line)
            code = process.wait()
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            try:
                remaining, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                remaining, _ = process.communicate()
            output(remaining or "")
            code = 130
    after = snapshot()
    save(directory / "source-after.json", after)
    totals = counts(directory / "junit.xml")
    summary.update(
        finished_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=round(time.monotonic() - clock, 3),
        pytest_exit_code=code,
        counts=totals,
        completed=code in (0, 1) and totals is not None and totals["tests"] > 0,
        sources_unchanged=before == after,
        git_after=git_state(),
    )
    save(directory / "summary.json", summary)
    save(RESULTS / "latest.json", summary)
    print(f"\n测试摘要：{directory / 'summary.json'}")
    print(f"最近一次结果：{RESULTS / 'latest.json'}")
    if before != after:
        print("测试期间源码或测试发生变化，本次结果不能代表固定版本，请重新运行。")
    return code if code else (0 if summary["completed"] and before == after else 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="只核对最近一次结果是否仍适用，不运行测试"
    )
    args = parser.parse_args()
    return check_latest() if args.check else run_all()


if __name__ == "__main__":
    raise SystemExit(main())
