"""One-thread native Codex App Server control loop for arena iteration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.benchmark.profile import ModelProfile, write_codex_home
from aa_arena.benchmark.protocol import JsonRpcStdioClient
from aa_arena.benchmark.service import BenchmarkService
from aa_arena.core.build_sandbox import BuildSandboxError
from aa_arena.core.cpp_build import CppBuildError, CppBuildSandboxError
from aa_arena.legacy.ai9 import Ai9Error


MODEL_PROXY_PORT = 18463


TOKEN_EFFICIENCY_INSTRUCTIONS = """控制器协议：
- 第一轮先调用 workspace_manifest 和 list_opponents，再读取 resources/ 和 skills/replay-reading.md；确认文件工具可用、策略可编译、对手 ID 来自公开排行榜前，不得调用比赛工具。
- 默认围绕一个主要可证伪假设组织实验。先搜索，再定点读取；不要完整输出 SDK、目录树、回放或日志。原始分析写入 artifacts/，终端通常只保留不超过 60 行的相关摘要。
- small_match 可选择 1-8 个对手；能共享假设时优先每批 4-8 个，但 small 与 large 预算彼此独立，由你按证据决定校准时机。
- 控制器会保存最佳 official 结果为 CHAMPION，但不会自动覆盖当前实验分支；需要回到最佳版本时显式调用 restore_champion。
- 未达到 rank 1 时使用完整 large 预算继续争取更好名次。最后一次 large 仍评测当前 challenger；预算结束后控制器冻结历史最佳 CHAMPION。
- 比赛工具返回紧凑摘要，完整结果位于 raw_result_path。只在需要时定点读取弱项；在 notes/ 维护包含假设、结果、失败原因和下一步的简洁结构化 checkpoint。
- 正式运行不会因阶段边界主动 compact；只有总上下文达到配置的 200,000 token 门槛时才由 Codex 自动 compact。"""


GOAL_TEMPLATE = """你正在为 {game} 开发可解释、可维护的竞技策略。正式目标是在静态 verified 选手池中最大化最终 large_match 排名；同排名时最大化 Elo。

你可以自由阅读 resources/ 中的公开规则、排行榜、SDK、明确标记的 rank-40 reference example、回放格式、阅读指南和 replay/reading_skill.md；可以编辑 strategy/、维护 skills/ 和 notes/、分析自己通过小对局获得的 replays/，并改进回放阅读 skill。每个已接受 seat-match 只会在 replays/ 中产生一个有界 replay.json 和一个自动生成的 replay.md；原始 Saiblo 回放永远不可见。只能通过 small_match 和 large_match 提交真实比赛。严禁尝试读取未标记的对手代码、评测器内部实现、隐藏文件、凭证或互联网，也不得按对手身份、seed 或回放 ID 写硬编码作弊逻辑。

小对局额度上限 {small_budget}，每个 opponent_id 消耗 1；大对局额度上限 {large_budget}，每次对完整 verified 池评测并返回 Elo、置信区间和排名。预算状态包含在每次比赛摘要和每个新阶段提示中。工程失败会消耗已接受任务的预算，提交前先做离线构建和接口检查。

每次改动都应说明假设、证据、取舍和可证伪条件。优先积累通用策略，不做单个身份、seed 或回放 ID 的硬编码。控制器保存 CHAMPION；只有 rank 1 或 large 预算耗尽才结束，结束时冻结历史最佳 official 快照。"""

# Keep the strategic guidance in the goal itself so it survives native context
# compaction and is present on every resumed iteration.
GOAL_TEMPLATE += """

探索建议：把每次对局的紧凑 replay.json 和 replay.md 当作主要证据，主动观看并
比较高 Elo 对手的行动顺序、资源交换和数值变化。可以蒸馏/模仿对手的通用决策，
也可以从规则推导优先使用的战术、道具或经济循环，进一步揣摩可证伪的最优解；
先在小对局验证，再用大对局确认。模仿必须转化为不依赖 opponent_id、seed 或回放
编号的通用代码，并记录哪些回放证据支持或否定该假设。

回放学习的最低流程：先读 replay 格式说明，再用已有 narrate 输出核对一局的首回合、
中段和终局；对增量/事件型回放要在自己的分析中按单位或格子重建状态，不能把单条
记录误当成全局快照。不要要求资源包替你总结关键回合或归因，结论必须来自你实际看到
的 JSON、自然语言序列和规则；若 JSON 与 narrate 不一致，先停止迭代并记录证据。"""

DYNAMIC_TOOLS = [
    {
        "type": "function",
        "name": "workspace_shell",
        "description": "Run one short build, test, search, or file-inspection command inside the isolated writable workspace. Output is capped at 32 KiB; private paths and network access are rejected.",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string", "minLength": 1}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "workspace_manifest",
        "description": "No-cost bootstrap check. Returns the exact public read-only files, writable paths, private paths, replay contract, and match tool contract. Call before any match.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "list_opponents",
        "description": "No-cost public leaderboard query. Returns verified opponent IDs with rank/Elo; use it instead of guessing IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rank_min": {"type": "integer", "minimum": 1},
                "rank_max": {"type": ["integer", "null"], "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 64},
            },
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "small_match",
        "description": "Evaluate one frozen challenger against 1-8 verified opponents in complementary roles. Prefer batches of 4-8 when they test the same hypothesis. Raw evidence is saved to the workspace; the tool returns a compact summary and remaining budgets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "opponent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                    "uniqueItems": True,
                }
            },
            "required": ["opponent_ids"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "large_match",
        "description": "Evaluate the frozen current strategy against the entire static verified pool. Costs one large-match budget unit and returns Elo/rank/outcomes without replays. Every budget unit, including the last, evaluates the current challenger.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "restore_champion",
        "description": "Restore the best completed baseline/large-tested strategy into the working strategy directory without spending match budget.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def experiment_instructions(experiment: ExperimentConfig) -> str:
    """Policy guidance mirrors service enforcement, never selects opponents here."""
    policy = {
        "model": "",
        "ladder": (
            f"阶梯实验：从公开 rank {experiment.initial_rank} 开始，按 small_match 中的对手顺序执行。"
            "可以重复当前 rank；每次前进只允许 rank 数值减 1 或减 2，禁止跳过超过两名。"
            "由服务端执行阶梯约束，依据工具返回的当前状态选择下一步。"
        ),
        "random": (
            "随机实验：small_match 的实际对手由控制器随机选择；传入的公开 opponent_ids"
            "仅指定批量大小，不决定对手身份。不得重抽来挑选对手。"
        ),
        "top5": "前五实验：small_match 只能选择公开排行榜 rank 1–5 内的对手。",
        "top4": "前四实验：small_match 只能选择公开排行榜 rank 1–4 内的对手。",
        "clone": (
            f"克隆实验：本次独立 trial 固定学习公开 rank {experiment.clone_rank} 的一个对手，"
            "恰好 32 次 small_match，每次仅该固定 opponent_id，small=32、large=0。"
            "本 trial 仅产出一个独立蒸馏策略，不能读取其他 trial 的输出。"
            "学习必须转化为通用决策，不得按对手身份、seed 或对局编号硬编码。"
            "第 32 次反馈送达后仍须完成最后一次策略学习、修改和离线检查；"
            "之后冻结当前最后学习代码。没有全池基线、正式比赛或 CHAMPION，"
            "也不能恢复基线代替学习结果。最终 Elo 和全池排名留待后续独立测量。"
        ),
        "offpolicy": (
            "Off-policy 观察实验：不得调用 small_match。通过 list_pool_trajectories 浏览"
            "冻结的人类选手池对局目录，用 view_dense_trajectory 最多观看 128 场 dense 轨迹"
            "（含主表同款的格式说明、翻译与阅读指南）。你可以随时提交 large_match 做全池"
            "Elo/排名评测（最多 16 次）；你自己的 large 对局不会释放 dense 回放。"
            "先 manifest + list_opponents + list_pool_trajectories，再定点读取轨迹与规则。"
        ),
    }[experiment.opponent_policy]
    if experiment.binary_feedback:
        policy += (
            "\n二元反馈实验：小对局仅提供每个 seat 的 win 布尔值，平局和失败都为 false。"
            "只基于该反馈、公开规则和离线检查形成可证伪假设；"
            "不请求或推断被隐藏的对局细节。"
        )
    if experiment.fixed_small_batch is not None:
        n = experiment.fixed_small_batch
        policy += (
            f"\n固定 small 批次消融：每次 small_match 必须且只能提交恰好 {n} 个 opponent_id；"
            f"提交 {n - 1} 或 {n + 1} 个都会被服务端拒绝且不消耗有效进度。"
        )
    return policy


def _small_batch_schema(experiment: ExperimentConfig) -> tuple[int, int]:
    lo, hi = experiment.small_batch_bounds()
    return lo, hi


def experiment_tools(experiment: ExperimentConfig) -> list[dict[str, Any]]:
    """Return independent tool schemas without mutating the default contract."""
    tools = deepcopy(DYNAMIC_TOOLS)
    batch_lo, batch_hi = _small_batch_schema(experiment)
    if experiment.is_clone:
        tools = [tool for tool in tools if tool["name"] not in {"large_match", "restore_champion"}]
    if experiment.is_offpolicy:
        tools = [
            tool
            for tool in tools
            if tool["name"] not in {"small_match"}
        ]
        tools.extend(
            [
                {
                    "type": "function",
                    "name": "list_pool_trajectories",
                    "description": "No-cost browse of frozen human-vs-human dense trajectory catalog with ranks and opponent IDs.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "rank_min": {"type": "integer", "minimum": 1},
                            "rank_max": {"type": ["integer", "null"], "minimum": 1},
                            "opponent_id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "view_dense_trajectory",
                    "description": "Release one cataloged human-pool dense trajectory into replays/pool_observations/ with narration. Costs one view budget unit.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"trajectory_id": {"type": "string", "minLength": 1}},
                        "required": ["trajectory_id"],
                        "additionalProperties": False,
                    },
                },
            ]
        )
    for tool in tools:
        if tool["name"] == "workspace_manifest" and experiment.binary_feedback:
            tool["description"] = (
                "No-cost bootstrap check. Returns public read-only files, writable paths, "
                "private paths and the permitted match tool contract. Call before any match."
            )
        if tool["name"] != "small_match":
            continue
        if experiment.is_clone:
            tool["inputSchema"]["properties"]["opponent_ids"]["maxItems"] = 1
            tool["description"] = (
                "Evaluate the current strategy against exactly one policy-permitted opponent. "
                "Costs one small budget unit. Bounded replay.json and replay.md evidence is "
                "saved under replays/; raw_result_path points to the full result. "
                + experiment_instructions(experiment)
            )
        elif experiment.opponent_policy == "ladder":
            tool["inputSchema"]["properties"]["opponent_ids"]["uniqueItems"] = False
            tool["inputSchema"]["properties"]["opponent_ids"]["minItems"] = batch_lo
            tool["inputSchema"]["properties"]["opponent_ids"]["maxItems"] = batch_hi
            tool["description"] = (
                f"Evaluate {batch_lo}-{batch_hi} opponents in supplied order. Repeats are allowed. "
                f"Start at rank {experiment.initial_rank}; each next rank may repeat or decrease by 1 or 2 only. "
                "Costs one small unit per entry, including repeats. The service enforces progression."
            )
        elif experiment.opponent_policy == "random":
            tool["description"] = (
                "Evaluate the current strategy against controller-selected random opponents. "
                "The public opponent_ids supplied specify batch size only; the controller "
                "replaces them with its sampled IDs. Costs one small unit per sampled opponent."
            )
            tool["inputSchema"]["properties"]["opponent_ids"]["minItems"] = batch_lo
            tool["inputSchema"]["properties"]["opponent_ids"]["maxItems"] = batch_hi
            tool["inputSchema"]["properties"]["opponent_ids"]["description"] = (
                f"Supply {batch_lo}-{batch_hi} public IDs to specify the number of controller-sampled opponents only."
            )
        elif experiment.opponent_policy == "top5":
            tool["description"] = (
                "Evaluate the current strategy against 1-5 distinct verified opponents from "
                "public ranks 1-5 only. Costs one small budget unit per opponent."
            )
            tool["inputSchema"]["properties"]["opponent_ids"]["minItems"] = batch_lo
            tool["inputSchema"]["properties"]["opponent_ids"]["maxItems"] = min(5, batch_hi)
        elif experiment.opponent_policy == "top4":
            tool["description"] = (
                "Evaluate the current strategy against verified opponents from "
                "public ranks 1-4 only. Costs one small budget unit per opponent."
            )
            tool["inputSchema"]["properties"]["opponent_ids"]["minItems"] = batch_lo
            tool["inputSchema"]["properties"]["opponent_ids"]["maxItems"] = min(4, batch_hi)
        elif experiment.opponent_policy == "model":
            props = tool["inputSchema"]["properties"]["opponent_ids"]
            props["minItems"] = batch_lo
            props["maxItems"] = batch_hi
        if experiment.binary_feedback:
            description = tool["description"]
            if experiment.is_clone:
                description = "Evaluate the current strategy against exactly one fixed clone opponent. Costs one small budget unit."
            else:
                description = description.replace(
                    "Raw evidence is saved to the workspace; the tool returns a compact summary and remaining budgets.", ""
                )
            tool["description"] = description + (
                " "
                "Returns only seat win booleans (draw/loss = false), opponent identity "
                "and controller bookkeeping including remaining budget. "
                + experiment_instructions(experiment)
            )
    return tools


def _binary_guidance(goal: str, instructions: str) -> tuple[str, str]:
    """Change evidence access only; retain default objectives and strategy guidance."""
    goal = goal.replace("、回放格式、阅读指南和 replay/reading_skill.md", "")
    goal = goal.replace("、分析自己通过小对局获得的 replays/，并改进回放阅读 skill", "")
    goal = goal.replace(
        "每个已接受 seat-match 只会在 replays/ 中产生一个有界 replay.json 和一个自动生成的 replay.md；"
        "原始 Saiblo 回放永远不可见。",
        "每个已接受 seat-match 仅提供 win 布尔反馈；平局和失败都为 false。",
    )
    before, exploration = goal.split("探索建议：", 1)
    # Preserve the rule-derived tactics, falsifiability, small-to-large validation
    # and generalization guidance verbatim, changing only the source of evidence.
    exploration = exploration.split("也可以从规则推导", 1)[1]
    exploration = exploration.split("\n\n回放学习的最低流程：", 1)[0]
    exploration = exploration.replace("哪些回放证据", "哪些二元反馈")
    goal = before + "探索建议：从规则推导" + exploration
    instructions = instructions.replace("resources/ 和 skills/replay-reading.md", "resources/")
    instructions = instructions.replace("不要完整输出 SDK、目录树、回放或日志", "不要完整输出 SDK、目录树或日志")
    instructions = instructions.replace(
        "比赛工具返回紧凑摘要，完整结果位于 raw_result_path。只在需要时定点读取弱项；",
        "小对局仅返回 win 布尔反馈和预算信息，大对局仍返回正式结果；",
    )
    return goal, instructions


def _thread_id(result: object) -> str:
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise ValueError("Codex response contains no thread")
    value = result["thread"].get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("Codex response contains no thread id")
    return value


def _normalized_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(item) for key, item in value.items() if isinstance(item, (int, float))}


def _decode_tool_arguments(value: object) -> object:
    """App-server versions differ: function arguments may be an object or JSON text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _candidate_preflight_failure(error: Exception) -> bool:
    """Recognize candidate failures, including wrappers, without repairing infra."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        if isinstance(item, (OSError, BuildSandboxError, CppBuildSandboxError)):
            return False
        if any(marker in str(item).lower() for marker in (
            "build_sandbox_error:", "runtime_overlay_error:",
            "cmake executable is unavailable", "native compiler is unavailable",
            "published build has no executable", "published build lost its executable",
            "no space left on device", "disk quota exceeded", "input/output error",
            "cannot allocate memory", "command not found", "cannot execute",
        )):
            return False
    return any(
        isinstance(item, (CppBuildError, SyntaxError, UnicodeError))
        or (isinstance(item, ValueError) and str(item).startswith((
            "strategy directory is missing", "strategy has no main.py",
            "candidate has no runnable Python or C++ entry:",
            "candidate Python package contains no sources:", "candidate Python preflight failed:",
        )))
        or (isinstance(item, Ai9Error) and (
            str(item).startswith("build failed (")
            or "player library does not export required callback" in str(item)
        ))
        for item in chain
    )


class CredentialProxy:
    def __init__(
        self, profile: ModelProfile, api_key: str, log_path: Path, socket_path: Path
    ) -> None:
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "aa_arena.benchmark.credential_proxy",
                "--upstream",
                profile.base_url,
                "--socket",
                str(socket_path),
                "--expected-model",
                profile.model,
                "--min-request-interval",
                os.environ.get("AA_ARENA_PROXY_MIN_REQUEST_INTERVAL", "0"),
                "--rate-limit-retries",
                os.environ.get("AA_ARENA_PROXY_RATE_LIMIT_RETRIES", "0"),
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            encoding="utf-8",
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join(sys.path),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        if not self.process.stdin or not self.process.stdout:
            raise RuntimeError("failed to start credential proxy")
        self.process.stdin.write(api_key + "\n")
        self.process.stdin.flush()
        self.process.stdin.close()
        endpoint = self.process.stdout.readline().strip()
        if not endpoint.startswith("unix:"):
            raise RuntimeError(f"credential proxy failed to start; see {log_path}")
        self.socket_path = Path(endpoint.removeprefix("unix:")).resolve()
        self.base_url = f"http://127.0.0.1:{MODEL_PROXY_PORT}"

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()
        self.socket_path.unlink(missing_ok=True)


class ToolInfrastructureError(RuntimeError):
    """Stop a broken controller instead of spending tokens on repeated failures."""


class CodexArenaRuntime:
    @property
    def experiment(self) -> ExperimentConfig:
        # Older embedding callers have no experiment attribute. Production
        # BenchmarkService loads and seals controller/experiment.json itself.
        return getattr(getattr(self, "service", None), "experiment", ExperimentConfig())

    def _experiment_context(self, prompt: str) -> str:
        if self.experiment == ExperimentConfig():
            return prompt
        marker = "\n\n不可变实验配置："
        if marker in prompt:
            return prompt
        # Opponent sampling must remain unpredictable to the model. Keep the
        # complete configuration sealed in the controller; publish only policy.
        public_contract = {key: getattr(self.experiment, key) for key in (
            "opponent_policy", "feedback", "initial_rank", "clone_rank",
        )}
        return (prompt + marker + json.dumps(public_contract, ensure_ascii=False,
                                             sort_keys=True)
                + "\n" + experiment_instructions(self.experiment))

    def _objective(self) -> str:
        budgets = self.service.ledger.budgets()
        experiment = self.experiment
        if experiment.is_clone:
            objective = (
                f"你正在为 {self.service.game} 开发可解释、可维护的通用竞技策略。"
                "目标是从固定对手反馈独立蒸馏一个可泛化的策略。"
                + "\n只阅读 resources/ 中公开规则、排行榜、SDK 和明确标记的 rank-40 reference example；"
                "可以编辑 strategy/、skills/、notes/ 和 artifacts/。只能通过提供的比赛工具提交真实比赛。"
                "严禁读取未标记的对手代码、评测器内部实现、隐藏文件或凭证，禁止互联网访问。"
                "不得按对手身份、seed 或对局编号硬编码作弊；每次改动记录假设、证据和可证伪条件。"
                f"\n小对局额度 {budgets.small_total}，大对局额度 {budgets.large_total}；"
                "每个已接受 opponent_id 消耗一个小对局额度，工程失败也消耗预算。"
                "先做离线构建和接口检查；预算以工具返回 remaining 为准。"
                "\n第一轮先调用 workspace_manifest 和 list_opponents 确认公开对手 ID 和工具接口，"
                "读取必要规则并验证策略可编译，再提交比赛。先搜索再定点读取，输出简短摘要，"
                "在 notes/ 保存假设、结果、失败原因和下一步。"
                "不因阶段边界主动 compact，只有配置的 200,000 token 门槛触发原生 compact。"
            )
            if not experiment.binary_feedback:
                objective += "\n\n探索建议：" + GOAL_TEMPLATE.split("探索建议：", 1)[1]
                # Clone has no large calibration, even in detailed-feedback mode.
                objective = objective.replace("先在小对局验证，再用大对局确认。", "在固定对手的小对局中验证。")
        elif experiment.is_offpolicy:
            objective = (
                f"你正在为 {self.service.game} 开发可解释、可维护的竞技策略。"
                "正式目标是在静态 verified 选手池中最大化 16 次 large_match 的最终排名；同排名时最大化 Elo。"
                "\n本 run 为 off-policy 观察：你只能观看目录中的人类选手池 vs 人类选手池 dense 轨迹，"
                f"最多 {budgets.small_total} 次 view_dense_trajectory；不得调用 small_match。"
                f"随时可提交 large_match（最多 {budgets.large_total} 次）测试当前策略；"
                "这些评测只返回 Elo/排名，不提供 dense 回放。"
                "\n可阅读 resources/ 规则、排行榜、SDK、replay 格式/指南/翻译；"
                "轨迹释放到 replays/pool_observations/。可编辑 strategy/、skills/、notes/、artifacts/。"
                "严禁读取未标记对手源码、隐藏文件或互联网；不得按 trajectory_id、对手身份或 seed 硬编码。"
                "\n第一轮：workspace_manifest、list_opponents、list_pool_trajectories，再定点读轨迹与规则。"
            )
            if not experiment.binary_feedback:
                objective += "\n\n探索建议：" + GOAL_TEMPLATE.split("探索建议：", 1)[1]
                objective = objective.replace(
                    "先在小对局验证，再用大对局确认。",
                    "先在观看的 pool 轨迹中形成假设，再用 large_match 校准。",
                )
                objective = objective.replace(
                    "把每次对局的紧凑 replay.json 和 replay.md 当作主要证据",
                    "把 replays/pool_observations/ 中释放的 replay.json 和 replay.md 当作主要证据",
                )
            objective += "\n\n" + TOKEN_EFFICIENCY_INSTRUCTIONS.replace(
                "small_match 可选择 1-8 个对手",
                "view_dense_trajectory 每次消耗 1 个轨迹额度",
            ).replace("不得调用比赛工具", "不得调用 view_dense_trajectory 或 large_match")
        else:
            objective = GOAL_TEMPLATE.format(
                game=self.service.game, small_budget=budgets.small_total,
                large_budget=budgets.large_total,
            )
            instructions = TOKEN_EFFICIENCY_INSTRUCTIONS
            if experiment.binary_feedback:
                objective, instructions = _binary_guidance(objective, instructions)
            if experiment.opponent_policy in {"ladder", "random", "top5"}:
                instructions = "\n".join(line for line in instructions.splitlines()
                                         if not line.startswith("- small_match"))
            objective += "\n\n" + instructions
        extension = self.service.ledger.state().get("metadata", {}).get("budget_extension")
        if extension:
            # Remove first-stage stopping rules rather than giving contradictory
            # instructions to an already rank-1 persistent goal.
            objective = objective.replace("只有 rank 1 或 large 预算耗尽才结束", "本追加阶段只有 large 预算耗尽才结束")
            objective = objective.replace("仅 rank 1 或 large 额度耗尽结束", "本追加阶段仅 large 额度耗尽结束")
            objective = objective.replace("未达到 rank 1 时使用完整 large 预算", "本追加阶段即使已有 rank 1 也使用完整 large 预算")
            objective += (
                "\n这是唯一一次追加预算阶段，继续同一策略、会话与 CHAMPION。"
                f"累计额度上限 {budgets.small_total}/{budgets.large_total}；历史真实消耗不归零，"
                "首阶段未使用的小、大对局额度均已过期，以 remaining 为准。"
                f"本阶段只追加 {extension['small_added']} 小对局和 {extension['large_added']} 大对局；"
                "即使首阶段 CHAMPION 或本阶段达到 rank 1，也必须继续完成所有追加大对局。"
                "比较两阶段成绩和 token 效率。首阶段成绩："
                + json.dumps(extension["first_stage_champion"], ensure_ascii=False)
            )
        return self._experiment_context(objective)

    def __init__(
        self,
        service: BenchmarkService,
        profile: ModelProfile,
        *,
        api_key: str,
        codex_binary: str = "codex",
        turn_timeout_s: float = 24 * 3600,
    ) -> None:
        self.service = service
        self.profile = profile
        self.api_key = api_key
        resolved = shutil.which(codex_binary)
        if not resolved:
            raise FileNotFoundError(f"Codex binary not found: {codex_binary}")
        self.codex_binary = Path(resolved).resolve()
        self.turn_timeout_s = turn_timeout_s
        self.codex_home = service.controller / "codex-home"
        self.proxy: CredentialProxy | None = None
        self.client: JsonRpcStdioClient | None = None
        self.thread_id: str | None = None
        ledger_state = service.ledger.state()
        self.token_usage = _normalized_usage(ledger_state.get("token_usage"))
        self.active_seconds = float(ledger_state["active_seconds"])
        self._active_started: float | None = None
        self.runtime_root = Path(
            tempfile.mkdtemp(prefix=f"aa-arena-codex-{service.run_id[:8]}-")
        ).resolve()
        self.runtime_root.chmod(0o700)
        self.netns_proxy_path = self.runtime_root / "netns_proxy.py"
        self.mount_gate: Path | None = None
        version = subprocess.run(
            (str(self.codex_binary), "--version"),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout.strip()
        self.service.ledger.update_runtime(
            metadata={
                **self.service.ledger.state().get("metadata", {}),
                "model": profile.model,
                "model_profile": profile.name,
                "provider": profile.model_provider,
                "wire_api": profile.wire_api,
                "reasoning_effort": profile.reasoning_effort,
                "codex_version": version,
            }
        )

    def _diagnostic(self, message: str) -> None:
        path = self.service.controller / "logs" / "runtime.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.time():.3f} {message}\n")

    def _prepare_filesystem(self) -> None:
        self.service.initialize_workspace()
        (self.codex_home / "home").mkdir(parents=True, exist_ok=True)
        for path in (self.service.workspace, self.codex_home):
            path.mkdir(parents=True, exist_ok=True)
            for child in (path, *path.rglob("*")):
                if self.service.workspace / "resources" in (child, *child.parents):
                    continue
                try:
                    if child.is_symlink():
                        continue
                    os.chown(child, os.geteuid(), os.getegid(), follow_symlinks=False)
                    child.chmod(0o777 if child.is_dir() else 0o666)
                except FileNotFoundError:
                    pass
        self.service.controller.chmod(0o700)
        shutil.copy2(Path(__file__).with_name("netns_proxy.py"), self.netns_proxy_path)
        self.netns_proxy_path.chmod(0o444)

    def _bwrap_command(self) -> tuple[str, ...]:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise FileNotFoundError("bubblewrap is required for the Codex arena runtime")
        unshare = shutil.which("unshare")
        if not unshare:
            raise FileNotFoundError("unshare is required for the Codex arena runtime")
        if self.mount_gate is not None:
            shutil.rmtree(self.mount_gate, ignore_errors=True)
        self.mount_gate = Path(tempfile.mkdtemp(prefix="mount-", dir=self.runtime_root)).resolve()
        mirror_root = self.mount_gate / "mirrors"
        mirror_root.mkdir()
        sources = {
            "codex-bin": self.codex_binary.parent,
            "workspace": self.service.workspace,
            "codex-home": self.codex_home,
        }
        mirrors: dict[str, Path] = {}
        plan = []
        for label, source in sources.items():
            target = mirror_root / label
            target.mkdir()
            mirrors[label] = target
            plan.append({"source": str(source.resolve()), "target": str(target.resolve())})
        plan_path = self.mount_gate / "mount-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        plan_path.chmod(0o600)
        command = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--unshare-net",
            "--uid",
            "65534",
            "--gid",
            "65534",
        ]
        for source in ("/usr", "/etc"):
            if Path(source).exists():
                command += ["--ro-bind", source, source]
        for destination, target in (
            ("usr/bin", "/bin"),
            ("usr/lib", "/lib"),
            ("usr/lib64", "/lib64"),
        ):
            if not Path(target).exists() or Path(target).is_symlink():
                command += ["--symlink", destination, target]
            else:
                command += ["--ro-bind", target, target]
        command += [
            "--dir",
            "/opt",
            "--ro-bind",
            str(mirrors["codex-bin"] / self.codex_binary.name),
            "/opt/codex",
            "--ro-bind",
            str(self.netns_proxy_path),
            "/opt/netns_proxy.py",
            "--dir",
            "/run/aa-arena",
            "--ro-bind",
            str(self.proxy.socket_path if self.proxy is not None else ""),
            "/run/aa-arena/credential-proxy.sock",
            "--bind",
            str(mirrors["workspace"]),
            "/workspace",
            "--bind",
            str(mirrors["codex-home"]),
            "/codex-home",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--chdir",
            "/workspace",
            "--setenv",
            "HOME",
            "/codex-home/home",
            "--setenv",
            "CODEX_HOME",
            "/codex-home",
            "--setenv",
            "OPENAI_API_KEY",
            "credential-held-by-host-proxy",
            "--",
            "/usr/bin/python3",
            "/opt/netns_proxy.py",
            "--socket",
            "/run/aa-arena/credential-proxy.sock",
            "--port",
            str(MODEL_PROXY_PORT),
            "--",
            "/opt/codex",
            "app-server",
            "--stdio",
        ]
        private_mount = Path(__file__).resolve().parents[1] / "sandbox" / "private_mount.py"
        return (
            unshare,
            *(["--user", "--map-root-user"] if os.geteuid() != 0 else []),
            "--mount",
            "--propagation",
            "private",
            sys.executable,
            str(private_mount),
            str(plan_path),
            "--",
            *command,
        )

    def start(self) -> None:
        self._prepare_filesystem()
        self.proxy = CredentialProxy(
            self.profile,
            self.api_key,
            self.service.controller / "logs" / "credential-proxy.log",
            self.runtime_root / "credential-proxy.sock",
        )
        write_codex_home(
            self.codex_home,
            self.profile,
            provider_base_url=self.proxy.base_url,
        )
        for path in (self.codex_home, *self.codex_home.rglob("*")):
            if not path.is_symlink():
                path.chmod(0o777 if path.is_dir() else 0o666)
        self.client = JsonRpcStdioClient(
            self._bwrap_command(),
            cwd=Path("/"),
            environment={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stderr_path=self.service.controller / "logs" / "app-server.stderr.log",
            secrets=(self.api_key,),
        )
        self.client.request(
            "initialize",
            {
                "clientInfo": {"name": "codex", "title": "AA-Arena", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.client.notify("initialized")

    def _start_thread(self, objective: str, *, tools: list[dict[str, object]], doctor: bool = False) -> str:
        assert self.client is not None
        result = self.client.request(
            "thread/start",
            {
                "model": self.profile.model,
                "modelProvider": self.profile.model_provider,
                "cwd": "/workspace",
                "baseInstructions": "Work only inside the isolated AA-Arena workspace.",
                "developerInstructions": (
                    ("This is an infrastructure diagnostic session. Public files under resources/ and artifacts/ may be read, and ordinary local shell commands may be executed with either the built-in shell or workspace_shell. For tests that intentionally attempt network access or private-path access, the sole authorized isolation test is "
                     "python3 artifacts/doctor-network-probe.py: run it unmodified when asked, to verify "
                     "that network access, control paths and credential variables are blocked. "
                     "Do not attempt any other network access or hidden-source discovery. "
                     if doctor else "Never attempt network access or hidden-source discovery. ") +
                    "Minimize token use: batch offline analysis, print only concise summaries, "
                    "and never poll long-running shell jobs frequently."
                    + ("" if doctor else self._experiment_context(""))
                ),
                "runtimeWorkspaceRoots": ["/workspace"],
                "permissions": "arena",
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
                "ephemeral": False,
                "dynamicTools": tools,
                "environments": [],
                "serviceName": "aa-arena",
            },
        )
        thread_id = _thread_id(dict(result))
        self.client.request(
            "thread/goal/set",
            {"threadId": thread_id, "objective": objective, "status": "active"},
        )
        return thread_id

    def _usage(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        if not isinstance(params, dict):
            return
        token_usage = params.get("tokenUsage")
        total = token_usage.get("total") if isinstance(token_usage, dict) else None
        if not isinstance(total, dict):
            return
        last = token_usage.get("last") if isinstance(token_usage, dict) else None
        last = last if isinstance(last, dict) else {}
        self.token_usage = {
            "input_tokens": int(total.get("inputTokens") or 0),
            "cached_input_tokens": int(total.get("cachedInputTokens") or 0),
            "output_tokens": int(total.get("outputTokens") or 0),
            "reasoning_tokens": int(total.get("reasoningOutputTokens") or 0),
            "total_tokens": int(total.get("totalTokens") or 0),
            "last_input_tokens": int(last.get("inputTokens") or 0),
            "last_cached_input_tokens": int(last.get("cachedInputTokens") or 0),
            "last_output_tokens": int(last.get("outputTokens") or 0),
            "last_reasoning_tokens": int(last.get("reasoningOutputTokens") or 0),
            "last_total_tokens": int(last.get("totalTokens") or 0),
        }
        self.service.ledger.update_runtime(token_usage=self.token_usage)

    def _native_compact(self, *, timeout_s: float = 300) -> None:
        """Compact the current persistent thread using the native App Server operation."""

        assert self.client is not None and self.thread_id is not None
        self.client.request("thread/compact/start", {"threadId": self.thread_id})
        self._diagnostic(f"runtime compact_started thread={self.thread_id}")
        deadline = time.monotonic() + timeout_s
        compact_turn_id = None
        while time.monotonic() < deadline:
            try:
                message = self.client.next_message(min(60.0, deadline - time.monotonic()))
            except TimeoutError:
                continue
            if message.get("method") == "thread/tokenUsage/updated":
                self._usage(message)
                continue
            params = message.get("params") or {}
            method = message.get("method")
            self._diagnostic(f"runtime compact_event method={method}")
            turn = params.get("turn") or {}
            item = params.get("item") or {}
            item_type = item.get("type") if isinstance(item, dict) else None
            if item_type == "contextCompaction":
                compact_turn_id = params.get("turnId")
            # A persistent goal may start a follow-up turn just before this
            # operation. Native compaction replaces that turn: its interrupted
            # completion is not the outcome of the compaction we requested.
            failed = (method in ("error", "protocol/error") and not params.get("willRetry")) or (
                method == "turn/completed" and turn.get("status") == "failed"
            ) or (
                method == "turn/completed" and compact_turn_id is not None
                and turn.get("id") == compact_turn_id and turn.get("status") == "interrupted"
            )
            if failed:
                detail = self.client._redact(str(params.get("error") or turn.get("error") or params))
                self._diagnostic("runtime compact_failed " + detail)
                raise RuntimeError("Codex native compact failed: " + detail)
            compacted = message.get("method") == "thread/compacted" or (
                message.get("method") == "item/completed" and item_type == "contextCompaction"
            )
            if compacted:
                self._diagnostic(f"runtime compact_completed thread={self.thread_id}")
                return
        raise TimeoutError("Codex native compact did not complete")

    def _pause_active(self) -> None:
        if self._active_started is not None:
            self.active_seconds += time.monotonic() - self._active_started
            self._active_started = None
            self.service.ledger.update_runtime(
                active_seconds=self.active_seconds, token_usage=self.token_usage
            )

    def _resume_active(self) -> None:
        if self._active_started is None:
            self._active_started = time.monotonic()

    def _dispatch_tool(self, message: dict[str, Any], *, final: bool = False,
                       probe: bool = False) -> dict[str, Any]:
        params = message.get("params") or {}
        name = str(params.get("tool"))
        arguments = _decode_tool_arguments(params.get("arguments"))
        # Doctor must exercise this exact dispatch/serialization path, but must
        # never consume match budgets when the model disobeys a probe prompt.
        if probe and name not in {"workspace_manifest", "list_opponents", "workspace_shell"}:
            raise ToolInfrastructureError(f"doctor requested unexpected tool {name}")
        try:
            if final:
                raise ValueError("official strategy is frozen; tools are disabled")
            result = self.service.model_tool_call(name, arguments)
            response = {"success": True, "contentItems": [
                {"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
            if probe and name == "workspace_shell" and result.get("exit_code") != 0:
                raise ToolInfrastructureError("doctor workspace_shell command failed")
        except Exception as exc:
            response = {"success": False, "contentItems": [
                {"type": "inputText", "text": f"{type(exc).__name__}: {exc}"}]}
            self.client.respond(message["id"], response)
            failures = getattr(self, "_tool_failures", {})
            signature = (name, type(exc).__name__, str(exc))
            failures[signature] = failures.get(signature, 0) + 1
            self._tool_failures = failures
            self._diagnostic(f"tool_failed name={name} type={type(exc).__name__} count={failures[signature]}")
            # Model-side validation (e.g. wrong small batch size) must not kill the run.
            model_retry = isinstance(exc, ValueError) and name in {
                "small_match",
                "large_match",
                "view_dense_trajectory",
            }
            if not model_retry and (
                probe or failures[signature] >= 3 or sum(failures.values()) >= 12
            ):
                raise ToolInfrastructureError(
                    f"tool infrastructure stalled: {name}: {type(exc).__name__}: {exc}"
                ) from exc
            return response
        self.client.respond(message["id"], response)
        self._tool_failures = {key: value for key, value in
                               getattr(self, "_tool_failures", {}).items() if key[0] != name}
        if name in {"small_match", "large_match", "view_dense_trajectory"}:
            self.service.ledger.mark_delivered(result["match_id"])
        self._diagnostic(f"tool_complete name={name}")
        return response

    def _run_turn(self, prompt: str, *, final: bool = False) -> dict[str, Any]:
        # A provider output ceiling is not a transport retry: repeating the
        # identical request discards progress and can spend the ceiling again.
        # Start a bounded new turn in the existing thread; accepted tools and
        # their original ledger charges remain authoritative.
        for recovery in range(3):
            try:
                return self._run_turn_once(prompt, final=final)
            except RuntimeError as error:
                context_exhausted = any(marker in str(error).lower() for marker in (
                    "context_length_exceeded", "maximum context length",
                    "context window exceeded", "input exceeds the context",
                ))
                recoverable = (
                    "Incomplete response returned, reason: max_output_tokens",
                    "stream disconnected before completion: Upstream request failed",
                )
                if not context_exhausted and not any(marker in str(error) for marker in recoverable):
                    raise
                if recovery == 2:
                    raise
                if context_exhausted:
                    self._diagnostic(f"context_limit_recovery attempt={recovery + 1} thread={self.thread_id}")
                    # Keep native session compaction, ledger charges, and tools.
                    # Failed compaction must remain a visible failed run.
                    self._native_compact(timeout_s=300)
                self._diagnostic(f"output_limit_recovery attempt={recovery + 1} thread={self.thread_id}")
                prompt = (
                    "上次回复因输出上限或上游请求中断而未完成。保留当前会话和已经完成的工具结果，"
                    "不要重放已提交的对局。停止重复展开同一分析，将工作拆成短步骤；"
                    "依据已有证据执行一个具体动作，然后再决定下一步。"
                )
                if final:
                    prompt += "策略与评测仍然冻结，只输出简短复盘。"
        raise AssertionError("unreachable")

    def _run_turn_once(self, prompt: str, *, final: bool = False) -> dict[str, Any]:
        assert self.client is not None and self.thread_id is not None
        self.client.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": self._experiment_context(prompt)}],
                "cwd": "/workspace",
                "effort": self.profile.reasoning_effort,
            },
        )
        active_turn_id: str | None = None
        self._resume_active()
        deadline = time.monotonic() + self.turn_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex arena turn exceeded its active wall-time limit")
            try:
                message = self.client.next_message(min(remaining, 60.0))
            except TimeoutError:
                # Queue polling is bounded separately from the whole turn.
                # A slow model call must still receive the configured deadline.
                continue
            method = message.get("method")
            if method == "turn/started":
                started = (message.get("params") or {}).get("turn") or {}
                if isinstance(started.get("id"), str):
                    active_turn_id = started["id"]
                    self.service.ledger.update_runtime(current_turn_id=active_turn_id)
                continue
            if method == "thread/tokenUsage/updated":
                self._usage(message)
                continue
            if method == "item/tool/call" and "id" in message:
                self._pause_active()
                tool_started = time.monotonic()
                try:
                    self._dispatch_tool(message, final=final)
                finally:
                    # Match/evaluation and other tool time is already excluded
                    # from active_seconds; apply the same rule to this deadline.
                    deadline += time.monotonic() - tool_started
                    self._resume_active()
                continue
            if method == "turn/completed":
                params = message.get("params") or {}
                observed = params.get("turn") or {}
                if active_turn_id is not None and observed.get("id") != active_turn_id:
                    continue
                self._pause_active()
                if observed.get("status") != "completed":
                    raise RuntimeError(f"Codex turn failed: {observed.get('error')}")
                return observed

    def compatibility_gate(self) -> dict[str, bool]:
        if self.client is None:
            self.start()
        assert self.client is not None
        probe_called = False
        thread_id = self._start_thread(
            "Before any other action, run exactly this shell command using the built-in shell or workspace_shell (both are authorized): `python3 -c \"import json; print(json.load(open('resources/manifest.json'))['game'])\"`. After it completes, reply SHELL_OK.",
            tools=DYNAMIC_TOOLS,
            doctor=True,
        )
        self._diagnostic(f"doctor thread_started thread={thread_id}")
        turn = self.client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Run the exact public-file shell command in your objective now, using the built-in shell or workspace_shell. Do not call workspace_manifest or list_opponents until that command has completed."}],
                "cwd": "/workspace",
                "effort": "low",
            },
        )
        turn_id = str((turn.get("turn") or {}).get("id"))
        active_turn_id: str | None = None
        shell_called = False
        self._diagnostic(f"doctor turn_started turn={turn_id}")
        token_seen = False
        while True:
            message = self.client.next_message(300)
            method = str(message.get("method"))
            observed_turn = ((message.get("params") or {}).get("turn") or {}).get("id")
            self._diagnostic(f"doctor message method={method} turn={observed_turn}")
            if message.get("method") == "turn/started" and isinstance(observed_turn, str):
                active_turn_id = observed_turn
            if message.get("method") == "thread/tokenUsage/updated":
                token_seen = True
            if message.get("method") == "item/completed":
                item = (message.get("params") or {}).get("item") or {}
                if isinstance(item, dict) and item.get("type") == "commandExecution":
                    output = item.get("aggregatedOutput")
                    if isinstance(output, str) and output.strip() and "command not found" not in output:
                        shell_called = True
            if message.get("method") == "item/tool/call" and "id" in message:
                self._dispatch_tool(message, probe=True)
                tool_name = str((message.get("params") or {}).get("tool"))
                if tool_name == "workspace_shell":
                    shell_called = True
                probe_called = True
            if message.get("method") == "turn/completed":
                observed = (message.get("params") or {}).get("turn") or {}
                if active_turn_id is None or observed.get("id") == active_turn_id:
                    if observed.get("status") != "completed":
                        raise RuntimeError(f"Model probe turn failed: {observed.get('error')}")
                    break
        self._diagnostic("doctor turn_completed")
        if not shell_called:
            raise RuntimeError("first-turn workspace shell was not available or not callable")
        # Probe the shell separately so optional bootstrap tools cannot satisfy
        # the request without exercising shell execution.
        self.client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Now call workspace_manifest once, list_opponents once, and workspace_shell with command `pwd` once. Then stop."}],
                "cwd": "/workspace",
                "effort": "low",
            },
        )
        bootstrap_tools: set[str] = set()
        bootstrap_turn_id: str | None = None
        while True:
            message = self.client.next_message(300)
            params = message.get("params") or {}
            observed = params.get("turn") or {}
            if message.get("method") == "turn/started" and isinstance(observed.get("id"), str):
                bootstrap_turn_id = observed["id"]
            if message.get("method") == "item/tool/call" and "id" in message:
                self._dispatch_tool(message, probe=True)
                bootstrap_tools.add(str(params.get("tool")))
                probe_called = True
            if message.get("method") == "turn/completed":
                if bootstrap_turn_id is None or observed.get("id") == bootstrap_turn_id:
                    if observed.get("status") != "completed":
                        raise RuntimeError(f"Codex bootstrap tool probe turn failed: {observed.get('error')}")
                    break
        if bootstrap_tools != {"workspace_manifest", "list_opponents", "workspace_shell"}:
            raise ToolInfrastructureError(f"doctor missed production tools: {bootstrap_tools}")
        network_probe = (
            "import os, socket\n"
            "from pathlib import Path\n"
            "def blocked(a):\n"
            " try:\n"
            "  s=socket.create_connection(a,0.3); s.close(); return False\n"
            " except OSError: return True\n"
            f"print('AA_ARENA_NET_BLOCKED='+str(blocked(('127.0.0.1',{MODEL_PROXY_PORT})) "
            "and blocked(('1.1.1.1',53))))\n"
            "print('AA_ARENA_CONTROL_HIDDEN='+str(not Path('/codex-home/config.toml').exists() "
            "and not Path('/run/aa-arena/credential-proxy.sock').exists()))\n"
            "print('AA_ARENA_SECRET_HIDDEN='+str('OPENAI_API_KEY' not in os.environ "
            f"and {self.profile.api_key_env!r} not in os.environ))\n"
        )
        probe_path = self.service.workspace / "artifacts" / "doctor-network-probe.py"
        probe_path.write_text(network_probe, encoding="utf-8")
        self.client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": (
                            "Run this exact command once using the shell, then stop: "
                            "python3 artifacts/doctor-network-probe.py"
                        ),
                    }
                ],
                "cwd": "/workspace",
                "effort": "low",
            },
        )
        network_blocked = False
        control_hidden = False
        secret_hidden = False
        network_probe_observed = False
        network_turn_id: str | None = None
        while True:
            message = self.client.next_message(300)
            params = message.get("params") or {}
            observed = params.get("turn") or {}
            item = params.get("item") or {}
            if message.get("method") == "turn/started" and isinstance(observed.get("id"), str):
                network_turn_id = observed["id"]
            if message.get("method") == "item/completed" and isinstance(item, dict):
                output = item.get("aggregatedOutput")
                if item.get("type") == "commandExecution" and isinstance(output, str):
                    network_probe_observed = "AA_ARENA_NET_BLOCKED=" in output
                    network_blocked = "AA_ARENA_NET_BLOCKED=True" in output
                    control_hidden = "AA_ARENA_CONTROL_HIDDEN=True" in output
                    secret_hidden = "AA_ARENA_SECRET_HIDDEN=True" in output
            if message.get("method") == "item/tool/call" and "id" in message:
                tool_name = str(params.get("tool"))
                arguments = _decode_tool_arguments(params.get("arguments"))
                try:
                    if tool_name != "workspace_shell":
                        raise ValueError("network probe requires workspace_shell")
                    result = self.service.model_tool_call(tool_name, arguments)
                    output = str(result.get("output", ""))
                    network_probe_observed = "AA_ARENA_NET_BLOCKED=" in output
                    network_blocked = "AA_ARENA_NET_BLOCKED=True" in output
                    control_hidden = "AA_ARENA_CONTROL_HIDDEN=True" in output
                    secret_hidden = "AA_ARENA_SECRET_HIDDEN=True" in output
                    response = {
                        "success": True,
                        "contentItems": [{"type": "inputText", "text": json.dumps(result)}],
                    }
                except Exception as exc:
                    response = {
                        "success": False,
                        "contentItems": [{"type": "inputText", "text": f"{type(exc).__name__}: {exc}"}],
                    }
                self.client.respond(message["id"], response)
            if message.get("method") == "turn/completed":
                if network_turn_id is None or observed.get("id") == network_turn_id:
                    if observed.get("status") != "completed":
                        raise RuntimeError(f"Codex shell-network isolation probe turn failed: {observed.get('error')}")
                    break
        if not network_probe_observed:
            raise RuntimeError("Codex isolation probe did not produce a result; isolation is unverified")
        if not network_blocked:
            raise RuntimeError("Codex shell can reach a network endpoint")
        if not control_hidden:
            raise RuntimeError("Codex shell can read control-plane paths")
        if not secret_hidden:
            raise RuntimeError("Codex shell inherited a model credential variable")
        self._diagnostic("doctor shell_network_blocked")
        goal = self.client.request("thread/goal/get", {"threadId": thread_id})
        self._diagnostic("doctor goal_get")
        self.thread_id = thread_id
        self._native_compact()
        compacted = True
        self.close_client_only()
        self._diagnostic("doctor first_client_closed")
        self.start_client_only()
        self._diagnostic("doctor second_client_started")
        resumed = self.client.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "model": self.profile.model,
                "modelProvider": self.profile.model_provider,
                "cwd": "/workspace",
                "approvalPolicy": "never",
                "excludeTurns": True,
                "permissions": "arena",
            },
        )
        if _thread_id(dict(resumed)) != thread_id:
            raise RuntimeError("Model resume returned a different thread")
        self._diagnostic("doctor resume_complete")
        return {
            "thread_start": True,
            "goal": isinstance(goal.get("goal"), dict),
            "dynamic_tool": probe_called,
            "first_turn_shell": shell_called,
            "token_usage": token_seen,
            "shell_network_blocked": network_blocked,
            "control_plane_hidden": control_hidden,
            "credential_environment_hidden": secret_hidden,
            "compact": compacted,
            "resume": True,
        }

    def start_client_only(self) -> None:
        if self.proxy is None:
            raise RuntimeError("credential proxy is not running")
        self.client = JsonRpcStdioClient(
            self._bwrap_command(),
            cwd=Path("/"),
            environment={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stderr_path=self.service.controller / "logs" / "app-server.stderr.log",
            secrets=(self.api_key,),
        )
        self.client.request(
            "initialize",
            {
                "clientInfo": {"name": "codex", "title": "AA-Arena", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.client.notify("initialized")

    def close_client_only(self) -> None:
        if self.client:
            self.client.close()
            self.client = None
        if self.mount_gate is not None:
            shutil.rmtree(self.mount_gate, ignore_errors=True)
            self.mount_gate = None

    def run(self) -> None:
        if self.client is None:
            self.start()
        budgets = self.service.ledger.budgets()
        objective = self._objective()
        existing_thread = self.service.ledger.state().get("thread_id")
        if existing_thread:
            assert self.client is not None
            resumed = self.client.request(
                "thread/resume",
                {
                    "threadId": existing_thread,
                    "model": self.profile.model,
                    "modelProvider": self.profile.model_provider,
                    "cwd": "/workspace",
                    "approvalPolicy": "never",
                    "excludeTurns": True,
                    "permissions": "arena",
                },
            )
            self.thread_id = _thread_id(dict(resumed))
            self.client.request(
                "thread/goal/set",
                {"threadId": self.thread_id, "objective": objective, "status": "active"},
            )
            self.service.ledger.update_runtime(
                thread_id=self.thread_id,
                goal={"objective": objective, "status": "active"},
            )
            recovery_path = self.service.controller / "native-recovery.json"
            if recovery_path.exists():
                recovery = json.loads(recovery_path.read_text())
                if recovery.get("compact_before_resume"):
                    if recovery.get("thread_id") != self.thread_id:
                        raise RuntimeError("Native recovery thread identity mismatch")
                    self._native_compact(timeout_s=300)
                    recovery.update(compact_before_resume=False, compacted_at=time.time())
                    recovery_path.write_text(json.dumps(recovery, indent=2) + "\n")
            missed = self.service.ledger.undelivered_results()
            prompt = "控制器已恢复。继续推进同一个持久目标，不要重新开始。"
            prompt += (
                " 若前次回复因输出上限中断，改用短步骤，先执行一个具体动作，"
                "不要重复展开同一分析或重放已提交的对局。"
            )
            rb = self.service.ledger.budgets()
            if rb.small_remaining == 0 and rb.large_remaining > 0:
                prompt += (
                    f" 小对局已用尽，禁止 small_match；立即调用 large_match 完成剩余 "
                    f"{rb.large_remaining}/{rb.large_total} 场大对局。"
                )
            if missed:
                recovered = [
                    {
                        "match_id": row["match_id"],
                        "kind": row["kind"],
                        "result": self.service.agent_result(row["result"]),
                    }
                    for row in missed
                ]
                prompt += " 上次中断前已完成但尚未送达的比赛结果如下：" + json.dumps(
                    recovered, ensure_ascii=False
                )
        else:
            self.thread_id = self._start_thread(objective, tools=experiment_tools(self.experiment))
            self.service.ledger.update_runtime(
                thread_id=self.thread_id,
                goal={"objective": objective, "status": "active"},
            )
            prompt = (
                "开始执行持久目标。只定点读取规则、排行榜和策略入口，写出第一个可证伪假设后开始一批验证。"
                f"当前剩余小对局 {budgets.small_remaining}/{budgets.small_total}，"
                f"大对局 {budgets.large_remaining}/{budgets.large_total}；"
                "比赛工具返回时会再次给出权威剩余额度。"
            )
        idle_turns = 0
        while self.service.ledger.state()["status"] == "running":
            before = self.service.ledger.budgets()
            if self.experiment.is_clone and before.small_remaining == 0:
                self._finish_clone_learning()
                break
            if before.small_remaining == 0 and before.large_remaining == 0:
                break
            self._run_turn(self._experiment_context(prompt))
            after = self.service.ledger.budgets()
            idle_turns = idle_turns + 1 if (before.small_used, before.large_used) == (after.small_used, after.large_used) else 0
            if idle_turns >= 8 and self.service.ledger.state()["status"] == "running":
                budgets = self.service.ledger.budgets()
                if budgets.small_remaining == 0 and budgets.large_remaining == 0:
                    break
                raise ToolInfrastructureError("eight completed turns without a match submission; inspect infrastructure before resuming")
            for row in self.service.ledger.undelivered_results():
                self.service.ledger.mark_delivered(row["match_id"])
            budgets = self.service.ledger.budgets()
            prompt = (
                "继续推进同一个持久目标。"
                f"当前小对局已用 {budgets.small_used}/{budgets.small_total}、"
                f"剩余 {budgets.small_remaining}；大对局已用 "
                f"{budgets.large_used}/{budgets.large_total}、剩余 {budgets.large_remaining}。"
                "自行决定下一步，不要等待人工指令。"
            )
            if budgets.large_remaining == 0 and budgets.small_remaining > 0:
                prompt += (
                    f" 大对局额度已耗尽，仅可使用 view_dense_trajectory 读取剩余轨迹额度： "
                    f"{budgets.small_remaining} 场。"
                )
            if budgets.small_remaining == 0 and budgets.large_remaining > 0:
                prompt += (
                    f" 小对局额度已耗尽，禁止再调用 small_match；本回合必须用 large_match 消耗剩余 "
                    f"{budgets.large_remaining}/{budgets.large_total} 场大对局。"
                )
        self.service.restore_frozen_strategy()
        self._freeze_workspace()
        if self.service.ledger.begin_final_review():
            missed = self.service.ledger.undelivered_results()
            final_prompt = (
                "正式策略和比赛工具现已冻结。结合最后一次大对局结果及其中的 CHAMPION，输出简洁的最终复盘："
                "最终成绩、关键策略、最有效证据、失败尝试、可维护性和下一步建议。不要尝试修改文件。"
            )
            if self.experiment.is_clone:
                final_prompt = (
                    "最后一次反馈后的学习代码已冻结，比赛工具已关闭。输出简短最终复盘："
                    "本 trial 的固定学习目标、32 次小对局的实际观察、蒸馏出的通用决策、"
                    "失败尝试和可维护性。不要尝试修改文件。"
                    "该策略尚未进行独立全池评测，不得声称已获得 Elo 或全池排名；"
                    "公开对手 rank 只是学习目标身份，不是本策略成绩。"
                )
            if missed:
                recovered = [
                    {
                        "match_id": row["match_id"],
                        "kind": row["kind"],
                        "result": self.service.agent_result(row["result"], persist=False),
                    }
                    for row in missed
                ]
                final_prompt += " 中断恢复的最终比赛结果：" + json.dumps(
                    recovered, ensure_ascii=False
                )
            self._run_turn(self._experiment_context(final_prompt), final=True)
            for row in missed:
                self.service.ledger.mark_delivered(row["match_id"])
        self.service.ledger.mark_complete()

    def _finish_clone_learning(self) -> None:
        """Consume the final observation before asking the service to freeze code.

        Completion requires preflight; at most two durable repair attempts can
        follow the final learning turn. Resume revalidates even an older complete
        marker. No submission is replayed or newly charged.
        """
        if self.service.ledger.pending_submission() is not None:
            raise ToolInfrastructureError("clone has a pending match; recover it before adaptation")
        state = self.service.ledger.state()
        metadata = state.get("metadata", {})
        if not (metadata.get("clone_adaptation_complete") or metadata.get("clone_adaptation_finished")):
            history = self.service.ledger.submissions()
            if len(history) != 32 or any(row["status"] != "complete" for row in history):
                raise ToolInfrastructureError("clone requires all 32 completed results before adaptation")
            prompt = (
                "32 次固定对手小对局现已全部完成，比赛预算耗尽。现在完成最后一次学习："
                "读取第 32 次反馈，提炼通用决策，必要时修改 strategy/ 并进行离线构建和接口检查。"
                "更新 notes/ 中的假设和证据，然后结束本轮。不要再提交比赛；"
                "控制器将在本轮完成后保存并冻结当前最后学习代码用于后续独立测量。"
                "第 32 次反馈："
                + json.dumps(self.service.agent_result(history[-1]["result"]), ensure_ascii=False)
            )
            self._run_turn(self._experiment_context(prompt))
            metadata = dict(self.service.ledger.state()["metadata"])
            metadata["clone_adaptation_finished"] = True
            self.service.ledger.update_runtime(metadata=metadata)
        while True:
            try:
                self.service._preflight()
            except Exception as error:
                self._repair_clone_candidate(error)
                continue
            metadata = dict(self.service.ledger.state()["metadata"])
            metadata["clone_adaptation_complete"] = True
            self.service.ledger.update_runtime(metadata=metadata)
            for row in self.service.ledger.undelivered_results():
                self.service.ledger.mark_delivered(row["match_id"])
            try:
                self.service.finalize_clone()
            except Exception as error:
                # The service rechecks preflight before taking its snapshot.
                # Invalidate completion if that second check finds bad code.
                self._repair_clone_candidate(error)
                continue
            break
        if self.service.ledger.state()["status"] != "finalizing":
            raise ToolInfrastructureError("clone service did not freeze the final learned snapshot")

    def _repair_clone_candidate(self, error: Exception) -> None:
        if not _candidate_preflight_failure(error):
            raise error
        metadata = dict(self.service.ledger.state()["metadata"])
        metadata.update(clone_adaptation_complete=False, clone_adaptation_finished=True)
        attempts = int(metadata.get("clone_final_repair_attempts", 0))
        if attempts >= 2:
            self.service.ledger.update_runtime(metadata=metadata)
            raise ToolInfrastructureError(
                "clone candidate still fails preflight after two final repair turns; no snapshot frozen"
            ) from error
        metadata["clone_final_repair_attempts"] = attempts + 1
        # Reserve the repair before the model turn, so interruption/restart does
        # not grant unlimited extra learning turns after the fixed match budget.
        self.service.ledger.update_runtime(metadata=metadata)
        self._run_turn(self._experiment_context(
            f"候选策略离线预检未通过。现在执行第 {attempts + 1}/2 次最终修复："
            "只修复 strategy/ 的构建入口、编译或语法问题，保留已学习的通用策略。"
            "使用 workspace_shell 进行离线构建和检查，根据工作区内的诊断修复代码。"
            "32 次比赛预算已经耗尽，不得提交比赛。修复后结束本轮，由控制器重新预检；"
            "通过后才会冻结代码。"
        ))

    def _freeze_workspace(self) -> None:
        for path in (self.service.workspace, *self.service.workspace.rglob("*")):
            try:
                if path.is_symlink():
                    continue
                os.chown(path, os.geteuid(), os.getegid(), follow_symlinks=False)
                path.chmod(0o555 if path.is_dir() else 0o444)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        self.close_client_only()
        if self.proxy:
            self.proxy.close()
            self.proxy = None
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def __enter__(self) -> CodexArenaRuntime:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
