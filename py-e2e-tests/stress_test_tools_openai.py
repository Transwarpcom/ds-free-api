#!/usr/bin/env python3
"""工具调用多轮对话压测脚本

模拟完整的工具调用多轮对话流程，重复多次并统计成功率。

流程（每轮）：
  1. 发送用户消息（带工具定义）
  2. 等待模型返回 tool_calls
  3. 模拟工具结果返回给模型
  4. 等待模型基于工具结果生成最终回复
  5. 验证各阶段输出是否符合预期

使用方式：
  uv run python py-e2e-tests/stress_test_tools.py
  uv run python py-e2e-tests/stress_test_tools.py --iterations 20 --parallel 5
  uv run python py-e2e-tests/stress_test_tools.py --stream --iterations 15
  uv run python py-e2e-tests/stress_test_tools.py --scenario 天气 --report report.json
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

from openai import OpenAI

BASE_URL = "http://127.0.0.1:5317/v1"
API_KEY = "sk-test"
MODELS = ["deepseek-default", "deepseek-expert"]

# ── 工具定义 ────────────────────────────────────────────────────────────────────

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "获取指定城市的天气信息，包括温度、湿度、风力等",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称，如北京、上海"},
            },
            "required": ["city"],
        },
    },
}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网获取最新信息",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
}

# ── 测试场景 ────────────────────────────────────────────────────────────────────
# 场景包含两类：
#   单轮场景 — 消息历史为空（第零轮），直接提问触发工具
#   多轮场景 — 消息历史中已包含完整的工具调用→工具结果周期，测试模型
#               在上下文理解基础上的连续调用能力

SCENARIOS: list[dict[str, Any]] = [
    # ── 单轮场景 ──────────────────────────────────────────
    {
        "name": "天气查询",
        "system": "你是一个天气助手，使用 get_weather 工具查询天气。",
        "messages": [{"role": "user", "content": "北京今天天气怎么样？"}],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
    },
    {
        "name": "多城市天气",
        "system": "你是一个天气助手，使用 get_weather 工具查询多个城市的天气。",
        "messages": [{"role": "user", "content": "比较一下北京、上海和深圳今天的天气。"}],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
    },
    {
        "name": "混合工具",
        "system": "你是一个全能助手，可以使用 get_weather 和 web_search 工具。",
        "messages": [
            {"role": "user", "content": "北京今天天气如何？有什么好玩的景点推荐？"}
        ],
        "tools": [WEATHER_TOOL, SEARCH_TOOL],
        "tool_choice": "auto",
    },
    {
        "name": "强制工具",
        "system": "你是一个天气助手，使用 get_weather 工具查询天气。",
        "messages": [{"role": "user", "content": "深圳今天天气怎么样？"}],
        "tools": [WEATHER_TOOL],
        "tool_choice": "required",
    },
    # ── 多轮场景（含历史工具调用记录） ────────────────────
    {
        "name": "追问天气",
        "system": "你是一个天气助手，使用 get_weather 工具查询天气。",
        "messages": [
            # 历史：第一轮工具调用（查北京天气）
            {"role": "user", "content": "北京今天天气怎么样？"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_prev_weather_bj",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "北京"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_prev_weather_bj",
                "content": json.dumps(
                    {
                        "city": "北京",
                        "temperature": "25°C",
                        "condition": "晴",
                        "humidity": "45%",
                    },
                    ensure_ascii=False,
                ),
            },
            {
                "role": "assistant",
                "content": "北京今天天气晴朗，气温25°C，湿度45%，适合外出活动。",
            },
            # 当前：用户追问新城市
            {"role": "user", "content": "那上海呢？也帮我查一下上海的天气。"},
        ],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
    },
    {
        "name": "基于数据推荐",
        "system": "你是一个旅游顾问，使用 get_weather 工具查询天气并给出建议。",
        "messages": [
            # 历史：已查过三个城市的天气
            {"role": "user", "content": "北京、上海、广州今天天气怎么样？"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_prev_w1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "北京"}',
                        },
                    },
                    {
                        "id": "call_prev_w2",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "上海"}',
                        },
                    },
                    {
                        "id": "call_prev_w3",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "广州"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_prev_w1",
                "content": json.dumps(
                    {"city": "北京", "temperature": "25°C", "condition": "晴"},
                    ensure_ascii=False,
                ),
            },
            {
                "role": "tool",
                "tool_call_id": "call_prev_w2",
                "content": json.dumps(
                    {"city": "上海", "temperature": "28°C", "condition": "多云"},
                    ensure_ascii=False,
                ),
            },
            {
                "role": "tool",
                "tool_call_id": "call_prev_w3",
                "content": json.dumps(
                    {"city": "广州", "temperature": "30°C", "condition": "阵雨"},
                    ensure_ascii=False,
                ),
            },
            {
                "role": "assistant",
                "content": (
                    "北京25°C晴朗，上海28°C多云，广州30°C有阵雨。"
                    "北京和上海更适合户外活动。"
                ),
            },
            # 当前：基于已有数据追问
            {"role": "user", "content": "哪个城市最适合去公园野餐？需要再查一下详细天气吗？"},
        ],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
    },
    {
        "name": "搜索+天气链",
        "system": "你是一个全能助手，可以使用 get_weather 和 web_search 工具。",
        "messages": [
            # 历史：搜索景点
            {"role": "user", "content": "北京有哪些必去的景点？"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_prev_search",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "北京必去景点推荐"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_prev_search",
                "content": json.dumps(
                    {
                        "query": "北京必去景点推荐",
                        "results": [
                            {"title": "故宫", "snippet": "明清两代的皇家宫殿"},
                            {"title": "颐和园", "snippet": "皇家园林博物馆"},
                            {"title": "长城", "snippet": "世界文化遗产"},
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
            {
                "role": "assistant",
                "content": "北京必去的景点包括故宫、颐和园和长城，都是世界级的文化遗产。",
            },
            # 当前：结合搜索结果的天气查询
            {
                "role": "user",
                "content": "这些景点今天适合去吗？帮我查一下北京的天气。",
            },
        ],
        "tools": [WEATHER_TOOL, SEARCH_TOOL],
        "tool_choice": "auto",
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
    tool_triggered: int  # 成功触发工具调用的次数
    total_time: list[float]
    tool_calls_per_run: list[int]


@dataclass
class Report:
    """压测总报告"""

    scenarios: dict[str, ScenarioStats]
    all_results: list[RunResult]
    start_time: str
    end_time: str
    config: dict  # 压测配置参数

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
        all_tokens = [
            r.completion_tokens for r in self.all_results if r.completion_tokens > 0
        ]
        tool_call_counts = [r.tool_call_count for r in self.all_results]

        print(f"\n{'=' * 64}")
        print(f"  工具调用压测报告")
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
            print(f"  P50 (中位数):     {statistics.median(all_times):.2f}s")
            print(f"  P95:              {sorted(all_times)[int(len(all_times) * 0.95)]:.2f}s")
        if all_tokens:
            print(f"  平均 completion tokens: {statistics.mean(all_tokens):.0f}")
        if tool_call_counts:
            avg_tc = statistics.mean(tool_call_counts)
            print(f"  平均 tool_calls / 轮:    {avg_tc:.1f}")

        # 各场景详情
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
    """检查服务器是否可用（使用认证客户端）"""
    try:
        client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
        client.models.list(timeout=5)
        return True
    except Exception:
        return False


def mock_tool_result(tool_call: Any) -> str:
    """根据工具调用的 name 和参数生成模拟结果"""
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError:
        args = {}

    if name == "get_weather":
        city = args.get("city", "未知")
        data = {
            "city": city,
            "temperature": "25°C",
            "condition": "晴",
            "humidity": "45%",
            "wind": "东北风2级",
            "air_quality": "良好",
        }
        return json.dumps(data, ensure_ascii=False)

    if name == "web_search":
        query = args.get("query", "")
        return json.dumps(
            {
                "query": query,
                "results": [
                    {
                        "title": f"关于 {query} 的推荐",
                        "snippet": f"这是 {query} 的相关信息...",
                    }
                ],
            },
            ensure_ascii=False,
        )

    return json.dumps({"result": "ok"})


def make_tool_results_messages(tool_calls: list[Any]) -> list[dict]:
    """构造 tool_results 消息列表"""
    return [
        {
            "role": "tool",
            "tool_call_id": tc.id,
            "content": mock_tool_result(tc),
        }
        for tc in tool_calls
    ]


def run_scenario(
    client: OpenAI, scenario: dict[str, Any], model: str, _idx: int, use_stream: bool = False
) -> RunResult:
    """执行一次完整的工具调用多轮对话"""
    name = scenario["name"]
    system = scenario.get("system", "")
    messages = list(scenario["messages"])
    tools = scenario["tools"]
    tool_choice = scenario.get("tool_choice", "auto")
    create_kwargs: dict[str, Any] = dict(
        model=model,
        messages=(
            [{"role": "system", "content": system}, *messages]
            if system
            else messages
        ),
        tools=tools,
        tool_choice=tool_choice,
        temperature=0.7,
        stream=False,
    )

    start = time.time()
    total_prompt = 0
    total_completion = 0

    try:
        # ── Turn 1: 用户消息 → 期望 tool_calls ──
        t1 = time.time()
        if use_stream:
            resp1 = _stream_collect(client, **create_kwargs)
        else:
            resp1 = client.chat.completions.create(**create_kwargs)
        assistant1_time = time.time() - t1

        if resp1.usage:
            total_prompt += resp1.usage.prompt_tokens or 0
            total_completion += resp1.usage.completion_tokens or 0

        choice1 = resp1.choices[0]
        msg1 = choice1.message
        tool_calls = msg1.tool_calls or []
        tc_count = len(tool_calls)
        tc_names = [tc.function.name for tc in tool_calls]
        tc_args = [
            json.loads(tc.function.arguments) if tc.function.arguments else {}
            for tc in tool_calls
        ]

        # 如果没有触发工具调用且非 required 模式，视本轮为有效但无工具
        if not tool_calls:
            final_content = msg1.content or ""
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
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                final_content=final_content,
                model=model,
                error="" if valid else "未触发工具调用且回复为空",
            )

        # ── Turn 2: 返回工具结果 → 期望最终回复 ──
        turn2_msgs = list(create_kwargs["messages"])
        turn2_msgs.append(msg1.model_dump())
        turn2_msgs.extend(make_tool_results_messages(tool_calls))

        t2 = time.time()
        if use_stream:
            resp2 = _stream_collect(
                client,
                model=model,
                messages=turn2_msgs,
                temperature=0.7,
            )
        else:
            resp2 = client.chat.completions.create(
                model=model, messages=turn2_msgs, temperature=0.7, stream=False
            )
        assistant2_time = time.time() - t2

        if resp2.usage:
            total_prompt += resp2.usage.prompt_tokens or 0
            total_completion += resp2.usage.completion_tokens or 0

        final_content = resp2.choices[0].message.content or ""
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
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
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


def _stream_collect(client: OpenAI, **kwargs: Any) -> Any:
    """流式请求：收集所有 chunks 并组装为 quasi-Response 对象"""
    stream = client.chat.completions.create(**{**kwargs, "stream": True})

    content_parts: list[str] = []
    tool_call_acc: dict[int, dict] = {}
    finish_reason: str | None = None
    usage: Any = None
    msg_role: str = "assistant"

    for chunk in stream:
        if chunk.usage:
            usage = chunk.usage
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        if choice.delta.role:
            msg_role = choice.delta.role

        if choice.delta.content:
            content_parts.append(choice.delta.content)

        # 累积 tool_calls 碎片
        if choice.delta.tool_calls:
            for tc in choice.delta.tool_calls:
                idx = tc.index
                if idx not in tool_call_acc:
                    tool_call_acc[idx] = {
                        "index": idx,
                        "id": tc.id or "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.id:
                    tool_call_acc[idx]["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        tool_call_acc[idx]["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        tool_call_acc[idx]["function"]["arguments"] += tc.function.arguments

    # 组装 quasi-response 对象
    tool_calls_list = sorted(tool_call_acc.values(), key=lambda x: x["index"])

    class FakeUsage:
        prompt_tokens: int = 0
        completion_tokens: int = 0

        def __init__(self, u: Any) -> None:
            if u is not None:
                self.prompt_tokens = u.prompt_tokens or 0
                self.completion_tokens = u.completion_tokens or 0

    class FakeChoice:
        class FakeMessage:
            content: str | None
            tool_calls: list[Any]
            role: str

            def __init__(self, content: str | None, tool_calls: list[Any], role: str):
                self.content = content
                self.tool_calls = tool_calls
                self.role = role

            def model_dump(self) -> dict:
                d: dict[str, Any] = {"role": self.role, "content": self.content}
                if self.tool_calls:
                    d["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": tc.get("type", "function"),
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in self.tool_calls
                    ]
                return d

        finish_reason: str | None
        message: FakeMessage

        def __init__(
            self,
            finish: str | None,
            content: str | None,
            tool_calls: list[Any],
            role: str,
        ):
            self.finish_reason = finish
            self.message = self.FakeMessage(content, tool_calls, role)

    class FakeResponse:
        choices: list[FakeChoice]
        usage: Any

        def __init__(self, choices: list[FakeChoice], usage: Any):
            self.choices = choices
            self.usage = FakeUsage(usage)

    content = "".join(content_parts) or None
    return FakeResponse(
        choices=[
            FakeChoice(finish_reason, content, tool_calls_list, msg_role)
        ],
        usage=usage,
    )


def build_report(results: list[RunResult], config: dict) -> Report:
    """汇总结果为报告"""
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
        description="工具调用多轮对话压测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  %(prog)s                            # 默认 10 轮顺序执行\n"
            "  %(prog)s --iterations 20 --parallel 5   # 20 轮 5 并发\n"
            "  %(prog)s --stream                      # 使用流式 API\n"
            "  %(prog)s --scenario 天气               # 仅运行天气场景\n"
            "  %(prog)s --report result.json          # 输出 JSON 报告\n"
        ),
    )
    parser.add_argument(
        "--iterations", type=int, default=DEFAULT_ITERATIONS, help="每场景迭代次数"
    )
    parser.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="并行数")
    parser.add_argument(
        "--models", type=str, nargs="*", default=MODELS,
        help=f"模型列表 (default: {' '.join(MODELS)})"
    )
    parser.add_argument(
        "--scenario", type=str, default=None, help="仅运行指定场景（名称关键字匹配）"
    )
    parser.add_argument(
        "--stream", action="store_true", help="使用流式 API（默认非流式）"
    )
    parser.add_argument(
        "--report", type=str, default=None, help="输出 JSON 报告文件路径"
    )
    parser.add_argument(
        "--tool-choice",
        type=str,
        default=None,
        choices=["auto", "required", "none"],
        help="覆盖所有场景的 tool_choice",
    )
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

    if args.tool_choice:
        for s in scenarios:
            s["tool_choice"] = args.tool_choice

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    config = {
        "models": args.models,
        "stream": args.stream,
        "iterations": args.iterations,
        "parallel": args.parallel,
        "scenario_filter": args.scenario or "all",
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    models = args.models or MODELS
    all_count = len(scenarios) * len(models) * args.iterations
    print(f"\n工具调用压测")
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
                    "tool_call_args": r.tool_call_args,
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
