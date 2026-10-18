import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

repo = Path("/Users/bmilk/code/project/agent-loom")
evidence = repo / "agentloom-sdk/qa/evidence/round8-fixes-20261018"
root = Path(tempfile.mkdtemp(prefix="agentloom-file-spi-wheel-", dir="/private/tmp"))
sdk = root / "sdk"
sdk.mkdir()
for name in ("pyproject.toml", "README.md"):
    shutil.copy2(repo / "agentloom-sdk" / name, sdk / name)
shutil.copytree(
    repo / "agentloom-sdk/src/agentloom",
    sdk / "src/agentloom",
    ignore=shutil.ignore_patterns("__pycache__"),
)
shutil.copytree(
    repo / "agentloom-sdk/tests/support/workspace_provider",
    root / "provider",
    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"),
)


def command(args, log):
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=60)
    (evidence / log).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (log, result.stdout, result.stderr)


command(
    [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--wheel-dir",
        str(root / "dist"),
        str(sdk),
        str(root / "provider"),
    ],
    "wheel-build.txt",
)
wheels = sorted((root / "dist").glob("*.whl"))
command(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-index",
        "--target",
        str(root / "installed"),
        *map(str, wheels),
    ],
    "wheel-install.txt",
)
wheel = next(p for p in wheels if p.name.startswith("agentloom_sdk-"))
with zipfile.ZipFile(wheel) as archive:
    verified = []
    for path in archive.namelist():
        if path.startswith("agentloom/") and not path.endswith("/"):
            assert archive.read(path) == (repo / "agentloom-sdk/src" / path).read_bytes(), path
            verified.append(path)
    assert "agentloom/workspace/loader.py" in verified

worker = root / "worker.py"
worker.write_text((repo / "agentloom-sdk/tests/support/workspace_spi_process.py").read_text())
tree = ast.parse(worker.read_text())


class LocalConfig(ast.NodeTransformer):
    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "FileProperties":
            node.args, node.keywords = [], []
        if isinstance(node.func, ast.Name) and node.func.id == "WorkspaceBinding":
            node.args = [
                ast.BinOp(ast.Name("root", ast.Load()), ast.Div(), ast.Constant("local-work"))
            ]
        return node


local_worker = root / "local_worker.py"
local_worker.write_text(ast.unparse(ast.fix_missing_locations(LocalConfig().visit(tree))))
env = {**os.environ, "PYTHONPATH": str(root / "installed"), "PYTHONDONTWRITEBYTECODE": "1"}
imported = subprocess.run(
    [sys.executable, "-c", "import agentloom; print(agentloom.__file__)"],
    cwd=root,
    env=env,
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()
assert imported == str(root / "installed/agentloom/__init__.py"), imported
checks = []


def probe(script, case, mode, phase, expected=0):
    case.mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(script), str(case), mode, phase],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == expected and not result.stderr, (
        mode,
        phase,
        result.stdout,
        result.stderr,
    )
    data = json.loads(result.stdout) if result.stdout else None
    checks.append(
        {
            "script": script.name,
            "mode": mode,
            "phase": phase,
            "returncode": result.returncode,
            "result": data,
        }
    )
    return data


for mode in ("react", "plan"):
    assert probe(worker, root / ("external-" + mode), mode, "all")["status"] == "succeeded"
    assert probe(local_worker, root / ("local-" + mode), mode, "all")["status"] == "succeeded"
    case = root / ("recovery-" + mode)
    probe(worker, case, mode, "cut", 75)
    saved = (case / "calls.jsonl").read_bytes()
    time.sleep(0.35)
    assert probe(worker, case, mode, "change") == {"error": "RECOVERY_BINDING_MISMATCH"}
    assert saved == (case / "calls.jsonl").read_bytes()
    assert probe(worker, case, mode, "resume")["status"] == "succeeded"
    calls = [json.loads(line) for line in (case / "calls.jsonl").read_text().splitlines()]
    methods = {call["method"] for call in calls if call["purpose"] == "tool"}
    assert {
        "read",
        "write",
        "list",
        "mkdir",
        "stat",
        "edit",
        "patch",
        "move",
        "delete",
        "glob",
        "grep",
    } <= methods
    writes = [call for call in calls if call["method"] == "write"]
    assert len(writes) == len({call["operation_id"] for call in writes}) == 3

summary = {
    "root": str(root),
    "wheel": str(wheel),
    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    "imported": imported,
    "source_files_match": len(verified),
    "checks": checks,
}
(evidence / "wheel-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(
    json.dumps(
        {
            "source_files_match": len(verified),
            "checks": len(checks),
            "summary": str(evidence / "wheel-summary.json"),
        }
    )
)
