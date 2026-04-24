#!/usr/bin/env python3
"""工具调用多轮对话压测脚本（Anthropic 协议版本）

与 OpenAI 版本对称，使用 Anthropic Messages API + SDK。

流程（每轮）：
  1. 发送用户消息（带工具定义）
  2. 等待模型返回 tool_use
  3. 模拟工具结果返回给模型
  4. 等待模型基于工具结果生成最终回复
  5. 验证各阶段输出是否符合预期

使用方式：
  uv run python py-e2e-tests/stress_test_tools_anthropic.py
  uv run python py-e2e-tests/stress_test_tools_anthropic.py --iterations 20 --parallel 5
  uv run python py-e2e-tests/stress_test_tools_anthropic.py --stream --iterations 15
  uv run python py-e2e-tests/stress_test_tools_anthropic.py --scenario 天气 --report report.json
"""

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from anthropic import Anthropic

BASE_URL = "http://127.0.0.1:5317/anthropic"
API_KEY = "sk-test"
MODELS = ["deepseek-default", "deepseek-expert"]
MAX_TOKENS = 4096

# ── 工具定义 ────────────────────────────────────────────────────────────────────

WEATHER_TOOL = {
    "type": "custom",
    "name": "get_weather",
    "description": "获取指定城市的天气信息，包括温度、湿度、风力等",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称，如北京、上海"},
        },
        "required": ["city"],
    },
}

SEARCH_TOOL = {
    "type": "custom",
    "name": "web_search",
    "description": "搜索互联网获取最新信息",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    },
}

# ── 测试场景 ────────────────────────────────────────────────────────────────────

SCENARIOS: list[dict[str, Any]] = [
    # ── 单轮场景 ──────────────────────────────────────────
    {
        "name": "天气查询",
        "system": "你是一个天气助手，使用 get_weather 工具查询天气。",
        "messages": [{"role": "user", "content": "北京今天天气怎么样？"}],
        "tools": [WEATHER_TOOL],
        "tool_choice": {"type": "auto"},
    },
    {
        "name": "多城市天气",
        "system": "你是一个天气助手，使用 get_weather 工具查询多个城市的天气。",
        "messages": [{"role": "user", "content": "比较一下北京、上海和深圳今天的天气。"}],
        "tools": [WEATHER_TOOL],
        "tool_choice": {"type": "auto"},
    },
    {
        "name": "混合工具",
        "system": "你是一个全能助手，可以使用 get_weather 和 web_search 工具。",
        "messages": [
            {"role": "user", "content": "北京今天天气如何？有什么好玩的景点推荐？"}
        ],
        "tools": [WEATHER_TOOL, SEARCH_TOOL],
        "tool_choice": {"type": "auto"},
    },
    {
        "name": "强制工具",
        "system": "你是一个天气助手，使用 get_weather 工具查询天气。",
        "messages": [{"role": "user", "content": "深圳今天天气怎么样？"}],
        "tools": [WEATHER_TOOL],
        "tool_choice": {"type": "any"},
    },
    # ── 多轮场景（含历史工具调用记录） ────────────────────
    {
        "name": "追问天气",
        "system": "你是一个天气助手，使用 get_weather 工具查询天气。",
        "messages": [
            {"role": "user", "content": "北京今天天气怎么样？"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_prev_bj",
                        "name": "get_weather",
                        "input": {"city": "北京"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_prev_bj",
                        "content": json.dumps(
                            {"city": "北京", "temperature": "25°C", "condition": "晴"},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "type": "text",
                        "text": "那上海呢？也帮我查一下上海的天气。",
                    },
                ],
            },
        ],
        "tools": [WEATHER_TOOL],
        "tool_choice": {"type": "auto"},
    },
    {
        "name": "基于数据推荐",
        "system": "你是一个旅游顾问，使用 get_weather 工具查询天气并给出建议。",
        "messages": [
            {"role": "user", "content": "北京、上海、广州今天天气怎么样？"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_prev_w1",
                        "name": "get_weather",
                        "input": {"city": "北京"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_prev_w2",
                        "name": "get_weather",
                        "input": {"city": "上海"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_prev_w3",
                        "name": "get_weather",
                        "input": {"city": "广州"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_prev_w1",
                        "content": json.dumps(
                            {"city": "北京", "temperature": "25°C", "condition": "晴"},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_prev_w2",
                        "content": json.dumps(
                            {"city": "上海", "temperature": "28°C", "condition": "多云"},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_prev_w3",
                        "content": json.dumps(
                            {"city": "广州", "temperature": "30°C", "condition": "阵雨"},
                            ensure_ascii=False,
                        ),
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "北京25°C晴朗，上海28°C多云，广州30°C有阵雨。",
            },
            {
                "role": "user",
                "content": "哪个城市最适合去公园野餐？需要再查一下详细天气吗？",
            },
        ],
        "tools": [WEATHER_TOOL],
        "tool_choice": {"type": "auto"},
    },
    {
        "name": "搜索+天气链",
        "system": "你是一个全能助手，可以使用 get_weather 和 web_search 工具。",
        "messages": [
            {"role": "user", "content": "北京有哪些必去的景点？"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_prev_search",
                        "name": "web_search",
                        "input": {"query": "北京必去景点推荐"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_prev_search",
                        "content": json.dumps(
                            {
                                "results": [
                                    {"title": "故宫", "snippet": "明清皇家宫殿"},
                                    {"title": "颐和园", "snippet": "皇家园林"},
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "type": "text",
                        "text": "这些景点今天适合去吗？帮我查一下北京的天气。",
                    },
                ],
            },
        ],
        "tools": [WEATHER_TOOL, SEARCH_TOOL],
        "tool_choice": {"type": "auto"},
    },
]

DEFAULT_ITERATIONS = 10
DEFAULT_PARALLEL = 1


@dataclass
class RunResult:
    """单轮压测结果"""

    scenario_name: str
    success: bool
    total_time: float
    assistant1_time: float
    assistant2_time: float
    tool_call_count: int
    tool_call_names: list[str]
    tool_call_args: list[dict]
    prompt_tokens: int
    completion_tokens: int
    final_content: str
    model: str = ""
    error: str = ""


@dataclass
class ScenarioStats:
    """单个场景统计"""

    name: str
    total: int
    success: int
    tool_triggered: int
    total_time: list[float]
    tool_calls_per_run: list[int]


@dataclass
class Report:
    """压测总报告"""

    scenarios: dict[str, ScenarioStats]
    all_results: list[RunResult]
    start_time: str
    end_time: str
    config: dict

    @property
    def total(self) -> int:
        return len(self.all_results)

    @property
    def success(self) -> int:
        return sum(1 for r in self.all_results if r.success)

    @property
    def failed(self) -> int:
        return self.total - self.success

    def print(self):
        rate = self.success / self.total * 100 if self.total else 0
        all_times = [r.total_time for r in self.all_results]
        success_times = [r.total_time for r in self.all_results if r.success]
        all_tokens = [r.completion_tokens for r in self.all_results if r.completion_tokens > 0]
        tool_call_counts = [r.tool_call_count for r in self.all_results]

        print(f"\n{'=' * 64}")
        print(f"  工具调用压测报告 (Anthropic)")
        print(f"  开始: {self.start_time}")
        print(f"  结束: {self.end_time}")
        print(f"  模型: {', '.join(self.config.get('models', ['?']))}")
        print(f"{'=' * 64}")
        print(f"  总运行:          {self.total}")
        print(f"  成功:            {self.success} ({rate:.1f}%)")
        print(f"  失败:            {self.failed}")
        print(f"  触发工具调用:    {sum(1 for r in self.all_results if r.tool_call_count > 0)}")
        if success_times:
            print(f"  成功平均耗时:     {statistics.mean(success_times):.2f}s")
        if all_times:
            print(f"  总平均耗时:       {statistics.mean(all_times):.2f}s")
            print(f"  最大耗时:         {max(all_times):.2f}s")
            print(f"  最小耗时:         {min(all_times):.2f}s")
            print(f"  P50:              {statistics.median(all_times):.2f}s")
            print(f"  P95:              {sorted(all_times)[int(len(all_times) * 0.95)]:.2f}s")
        if all_tokens:
            print(f"  平均 completion tokens: {statistics.mean(all_tokens):.0f}")
        if tool_call_counts:
            print(f"  平均 tool_calls / 轮:    {statistics.mean(tool_call_counts):.1f}")

        print(f"\n{'─' * 64}")
        print(f"  各场景统计:")
        print(f"  {'场景':12s} {'总数':>5s} {'成功':>5s} {'成功率':>7s} {'触发工具':>8s} {'平均耗时':>8s}")
        print(f"{'─' * 64}")
        for name, ss in sorted(self.scenarios.items()):
            trigger_rate = ss.tool_triggered / ss.total * 100
            avg_t = statistics.mean(ss.total_time) if ss.total_time else 0
            succ_rate = ss.success / ss.total * 100 if ss.total else 0
            print(f"  {name:12s} {ss.total:5d} {ss.success:5d} {succ_rate:6.1f}% "
                  f"{trigger_rate:6.1f}%  {avg_t:7.2f}s")
        print(f"{'─' * 64}")
        for i, r in enumerate(self.all_results):
            status = "✓" if r.success else "✗"
            tools = ",".join(r.tool_call_names) if r.tool_call_names else "-"
            err = f"  ERR: {r.error}" if r.error else ""
            model_short = r.model.replace("deepseek-", "ds-")
            print(f"  #{i + 1:3d} [{status}] {model_short:10s} {r.scenario_name:10s} "
                  f"{r.total_time:6.2f}s  tools={r.tool_call_count:2d}({tools:20s})  "
                  f"tok={r.completion_tokens:5d}{err}")
        print(f"{'=' * 64}\n")


def check_server() -> bool:
    """检查服务器是否可用"""
    try:
        client = _make_client()
        client.models.list(timeout=5)
        return True
    except Exception:
        return False


def _make_client() -> Anthropic:
    return Anthropic(
        base_url=BASE_URL,
        api_key=API_KEY,
        default_headers={"Authorization": f"Bearer {API_KEY}"},
        http_client=httpx.Client(timeout=120),
    )


def mock_tool_result(tool_use_id: str, name: str, input_args: dict) -> list[dict]:
    """根据工具调用生成模拟结果（Anthropic tool_result 格式）"""
    if name == "get_weather":
        city = input_args.get("city", "未知")
        data = {
            "city": city,
            "temperature": "25°C",
            "condition": "晴",
            "humidity": "45%",
            "wind": "东北风2级",
            "air_quality": "良好",
        }
        return [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(data, ensure_ascii=False),
            }
        ]

    if name == "web_search":
        query = input_args.get("query", "")
        return [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(
                    {
                        "results": [
                            {"title": f"关于 {query} 的推荐", "snippet": f"这是 {query} 的相关信息..."}
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        ]

    return [
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps({"result": "ok"}),
        }
    ]


def extract_tool_uses(msg: Any) -> list[tuple[str, str, dict]]:
    """从 Anthropic Message 中提取 (id, name, input) 列表"""
    results: list[tuple[str, str, dict]] = []
    for block in msg.content:
        if block.type == "tool_use":
            results.append((block.id, block.name, block.input))
    return results


def collect_text(msg: Any) -> str:
    """收集 Anthropic Message 中的全部 text 内容"""
    parts: list[str] = []
    for block in msg.content:
        if block.type == "text":
            parts.append(block.text)
    return "".join(parts)


def run_scenario(
    client: Anthropic, scenario: dict[str, Any], model: str, _idx: int, use_stream: bool = False
) -> RunResult:
    """执行一次完整的工具调用多轮对话"""
    name = scenario["name"]
    system = scenario.get("system", "")
    messages = list(scenario["messages"])
    tools = scenario["tools"]
    tool_choice = scenario.get("tool_choice", {"type": "auto"})

    start = time.time()
    total_input_tokens = 0
    total_output_tokens = 0

    create_kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
    )
    if system:
        create_kwargs["system"] = system

    try:
        # ── Turn 1: 用户消息 → 期望 tool_use ──
        t1 = time.time()
        if use_stream:
            msg1 = _stream_collect(client, **create_kwargs)
        else:
            msg1 = client.messages.create(**create_kwargs)
        assistant1_time = time.time() - t1

        total_input_tokens += msg1.usage.input_tokens
        total_output_tokens += msg1.usage.output_tokens

        tool_uses = extract_tool_uses(msg1)
        tc_count = len(tool_uses)
        tc_names = [tu[1] for tu in tool_uses]
        tc_args = [tu[2] for tu in tool_uses]

        if not tool_uses:
            final_content = collect_text(msg1)
            valid = bool(final_content.strip())
            return RunResult(
                scenario_name=name,
                success=valid,
                total_time=time.time() - start,
                assistant1_time=assistant1_time,
                assistant2_time=0,
                tool_call_count=0,
                tool_call_names=[],
                tool_call_args=[],
                prompt_tokens=total_input_tokens,
                completion_tokens=total_output_tokens,
                final_content=final_content,
                model=model,
                error="" if valid else "未触发工具调用且回复为空",
            )

        # ── Turn 2: 返回工具结果 → 期望最终回复 ──
        # 构造 tool_result 消息
        tool_result_blocks: list[dict] = []
        for tu_id, tu_name, tu_input in tool_uses:
            tool_result_blocks.extend(mock_tool_result(tu_id, tu_name, tu_input))
        turn2_messages = list(messages)
        turn2_messages.append({"role": "assistant", "content": [{"type": "text", "text": collect_text(msg1)}] if collect_text(msg1) else [{"type": "text", "text": ""}]})
        turn2_messages.append({"role": "user", "content": tool_result_blocks})

        t2 = time.time()
        if use_stream:
            msg2 = _stream_collect(
                client,
                model=model,
                max_tokens=MAX_TOKENS,
                messages=turn2_messages,
                system=system or None,
            )
        else:
            msg2 = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                messages=turn2_messages,
                system=system or None,
            )
        assistant2_time = time.time() - t2

        total_input_tokens += msg2.usage.input_tokens
        total_output_tokens += msg2.usage.output_tokens

        final_content = collect_text(msg2)
        valid = bool(final_content.strip())
        error = "" if valid else "工具结果回复后模型返回空内容"

        return RunResult(
            scenario_name=name,
            success=valid,
            total_time=time.time() - start,
            assistant1_time=assistant1_time,
            assistant2_time=assistant2_time,
            tool_call_count=tc_count,
            tool_call_names=tc_names,
            tool_call_args=tc_args,
            prompt_tokens=total_input_tokens,
            completion_tokens=total_output_tokens,
            final_content=final_content,
            model=model,
            error=error,
        )

    except Exception as e:
        return RunResult(
            scenario_name=name,
            success=False,
            total_time=time.time() - start,
            assistant1_time=0,
            assistant2_time=0,
            tool_call_count=0,
            tool_call_names=[],
            tool_call_args=[],
            prompt_tokens=0,
            completion_tokens=0,
            final_content="",
            model=model,
            error=str(e),
        )


def _stream_collect(client: Anthropic, **kwargs: Any) -> Any:
    """流式请求：收集 Anthropic stream events 并组装为 quasi-Message 对象"""
    # 移除 max_tokens if None — 它可能在用户 kwargs 里带了 None
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    content_blocks: list[dict] = []
    current_tool_use: dict | None = None
    input_tokens = 0
    output_tokens = 0
    stop_reason: str | None = None

    with client.messages.stream(**kwargs) as stream:
        for event in stream:
            if event.type == "message_start":
                if hasattr(event.message, "usage"):
                    input_tokens = event.message.usage.input_tokens or 0
                    output_tokens = event.message.usage.output_tokens or 0
            if event.type == "content_block_start":
                if event.content_block.type == "tool_use":
                    current_tool_use = {
                        "type": "tool_use",
                        "id": event.content_block.id,
                        "name": event.content_block.name,
                        "input": {},
                    }
                elif event.content_block.type == "text":
                    content_blocks.append(
                        {"type": "text", "text": event.content_block.text or ""}
                    )
            if event.type == "content_block_delta":
                if event.delta.type == "input_json_delta" and current_tool_use is not None:
                    partial = event.delta.partial_json
                    if partial:
                        # 累积累加 partial_json
                        current_tool_use["input"] = partial
                if event.delta.type == "text_delta":
                    if content_blocks and content_blocks[-1]["type"] == "text":
                        content_blocks[-1]["text"] += event.delta.text
            if event.type == "content_block_stop":
                if current_tool_use is not None:
                    try:
                        parsed = json.loads(current_tool_use["input"]) if isinstance(current_tool_use["input"], str) else current_tool_use["input"]
                        current_tool_use["input"] = parsed
                    except (json.JSONDecodeError, TypeError):
                        pass
                    content_blocks.append(current_tool_use)
                    current_tool_use = None
            if event.type == "message_delta":
                if hasattr(event.delta, "stop_reason"):
                    stop_reason = event.delta.stop_reason
                if hasattr(event, "usage") and event.usage:
                    output_tokens = event.usage.output_tokens or output_tokens

    # 组装 quasi-Message
    class FakeUsage:
        input_tokens: int = 0
        output_tokens: int = 0

        def __init__(self, inp: int, out: int):
            self.input_tokens = inp
            self.output_tokens = out

    class FakeBlock:
        type: str
        id: str = ""
        name: str = ""
        input: dict | None = None
        text: str = ""

    blocks: list[Any] = []
    for b in content_blocks:
        fb = FakeBlock()
        fb.type = b["type"]
        if b["type"] == "text":
            fb.text = b.get("text", "")
        elif b["type"] == "tool_use":
            fb.id = b.get("id", "")
            fb.name = b.get("name", "")
            fb.input = b.get("input", {})
        blocks.append(fb)

    class FakeMessage:
        content: list[Any]
        usage: Any
        stop_reason: str | None

        def __init__(self, content: list, usage: Any, stop_reason: str | None):
            self.content = content
            self.usage = usage
            self.stop_reason = stop_reason

    return FakeMessage(
        content=blocks,
        usage=FakeUsage(input_tokens, output_tokens),
        stop_reason=stop_reason,
    )


def build_report(results: list[RunResult], config: dict) -> Report:
    scenes: dict[str, list[RunResult]] = {}
    for r in results:
        scenes.setdefault(r.scenario_name, []).append(r)

    stats = {}
    for sname, sresults in scenes.items():
        stats[sname] = ScenarioStats(
            name=sname,
            total=len(sresults),
            success=sum(1 for r in sresults if r.success),
            tool_triggered=sum(1 for r in sresults if r.tool_call_count > 0),
            total_time=[r.total_time for r in sresults],
            tool_calls_per_run=[r.tool_call_count for r in sresults],
        )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return Report(
        scenarios=stats,
        all_results=results,
        start_time=config.get("start_time", now_str),
        end_time=now_str,
        config=config,
    )


def main():
    parser = argparse.ArgumentParser(
        description="工具调用多轮对话压测 (Anthropic)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  %(prog)s                             # 默认 10 轮顺序执行\n"
            "  %(prog)s --iterations 20 --parallel 5    # 20 轮 5 并发\n"
            "  %(prog)s --stream                       # 使用流式 API\n"
            "  %(prog)s --scenario 天气                # 仅运行天气场景\n"
            "  %(prog)s --report result.json           # 输出 JSON 报告\n"
        ),
    )
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS, help="每场景迭代次数")
    parser.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="并行数")
    parser.add_argument(
        "--models", type=str, nargs="*", default=MODELS,
        help=f"模型列表 (default: {' '.join(MODELS)})"
    )
    parser.add_argument("--scenario", type=str, default=None, help="仅运行指定场景（名称关键字匹配）")
    parser.add_argument("--stream", action="store_true", help="使用流式 API（默认非流式）")
    parser.add_argument("--report", type=str, default=None, help="输出 JSON 报告文件路径")
    args = parser.parse_args()

    if not check_server():
        print(f"[错误] 服务器不可用 ({BASE_URL})，请先启动: just e2e-serve")
        sys.exit(1)

    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in scenarios if args.scenario.lower() in s["name"].lower()]
        if not scenarios:
            print(f"[错误] 未找到匹配的场景: {args.scenario}")
            sys.exit(1)

    client = _make_client()
    models = args.models or MODELS
    all_count = len(scenarios) * len(models) * args.iterations
    config = {
        "models": models,
        "stream": args.stream,
        "iterations": args.iterations,
        "parallel": args.parallel,
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"\n工具调用压测 (Anthropic)")
    print(f"  模型: {', '.join(models)}")
    print(f"  模式: {'流式' if args.stream else '非流式'}")
    print(f"  场景: {len(scenarios)} 个 ({', '.join(s['name'] for s in scenarios)})")
    print(f"  迭代: {args.iterations} 次/场景/模型")
    print(f"  并行: {args.parallel}")
    print(f"  总计: {all_count} 次请求\n")

    all_results: list[RunResult] = []

    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = []
        for model in models:
            for scenario in scenarios:
                for i in range(args.iterations):
                    futures.append(executor.submit(run_scenario, client, scenario, model, i, args.stream))

        done = 0
        for future in as_completed(futures):
            done += 1
            all_results.append(future.result())
            if done % max(1, all_count // 10) == 0 or done == all_count:
                print(f"  进度: {done}/{all_count} ({done * 100 // all_count}%)", end="\r", flush=True)

    print(f"\n  完成!                                  ")

    report = build_report(all_results, config)
    report.print()

    if args.report:
        json_data = {
            "config": config,
            "summary": {
                "total": report.total,
                "success": report.success,
                "failed": report.failed,
                "success_rate": round(report.success / report.total * 100, 1),
            },
            "scenarios": {
                name: {
                    "total": ss.total,
                    "success": ss.success,
                    "tool_triggered": ss.tool_triggered,
                    "avg_time": round(statistics.mean(ss.total_time), 3),
                    "max_time": round(max(ss.total_time), 3),
                    "min_time": round(min(ss.total_time), 3),
                    "avg_tool_calls": round(statistics.mean(ss.tool_calls_per_run), 1),
                }
                for name, ss in report.scenarios.items()
            },
            "runs": [
                {
                    "scenario": r.scenario_name,
                    "success": r.success,
                    "total_time": round(r.total_time, 3),
                    "tool_call_count": r.tool_call_count,
                    "tool_call_names": r.tool_call_names,
                    "completion_tokens": r.completion_tokens,
                    "error": r.error,
                }
                for r in all_results
            ],
        }
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        print(f"  JSON 报告已输出: {args.report}")


if __name__ == "__main__":
    main()
