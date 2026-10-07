"""决策空间契约的**机器可读**部分 —— 行为信息增益（IG/KL）的唯一事实源。

`games/<game>/decision_space.yaml` 原本是给人读的契约文档（观测字段、动作表、
合法性语义）。但 `docs/metrics-schema.md` 里的 `local_policy_kl_trace` 要求两件
可被程序执行的事：

1. **动作要有稳定的规范表示**——否则"新旧策略是否做了同一件事"无法判定；
2. **支撑集大小 |A| 必须有出处**——ε 正则通道下，确定性策略的 KL 是

   ``KL = (1-ε+ε/|A|)·ln((1-ε+ε/|A|)/(ε/|A|)) + (ε/|A|)·ln((ε/|A|)/(1-ε+ε/|A|))``

   只要 |A| 没有出处，这个数就没有科研意义。

所以每个游戏的 `decision_space.yaml` 里新增一段 `information_gain:`，本模块负责
加载 + 校验它。**它只声明测量口径，不声明结论**：`support.mode` 会一路写进事件与
曲线，读者永远能看出 |A| 是精确枚举出来的还是一个声明的测量约定。

设计约束
--------

* 本模块属于框架 Core，**不含任何游戏语义**：它只读 YAML 里的声明；
* A 的 Core 是零依赖包，因此 ``yaml`` 只在函数内部延迟导入
  （安装 ``aa_arena[measure]`` 才需要 PyYAML）；
* 未声明 `information_gain:` 的游戏返回 ``None``，调用方必须**诚实记 null + 原因**，
  绝不用别的量冒充行为信息增益。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

SPEC_FILENAME = "decision_space.yaml"
SPEC_KEY = "information_gain"

#: 探针种类。``transcript_replay`` = 在冻结的线协议观测流上重放候选进程
#: （见 B 仓 ``aa_arena_hl.adapters.transcript``）。
PROBES = ("transcript_replay",)

#: 选手 → 判题器的帧格式。本仓 8 个游戏沿用同一套 Saiblo 传输层。
PLAYER_FRAMES = ("length_prefixed_i32_be",)

#: 规范动作 token 的构造方式。
ACTION_TOKENS = ("sha256_of_frame_body",)

#: 一次线协议回复在游戏语义上代表什么粒度的动作。
GRANULARITIES = (
    "atomic_operation",        # 一帧 = 一个原子操作（snakego 的单个操作码）
    "single_operation",        # 一帧 = 一条带参数的操作（miracle / lostspace）
    "joint_direction_vector",  # 一帧 = 一个联合动作向量（rollman 的三只幽灵）
    "turn_bundle",             # 一帧 = 本回合的一串操作（antwar / antwar2 / generals）
    "staged_operation",        # 一帧 = 当前阶段要求的那类操作（aquawar）
)

#: |A| 的来源口径。**这个字段会随每个 IG 数一起上报，不允许省略。**
SUPPORT_MODES = (
    # 整个线协议动作本身就是一个有限可枚举集合 ⇒ |A| 精确。
    "enumerated",
    # 线协议动作是带参数的操作/操作串，参数域随状态变化 ⇒ |A| 取"该游戏选手可发出的
    # 操作类型字母表大小"，这是一个**固定且声明过的测量约定**（不是精确合法集）。
    "opcode_alphabet",
)

#: occupancy（状态访问分布）的 state id 口径。
OCCUPANCY_IDS = ("sha256_of_decision_observation_bytes",)


class DecisionSpaceError(ValueError):
    """`decision_space.yaml` 的 `information_gain:` 段不满足契约。"""


@dataclass(frozen=True)
class SupportSpec:
    """决策支撑集 |A| 的声明。"""

    mode: str
    cardinality: int | None
    provenance: str
    cardinality_by_role: Mapping[str, int] = field(default_factory=dict)

    def size_for(self, role: str | None = None) -> int:
        """该角色下的 |A|。非对称游戏（rollman）两个座次的动作空间不同。"""

        if role is not None:
            value = self.cardinality_by_role.get(role)
            if value is not None:
                return int(value)
        if self.cardinality is None:
            raise DecisionSpaceError(
                f"support has no cardinality for role {role!r}; "
                f"declared roles: {sorted(self.cardinality_by_role)}"
            )
        return int(self.cardinality)

    def describe(self, role: str | None = None) -> dict[str, object]:
        """随 IG 一起上报的口径说明。"""

        return {
            "support_mode": self.mode,
            "support_cardinality": self.size_for(role),
            "support_provenance": self.provenance,
        }


@dataclass(frozen=True)
class InformationGainSpec:
    """一个游戏的行为信息增益测量契约。"""

    game: str
    schema: str
    probe: str
    player_frame: str
    action_granularity: str
    action_token: str
    support: SupportSpec
    occupancy_state_id: str

    def describe(self, role: str | None = None) -> dict[str, object]:
        return {
            "game": self.game,
            "schema": self.schema,
            "probe": self.probe,
            "action_granularity": self.action_granularity,
            "action_token": self.action_token,
            "occupancy_state_id": self.occupancy_state_id,
            **self.support.describe(role),
        }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DecisionSpaceError(f"{label} must be a mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _choice(value: object, label: str, allowed: tuple[str, ...]) -> str:
    text = str(value or "")
    if text not in allowed:
        raise DecisionSpaceError(f"{label} must be one of {allowed}, got {value!r}")
    return text


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionSpaceError(f"{label} must be a non-empty string")
    return value.strip()


def _cardinality(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionSpaceError(f"{label} must be an integer")
    if value < 2:
        # |A| = 1 时策略无从改变，KL 恒为 0：这样的声明只会画出一条假的水平线。
        raise DecisionSpaceError(f"{label} must be at least 2 for a KL to be defined")
    return int(value)


def _parse_support(value: object) -> SupportSpec:
    root = _mapping(value, f"{SPEC_KEY}.support")
    mode = _choice(root.get("mode"), f"{SPEC_KEY}.support.mode", SUPPORT_MODES)
    provenance = _text(root.get("provenance"), f"{SPEC_KEY}.support.provenance")
    raw_by_role = root.get("cardinality_by_role")
    by_role: dict[str, int] = {}
    if raw_by_role is not None:
        for role, item in _mapping(raw_by_role, f"{SPEC_KEY}.support.cardinality_by_role").items():
            by_role[role] = _cardinality(
                item, f"{SPEC_KEY}.support.cardinality_by_role[{role}]"
            )
    raw = root.get("cardinality")
    cardinality = (
        None if raw is None else _cardinality(raw, f"{SPEC_KEY}.support.cardinality")
    )
    if cardinality is None and not by_role:
        raise DecisionSpaceError(
            f"{SPEC_KEY}.support needs cardinality or a non-empty cardinality_by_role"
        )
    return SupportSpec(
        mode=mode,
        cardinality=cardinality,
        provenance=provenance,
        cardinality_by_role=by_role,
    )


def parse_information_gain(document: object, *, game: str) -> InformationGainSpec | None:
    """从已解析的 `decision_space.yaml` 文档里取出测量契约（缺失返回 None）。"""

    root = _mapping(document, f"{game} {SPEC_FILENAME}")
    section = root.get(SPEC_KEY)
    if section is None:
        return None
    spec = _mapping(section, SPEC_KEY)
    wire = _mapping(spec.get("wire"), f"{SPEC_KEY}.wire")
    action = _mapping(spec.get("action"), f"{SPEC_KEY}.action")
    occupancy = _mapping(spec.get("occupancy"), f"{SPEC_KEY}.occupancy")
    return InformationGainSpec(
        game=game,
        schema=_text(spec.get("schema"), f"{SPEC_KEY}.schema"),
        probe=_choice(spec.get("probe"), f"{SPEC_KEY}.probe", PROBES),
        player_frame=_choice(
            wire.get("player_frame"), f"{SPEC_KEY}.wire.player_frame", PLAYER_FRAMES
        ),
        action_granularity=_choice(
            action.get("granularity"), f"{SPEC_KEY}.action.granularity", GRANULARITIES
        ),
        action_token=_choice(action.get("token"), f"{SPEC_KEY}.action.token", ACTION_TOKENS),
        support=_parse_support(spec.get("support")),
        occupancy_state_id=_choice(
            occupancy.get("state_id"), f"{SPEC_KEY}.occupancy.state_id", OCCUPANCY_IDS
        ),
    )


def load_information_gain(game_dir: str | Path) -> InformationGainSpec | None:
    """读 `games/<game>/decision_space.yaml` 的测量契约。

    返回 ``None`` 的两种情况都必须由调用方如实上报：
    - 该游戏还没有 `decision_space.yaml`；
    - 有文件但没有 `information_gain:` 段（还没接入行为 IG）。
    """

    directory = Path(game_dir)
    path = directory / SPEC_FILENAME
    if not path.is_file():
        return None
    import yaml  # Core 是零依赖包，YAML 解析按需加载

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_information_gain(document, game=directory.name)


def information_gain_spec(game: str, games_root: str | Path) -> InformationGainSpec | None:
    """按游戏名加载测量契约。"""

    return load_information_gain(Path(games_root) / game)
