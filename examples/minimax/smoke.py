"""Run bounded, real MiniMax calls through the current AgentLoom SDK.

No fake/model fallback is installed here. The recording adapter delegates every
request unchanged to OpenAIChatAdapter. See README.md before running.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agentloom import (
    AgentDefinition,
    AgentLoom,
    AgentLoomError,
    AgentProperties,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    FunctionToolAdapter,
    ModelOptions,
    OpenAIChatAdapter,
    PropertiesConfig,
    RuntimeProperties,
    ServiceProperties,
    WorkspaceBinding,
)
from agentloom.configuration import json_copy

CASES = ("basic", "react", "plan", "schema")


def save_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class RecordingChatAdapter(OpenAIChatAdapter):
    """Observe SDK messages/results without modifying provider behavior."""

    def __init__(self, *, log_path, **kwargs):
        super().__init__(**kwargs)
        self.log_path = log_path
        self.calls = []

    async def stream_once(self, request, context):
        record = {
            "role": request.role_key,
            "purpose": request.purpose,
            "operation_id": request.operation_id,
            "messages": json_copy(request.messages),
            "tools": json_copy(request.tools),
            "max_output_tokens": request.max_output_tokens,
            "parameters": json_copy(request.parameters),
            "reasoning_chars": 0,
            "completed": False,
        }
        self.calls.append(record)
        print(f"  model call {len(self.calls)}: {request.role_key}", flush=True)
        try:
            async for event in super().stream_once(request, context):
                if event.kind == "reasoning_delta" and isinstance(event.data, str):
                    record["reasoning_chars"] += len(event.data)
                if event.kind == "completed":
                    record["completed"] = True
                    record["response"] = json_copy(event.data)
                yield event
        except AgentLoomError as exc:
            record["error_code"] = exc.code
            raise
        finally:
            # Never serialize the client, API key, Authorization or HTTP headers.
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")


def answer_text(value):
    """For the text smoke assertion only; never change SDK results or history.

    MiniMax can put a leading <think> block inside content. Keep that original
    content in the result and model-call logs. The schema case does not use this.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("<think>") and "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    return text


async def run_case(case, args, api_key, directory):
    expected = "MINIMAX_OK_" + uuid4().hex
    tool_calls = []

    async def read_probe(arguments, context):
        context.check_active()
        tool_calls.append(
            {
                "role": context.role_key,
                "operation_id": context.operation_id,
                "arguments": json_copy(arguments),
                "value": expected,
            }
        )
        print(f"  tool read_probe: role={context.role_key}", flush=True)
        return {"value": expected}

    registry = CapabilityRegistry(
        tools=(
            CapabilityRegistration(
                adapter_key="probe",
                adapter=FunctionToolAdapter(read_probe),
                members=(
                    CapabilitySpec(
                        capability_key="read_probe",
                        name="read_probe",
                        description="读取本次运行的随机校验值。无参数；返回 JSON，value 是原始校验值。",
                        input_schema={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    ),
                ),
            ),
        )
    )
    binding = CapabilityBinding(
        binding_key="read_probe",
        kind="tool",
        visible_name="read_probe",
        adapter_key="probe",
        member_key="read_probe",
    )
    worker = AgentDefinition(
        name="probe-worker",
        description="调用 read_probe 工具获取本次运行的随机校验值。",
        prompt="必须调用 read_probe。拿到工具结果后，只输出 value 字段原文，不解释、不猜测。",
        capabilities=(binding,),
    )
    if case in {"basic", "schema"}:
        schema = None
        if case == "schema":
            schema = {
                "type": "object",
                "properties": {"status": {"const": "ok"}, "value": {"type": "string"}},
                "required": ["status", "value"],
                "additionalProperties": False,
            }
        definition = AgentDefinition(
            name=f"minimax-{case}",
            prompt="严格按用户要求返回最终答案。",
            output_schema=schema,
        )
        input_text = (
            f"只输出 {expected}，不要 Markdown、解释或前后缀。"
            if case == "basic"
            else f'最终只输出 JSON 对象，status 为 "ok"，value 为 "{expected}"。'
        )
    elif case == "react":
        definition = worker
        input_text = "请调用 read_probe，最终只输出工具返回的 value 原文。"
    else:
        definition = AgentDefinition(
            name="probe-planner",
            prompt=(
                "必须使用 delegate_task 委派 worker 读取随机校验值。"
                "收到 worker 的实际结果后，只输出其中的校验值原文，不解释、不猜测。"
            ),
        )
        input_text = "请委派 worker 调用 read_probe，最终只输出它拿到的 value 原文。"

    model = RecordingChatAdapter(
        model=args.model,
        api_key=api_key,
        base_url=args.base_url,
        context_window_tokens=args.context_window,
        log_path=directory / f"{case}-model-calls.jsonl",
    )
    config = PropertiesConfig(
        runtime=RuntimeProperties(
            state_directory=directory / "state", namespace="minimax-smoke"
        ),
        defaults=AgentProperties(
            workspace=WorkspaceBinding(directory / "workspace"),
            execution_policy=ExecutionPolicy(
                loop_max=args.max_loops,
                run_timeout_seconds=args.timeout,
                model_first_output_timeout_seconds=60,
                model_stream_idle_timeout_seconds=60,
            ),
            model_options=ModelOptions(max_output_tokens=args.max_output_tokens),
        ),
        services=ServiceProperties(model=model, capabilities=registry),
    )
    async with AgentLoom(config) as app:
        agent = app.build(
            definition, executors={"worker": worker} if case == "plan" else None
        )
        handle = await agent.start(input_text)
        print(f"  run_id={handle.run_id}", flush=True)
        result = await handle.wait()

    value = json_copy(result.output.value) if result.output is not None else None
    checks = {
        "run_succeeded": result.status == "succeeded",
        "answer_matches": (
            value == {"status": "ok", "value": expected}
            if case == "schema"
            else answer_text(value) == expected
        ),
    }
    if case in {"react", "plan"}:
        # The expected value is absent from these prompts/tool definitions. Only
        # an actual local tool invocation can disclose it to the real model.
        checks["tool_executed"] = bool(tool_calls)
        checks["tool_role_correct"] = bool(tool_calls) and all(
            call["role"] == ("worker" if case == "plan" else "react")
            for call in tool_calls
        )
    if case == "plan":
        checks["planner_and_worker_called"] = {"planner", "worker"} <= {
            call["role"] for call in model.calls
        }
    report = {
        "case": case,
        "passed": all(checks.values()),
        "checks": checks,
        "model": args.model,
        "base_url": args.base_url,
        "context_window": args.context_window,
        "max_output_tokens": args.max_output_tokens,
        "max_loops": args.max_loops,
        "run_timeout_seconds": args.timeout,
        "run_id": result.run_id,
        "session_id": result.session_id,
        "status": result.status,
        "error": json_copy(result.error),
        "input": input_text,
        "expected": expected,
        "output": {"kind": result.output.kind, "value": value}
        if result.output
        else None,
        "usage": asdict(result.usage),
        "tool_calls": tool_calls,
        "model_call_count": len(model.calls),
        "inline_thinking_seen": any(
            "<think>" in call.get("response", {}).get("text", "")
            for call in model.calls
        ),
        "separate_reasoning_seen": any(call["reasoning_chars"] for call in model.calls),
    }
    save_json(directory / f"{case}-result.json", report)
    return report


def parse_args():
    parser = argparse.ArgumentParser(
        description="通过 AgentLoom 进行真实 MiniMax 冒烟测试。"
    )
    parser.add_argument("--case", choices=(*CASES, "all"), default="basic")
    parser.add_argument("--model", default=os.getenv("MINIMAX_MODEL", "MiniMax-M2.7"))
    parser.add_argument(
        "--base-url", default=os.getenv("MINIMAX_BASE_URL", "https://api.minimax.cn/v1")
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=os.getenv("MINIMAX_CONTEXT_WINDOW", "204800"),
    )
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--max-loops", type=int, default=8)
    parser.add_argument(
        "--timeout", type=int, default=180, help="每个 Run 的执行期限，单位秒"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".agentloom/live-minimax")
    )
    return parser.parse_args()


async def run(args, api_key, directory):
    reports = []
    for case in CASES if args.case == "all" else (args.case,):
        print(f"[{case}] starting", flush=True)
        report = await run_case(case, args, api_key, directory)
        reports.append(report)
        save_json(directory / "results.json", reports)
        label = "PASS" if report["passed"] else "FAIL"
        print(
            f"[{case}] {label}: status={report['status']}, usage={report['usage']}",
            flush=True,
        )
        if not report["passed"]:
            print(
                "首个失败用例后停止。详细原因见结果文件；可用 --case 单独运行其他用例。"
            )
            return 1
    return 0


def main():
    args = parse_args()
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key and sys.stdin.isatty():
        api_key = getpass.getpass("MiniMax API Key（隐藏输入，不写文件）: ").strip()
    if not api_key:
        print(
            "缺少 MINIMAX_API_KEY。请在本机交互式终端运行，或在本机设置此环境变量。",
            file=sys.stderr,
        )
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = args.output_dir.resolve() / f"{stamp}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    print(f"MiniMax: {args.model} @ {args.base_url}")
    print(f"结果目录: {directory}", flush=True)
    try:
        return asyncio.run(run(args, api_key, directory))
    except AgentLoomError as exc:
        print(f"启动或执行失败: {exc.code}；结果目录: {directory}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断本地运行。已有记录保留在结果目录。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
