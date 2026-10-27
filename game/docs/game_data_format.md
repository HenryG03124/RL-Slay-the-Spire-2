# STS2 战斗输出数据格式

这份文档说明当前项目中，调用原生战斗接口 `reset`、`step` 后会得到什么数据，以及怎样从中读取玩家、敌人、手牌和合法动作。

当前接口返回的是嵌套 JSON，读入 Python 后是 `dict` 和 `list`，还不是神经网络直接接收的固定长度向量。这里的“原生接口”指 `divine-sts2` 对游戏程序集的封装，不是游戏官方提供的 Gym 接口。

适用版本：游戏 **v0.107.1 / 59260271**，接口源码提交 **7cb715916fc9abfb0281c0adf4939c74468e05a4**；协议版本为 **1**，`observation.schema_version` 为 **2**。范围是目前的 Ironclad 单场战斗，地图、商店、事件等其他模式不在本文展开。

## 1 快速查找

后文统一使用 `state = response["result"]`。

| 想读取的内容 | Python 路径或方法 |
|---|---|
| 当前回合 | `state["observation"]["combat"]["turn"]` |
| 当前能量 | `state["observation"]["combat"]["energy"]` |
| 玩家和敌人 | `state["observation"]["combat"]["creatures"]`，按 `side` 查找 |
| 手牌 | `state["observation"]["combat"]["piles"]` 中 `name == "Hand"` 的 `cards` |
| 原生合法动作 | `state["legal_actions"]` |
| 战斗是否结束 | `state["terminated"]` |
| 是否获胜 | `state["victory"]` |
| 四动作 mask 和对应原生动作 | `get_actions(state)`，见 [action_space.py](../../action_space.py) |

**不要把整个 `state` 或原始 `observation` 直接送给模型。** 其中有随机数状态、真实抽牌顺序和内部标识。可用于决策的字段见第 12 节。

## 2 先区分三层数据

### 通信响应

接口通过标准输入和标准输出逐行传递 JSON。一个请求对应一个响应，用字符串 `id` 配对。下面是实际运行中的出牌请求：

```json
{"id":"4","method":"step","params":{"action_id":"play:strike-0:target:1"}}
```

成功响应的结构如下。这是结构示意，不是完整 JSON：

```text
response
├── id: str                 请求编号，本例为 "4"
├── ok: bool                本例为 true
└── result: dict            reset/step 返回的 state
    ├── observation
    ├── legal_actions
    ├── terminated
    └── ...
```

错误响应则是 `id`、`ok: false`、`error`，其中 `error` 包含字符串 `code`、字符串 `message`，以及可选的 `details`。错误响应不是正常状态转移，不能当成一条训练样本。

Godot 的启动横幅和普通日志也可能出现在 stdout 中，不能把每一行 stdout 都假设成协议响应。现有探针已经处理这个问题。

### 原生状态 state

从原始响应中取：

```python
state = response["result"]
obs = state["observation"]
combat = obs["combat"]
```

上游 `NativeWorker.reset()` / `step()` 以及当前探针内部的 `request()` 已经解开外层响应，直接返回 `state`，不能再取一次 `["result"]`。

`protocol.jsonl` 是探针自己的日志格式，每行额外包了一层 `{"request": ...}` 或 `{"response": ...}`。因此读响应日志行时是 `row["response"]["result"]`。

### 探针摘要和四动作映射

探针检查报告保存在系统临时目录 `sts2-rl/probes/`，里面的 `initial`、`after` 是 `visible_summary()` 生成的摘要，不是原生 `state`。

摘要会把敌人意图放到 `creatures[*].intents`，把手牌放到顶层 `hand`，把牌堆变成 `pile_counts`，并去掉部分内部字段。原生格式的路径则分别是 `creatures[*].next_move.intents`、`combat.piles`。

`action_space.get_actions(state)` 生成的四动作 mask 也是本项目的转换结果。原生接口只给动态长度的合法动作列表，不直接输出这四个布尔值。

## 3 state 的顶层字段

| 字段 | Python 类型 | 含义 |
|---|---|---|
| `observation` | `dict` | 当前原始状态快照，详细结构见下文 |
| `legal_actions` | `list[dict]` | 当前允许执行的原生动作 |
| `terminated` | `bool` | 当前孤立战斗是否自然结束 |
| `victory` | `bool` | 是否以胜利结束；战斗进行中也为 `False` |
| `state_hash` | `str` | 状态校验哈希，用于复现与一致性检查；不是模型特征 |
| `state_handle` | `str` | 当前 worker 的分支/恢复句柄；不是动作 ID，也不是模型特征 |
| `transition` | `dict`，可缺省 | 产生本次结果的操作说明；`observe` 时可没有此字段 |
| `scoring_features` | `dict`，协议允许缺省 | 另一份用于评分的特征投影；不是标量奖励 |

当前原生 `state` **没有** `reward`、`truncated`、四动作 `mask`，也不是 Gym 常见的 `(obs, reward, terminated, truncated, info)` 元组。后续需要我们单独定义奖励和动作数上限造成的截断。

`state_hash` 同时考虑原始观测和部分执行状态，不能用模型输入的编码向量自行重算并期待得到同一个值。`state_handle` 可能被缓存淘汰，也不保证能拿到另一个 worker 中恢复。

## 4 observation 和 combat

### observation

| 字段 | 类型 | 当前战斗中的内容 |
|---|---|---|
| `schema_version` | `int` | `2` |
| `game_build` | `dict` | `version`、`assembly_sha256`、`pck_sha256`，均为字符串 |
| `run` | `dict` | 种子、进阶等级、金币和随机数流计数 |
| `combat` | `dict` | 回合、能量、角色、敌人、牌堆等 |
| `inventory` | `dict` | 遗物和药水槽 |
| `outstanding_choice` | `dict`，可缺省 | 待处理的选牌/选项；当前三种基础牌的记录中没有 |
| `decision` | `dict` | `kind` 和 `legal_actions` |
| `terminal` | `bool` | 与当前战斗 `state["terminated"]` 一致，注意字段拼写不同 |
| `victory` | `bool` | 与 `state["victory"]` 一致 |

`decision.kind` 在当前记录中为 `"combat_action"` 或 `"terminal"`。上游还有需要额外选择的决策类型，不能把这两个值当成整个游戏的完整枚举。

`observation.decision.legal_actions` 和 `state.legal_actions` 是当前结果中的同一份动作内容，不需要拼接两者。

`game_build.version` 是 DLL 产品版本，本次为 `0.1.0+59260271157f76a2896f0eab5bc6ea1245d8b314`，不是发布版本字符串 `v0.107.1`。

### run

| 字段 | 类型 | 说明 |
|---|---|---|
| `seed` | `str` | 此次重置的种子，用于实验复现，不给策略 |
| `ascension` | `int` | 进阶等级，当前为 `0` |
| `gold` | `int` | 当前金币，当前为 `0` |
| `rng_counters` | `dict[str, int]` | 各随机数流已推进的计数，例如 `Shuffle`、`MonsterAi`；不提供给策略 |

### combat

| 字段 | 类型 | 初始实例或含义 |
|---|---|---|
| `turn` | `int` | 回合编号，初始为 `1`；不是已执行动作数 |
| `phase` | `str` | 当前战斗阶段，本次决策点为 `"Play"` |
| `energy` | `int` | 当前剩余能量，初始为 `3` |
| `max_energy` | `int` | 原生 `MaxEnergy` 值，初始为 `3`；不能据此断言 `energy` 永远不超过它 |
| `stars` | `int` | 星资源，当前 Ironclad 任务为 `0` |
| `creatures` | `list[dict]` | 玩家与敌人的对象列表，长度会变化 |
| `piles` | `list[dict]` | 五个牌堆对象，具体见第 6 节 |
| `orbs` | `dict`，可缺省 | 充能球队列；本次为 `{"capacity": 0, "entries": []}` |

一次 `step` 是执行一次决策，例如打一张牌或者结束回合。打出 Strike 后通常仍然是同一回合；执行 `end_turn` 会推进敌人行动和下一回合流程。

## 5 玩家和敌人 creatures

初始敌人的原始数据如下：

```json
{
  "combat_id": 1,
  "model_id": "NIBBIT",
  "side": "Enemy",
  "hp": 46,
  "max_hp": 46,
  "block": 0,
  "alive": true,
  "next_move": {
    "id": "BUTT_MOVE",
    "intents": [
      {"intent_type": "Attack", "implementation": "SingleAttackIntent", "damage": 12, "repeats": 1}
    ]
  },
  "powers": []
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `combat_id` | `int` | 战斗内实体标识，用于动作目标匹配；不是数组位置 |
| `model_id` | `str` | 角色/怪物类型，如 `IRONCLAD`、`NIBBIT` |
| `side` | `str` | 本任务为 `Player` 或 `Enemy` |
| `hp` / `max_hp` | `int` | 当前生命与最大生命；这里字段叫 `hp`，不叫 `current_hp` |
| `block` | `int` | 当前格挡 |
| `alive` | `bool` | 是否存活 |
| `powers` | `list[dict]` | 力量、易伤等状态，每项含 `model_id` 和数值 `amount` |
| `next_move` | `dict`，可缺省 | 敌人的下一次行动信息，玩家通常没有该字段 |
| `next_move.id` | `str` | 游戏内部行动标识，不直接作为玩家可见特征 |
| `next_move.intents` | `list[dict]` | 当前显示的行动意图，可包含多个项目 |

每个 intent 的字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `intent_type` | `str` | 意图类别，如 `Attack`、`Defend`、`Buff`；此处不是完整枚举 |
| `implementation` | `str` | 原生类名，用于诊断，不作为策略输入 |
| `damage` | 数值，可缺省 | 攻击意图的单次伤害值；不是实际结算后的玩家掉血量 |
| `repeats` | `int`，可缺省 | 攻击次数；非攻击意图通常没有这个字段 |

初始 `damage=12`、`repeats=1` 表示一次 12 点攻击。实际扣血还受格挡等机制影响，不能直接把意图伤害当成环境奖励或血量变化。

非攻击意图不一定提供效果数值。例如本次原始记录中的 Defend 意图只有 `intent_type` 和 `implementation`，没有敌人将获得多少格挡的字段。

状态层数也会影响决策。本次闭环记录中敌人出现了 `STRENGTH_POWER`；不能只读取血量和手牌、忽略 `powers`。

读取时按 `side` 或 `combat_id` 查找。敌人死亡后可能直接从列表中消失：本次胜利状态只剩玩家，不能一直使用 `creatures[1]` 读取敌人。

## 6 牌堆和卡牌

### piles

每个牌堆是 `{"name": str, "type": str, "cards": list[dict]}`。

| `name` | `type` | 含义 | 初始卡牌数 |
|---|---|---|---:|
| `Hand` | `Hand` | 手牌 | 5 |
| `DrawPile` | `Draw` | 抽牌堆 | 5 |
| `DiscardPile` | `Discard` | 弃牌堆 | 0 |
| `ExhaustPile` | `Exhaust` | 消耗堆 | 0 |
| `PlayPile` | `Play` | 正在打出/结算中的牌 | 0 |

`cards` 长度会随动作变化，不固定为 5。用 `name` 找牌堆，不把列表位置当作牌堆语义。

**原始 `DrawPile.cards` 保留真实顺序。** 给策略时应改成公开可知的无序卡牌统计或数量，不保留顺序，也不通过内部 ID 推断顺序。`scoring_features.combat.piles.draw` 同样有这个问题。

### cards

一张初始 Strike 的真实数据：

```json
{
  "instance_id": "strike-0",
  "net_id": 0,
  "model_id": "STRIKE_IRONCLAD",
  "card_type": "Attack",
  "target_type": "AnyEnemy",
  "energy_cost": 1,
  "costs_x": false,
  "upgrades": 0,
  "native_state": {}
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `instance_id` | `str` | 具体卡牌实例的标识；两张 Strike 有不同 ID，用于动作桥接 |
| `net_id` | `int` | 原生卡牌执行标识，对应动作的 `parameters.card_id` |
| `model_id` | `str` | 卡牌种类，如 `STRIKE_IRONCLAD`、`DEFEND_IRONCLAD`、`BASH` |
| `card_type` | `str` | 卡牌类别，本任务有 `Attack`、`Skill` |
| `target_type` | `str` | 目标类型，本任务有 `AnyEnemy`、`Self` |
| `energy_cost` | `int` | 原生系统解析后的当前费用，不能仅凭基础卡种写死所有场景的费用 |
| `costs_x` | `bool` | 是否属于 X 费卡，本任务为 `False` |
| `upgrades` | `int` | 当前升级等级，本任务为 `0` |
| `enchantment` | `dict`，可缺省 | 附魔，包含 `model_id` 和数值 `amount`；无附魔时不出现 |
| `native_state` | `dict` | 游戏保存属性的投影；值可为整数、布尔、字符串、整数列表等，当前三种牌为 `{}` |

这里**没有统一的 `damage`、`block`、卡牌说明文本字段**。例如不能直接写 `card["damage"]` 读取 Strike 的 6 点伤害。牌的效果由游戏逻辑执行，`native_state` 也不是一份完整的效果说明。

`net_id` 不等于当前手牌下标。第一张 Strike 打出后，剩余手牌的第 0 张是 `strike-1`，其 `net_id` 仍为 `1`。

## 7 遗物 药水和充能球

| 路径 | 类型 | 内容 |
|---|---|---|
| `inventory.relics` | `list[dict]` | 遗物列表，本任务为 `[]` |
| `relics[*].model_id` | `str` | 遗物类型 |
| `relics[*].counter` | 数值，可缺省 | 仅在遗物显示计数时提供 |
| `relics[*].native_state` | `dict` | 遗物的原生保存属性，不直接当作可见特征 |
| `inventory.potions` | `list[dict \| None]` | 药水槽列表，本任务为 `[None, None, None]` |
| 非空药水槽对象 | `dict` | `slot: int`、`model_id: str`；槽位从 0 开始 |
| `combat.orbs.capacity` | `int` | 充能球槽容量，本任务为 `0` |
| `combat.orbs.entries` | `list[dict]` | 球对象含 `model_id`、数值 `passive` / `evoke`、`native_state` |

“没有药水”和“没有药水槽”不是一回事：三个 `null` 表示三个空槽，不能直接用 `len(potions)` 当药水数量。

## 8 合法动作和四动作 mask

### 原生 legal_actions

当前初始状态的列表有 **6 项**：两张 Strike、两张 Defend、一张 Bash、结束回合。以下是三种实际结构：

```json
[
  {
    "action_id": "play:strike-0:target:1",
    "kind": "play_card",
    "parameters": {"instance_id": "strike-0", "card_id": 0, "target_id": 1}
  },
  {
    "action_id": "play:defend-0:none",
    "kind": "play_card",
    "parameters": {"instance_id": "defend-0", "card_id": 2, "target_id": null}
  },
  {"action_id": "end_turn", "kind": "end_turn", "parameters": {}}
]
```

上面是列表中的三项摘录，不是初始完整动作列表。

| 字段 | 类型 | 用法 |
|---|---|---|
| `action_id` | `str` | 从当前列表中取出并原样传给 `step`，不要自己拼字符串 |
| `kind` | `str` | 本任务为 `play_card`、`end_turn` |
| `parameters` | `dict` | 参数随动作种类变化；结束回合为空字典 |
| `parameters.instance_id` | `str` | 对应手牌的 `instance_id` |
| `parameters.card_id` | `int` | 对应卡牌的 `net_id` |
| `parameters.target_id` | `int \| None` | 对应目标 `combat_id`；不需要显式目标时为 `None` |

Defend 的 `target_type` 是 `Self`，但原生合法动作中的 `target_id` 是 `null`，不需要人为改为玩家 ID。

执行动作只需 `{"action_id": 选中的字符串}`，不需要把整个卡牌或全部参数回传。每次 `step` 后都应读取新返回的合法动作，旧动作不保证仍有效。

### 本项目的四动作结果

```python
mask, action_ids = get_actions(state)
```

二者目前都是 Python 列表，长度为 4，还不是 NumPy 数组或 PyTorch 张量。

| 模型编号 | 含义 | 初始对应的原生 action_id |
|---:|---|---|
| 0 | Strike | `play:strike-0:target:1` |
| 1 | Defend | `play:defend-0:none` |
| 2 | Bash | `play:bash-0:target:1` |
| 3 | 结束回合 | `end_turn` |

初始得到 `mask = [True, True, True, True]`。能量为 0、只能结束回合时得到 `[False, False, False, True]`，前三个 `action_ids` 为 `None`。

终止时得到四个 `False` 和四个 `None`。此时应结束 episode，不能继续对全 False 的 mask 做 softmax 或采样。

这个合并只适用于当前单敌人、三种未升级基础牌、无费用改动和已支持动作的任务。多目标、附魔、临时改牌和选牌事件需要扩展动作表示，不能把四个编号当成整个 STS2 的动作空间。

## 9 transition 和 scoring_features

`transition` 描述操作本身，不是完整的 `(obs, action, reward, next_obs)` 训练样本。

实际 reset：

```json
{"kind": "reset", "replayed_actions": 0}
```

实际单步 Strike：

```json
{
  "kind": "play_card",
  "action_id": "play:strike-0:target:1",
  "elapsed_ms": 66.3043,
  "history_length": 1
}
```

`elapsed_ms` 是该原生操作测得的耗时，不含随后生成快照的 `Capture`，也不等于完整 Python 往返或 worker 启动耗时。它不是奖励。`history_length` 是动作历史长度，和 `combat.turn` 不是同一个计数。不同操作的 transition 字段会变化，不能假设每次都有 `elapsed_ms`。

`scoring_features` 当前版本为 `3`，与观测版本 `2` 分别维护。主要字段如下：

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | `int` | `3` |
| `character` | `str` | 角色类型 |
| `ascension`、`current_hp`、`max_hp`、`block`、`gold` | `int` | 玩家概况；这里使用 `current_hp`，与 creature 的 `hp` 区分 |
| `act_index`、`act_floor` | `int` | 幕和层信息，本任务均为 `0` |
| `map_col`、`map_row` | `int`，可缺省 | 地图坐标，当前孤立战斗样例中缺省 |
| `deck` | `list[dict]` | 整副牌的投影：`model_id`、`upgrades`、可选的附魔类型字符串 `enchantment` |
| `relics` | `list[str]` | 遗物类型列表；和 `observation.inventory.relics` 的对象列表不同 |
| `potion_count`、`potion_capacity` | `int` | 药水数量和槽位容量，当前为 `0`、`3` |
| `combat` | `dict`，可缺省 | `turn`、`energy`、`max_energy`、`stars`、`piles` 和 `creatures` |

这里的 `combat.piles` 是字典，键为 `hand`、`draw`、`discard`、`exhaust`、`play`，值为只含 `model_id` 和 `upgrades` 的卡牌列表。它与 `observation.combat.piles` 的列表结构不同。

这里的 `combat.creatures` 类似原始 creatures，但没有 `combat_id`。它仍有内部 `next_move.id`；`piles.draw` 也仍有真实顺序。因此 `scoring_features` 不能因为名字带 features 就整包给策略，也不能因为名字带 scoring 就当成已计算好的 reward。

## 10 结束状态 缺省字段和数据类型

当前孤立战斗中：

| 情况 | `terminated` | `victory` |
|---|---|---|
| 仍在战斗 | `False` | `False` |
| 战斗胜利 | `True` | `True` |
| 玩家死亡 | `True` | `False` |
| 脚本达到动作数上限 | 原生值不因此自动改变 | 不因此自动改变 |

动作数上限属于外层 `truncated` 的职责。进程超时或接口报错则是执行失败，应单独记录，不能自动当作游戏死亡。`ok` 表示请求是否处理成功，和 `victory` 无关。

JSON 中的 `true`、`false`、`null` 读入 Python 后分别是 `True`、`False`、`None`。对象变为 `dict`，数组变为 `list`；数值字段需要根据用途转换为训练张量。

**缺字段不等于一定存在一个值为 null 的字段。** 当前 C# 序列化会省略很多值为 null 的对象属性，例如玩家的 `next_move`、无附魔卡的 `enchantment`、非攻击意图的 `damage` / `repeats`、没有选择时的 `outstanding_choice`。字典中的 `target_id: null` 和药水槽数组中的 `null` 则会保留。

读取可选值可用 `creature.get("next_move")`、`intent.get("damage")`；不要把缺失的攻击伤害误当成所有意图都不造成效果。

## 11 真实例子和读取代码

三个 JSON 文件保留完整原生响应，只改变缩进，均来自本机运行记录：

- [reset_response.json](examples/reset_response.json)：初始化完成后的响应。
- [step_response.json](examples/step_response.json)：打出 `strike-0` 后的响应。
- [terminal_response.json](examples/terminal_response.json)：固定随机合法动作探针取得战斗胜利后的响应。

在项目根目录运行下面的代码，只读取已有文件，不启动游戏：

```python
import json
from pathlib import Path
from action_space import get_actions

response = json.loads(Path("docs/examples/reset_response.json").read_text(encoding="utf-8"))
state = response["result"]
combat = state["observation"]["combat"]

player = next(c for c in combat["creatures"] if c["side"] == "Player")
enemies = [c for c in combat["creatures"] if c["side"] == "Enemy" and c["alive"]]
hand = next(p for p in combat["piles"] if p["name"] == "Hand")["cards"]
mask, action_ids = get_actions(state)

print(player["hp"], combat["energy"])                 # 80 3
print([(c["model_id"], c["hp"]) for c in enemies])   # [('NIBBIT', 46)]
print([c["model_id"] for c in hand])
print(mask)                                         # [True, True, True, True]
```

两个非终止样例的差别：

| 数据 | reset 后 | Strike 后 |
|---|---:|---:|
| `combat.turn` | 1 | 1 |
| `combat.energy` | 3 | 2 |
| 玩家 HP | 80 | 80 |
| Nibbit HP | 46 | 40 |
| 手牌数 | 5 | 4 |
| 弃牌数 | 0 | 1 |
| 原生合法动作数 | 6 | 5 |
| 四动作 mask | 全 True | 全 True |
| `terminated` | False | False |

第一张 Strike 消耗了一张卡和一点能量，但手里还有另一张 Strike，且 Bash 的 2 点费用仍然付得起，所以四动作 mask 不变。

胜利样例中 `terminated=True`、`victory=True`、`legal_actions=[]`，玩家剩余 60 HP。此时 `phase` 仍为 `"Play"`，并且致胜卡仍在 `PlayPile`。应以终止标志判断战斗结束，不能要求 phase 改名或所有牌都已进弃牌堆。

## 12 哪些字段用于决策

| 数据 | 当前使用原则 |
|---|---|
| 玩家/敌人生命、格挡、可见状态层数，能量、手牌类型和费用 | 可作为观测编码的基础 |
| 当前公开意图的 `intent_type`、`damage`、`repeats` | 可用于表示敌人意图，需保留“非攻击/缺省”的区别 |
| 公开可知的抽牌堆组成 | 只用无序统计或数量，不能保留真实顺序 |
| `legal_actions` 和卡牌/目标 ID | 环境桥接使用，用于构建 mask、定位实际执行对象；不把内部编号当成连续特征 |
| `seed`、`rng_counters`、`state_hash`、`state_handle`、版本哈希 | 只用于复现、校验和诊断 |
| `next_move.id`、`implementation`、未经审计的 `native_state` | 不直接给策略 |
| `scoring_features` | 如需使用，按明确字段提取并移除隐藏信息；不是已经安全处理的模型输入 |
| 探针 `initial` / `after` 摘要 | 方便人阅读；会省略部分字段，不等于完整的训练观测设计 |

上游另有 `extract_agent_observation()`，会屏蔽种子/RNG并把抽牌堆改成无序表示，但仍复制一些内部标识和其他原始对象。本项目当前没有把它直接接到策略；后续仍需逐项定义自己的观测编码。

固定的角色和牌组使第一步更容易，但并不意味着只用几个 HP 数字就描述了全部决策条件。当前基础编码及维度见第 14 节。

## 13 依据和版本维护

字段定义以当前固定提交为准：

- [Messages.cs](../.setup/divine-sts2/src/Sts2.NativeSim.Protocol/Messages.cs)：协议封套、动作、reset 参数和 `EnvironmentResult`。
- [Main.cs](../.setup/divine-sts2/src/Sts2.NativeSim.GodotHost/Main.cs)：第 14 行序列化规则，第 196 行起的请求分发。
- [PersistentNativeCombatEnvironment.cs](../.setup/divine-sts2/src/Sts2.NativeSim.Core/PersistentNativeCombatEnvironment.cs)：第 673 行起生成战斗快照，第 734 行起生成合法动作，第 1677 行起生成评分特征，第 1920 行起生成 creature、intent 和 card。
- [observations.py](../.setup/divine-sts2/python/sts2_native_sim/observations.py)：上游观测投影；其输出与原始状态格式不同。
- [reset 样例](examples/reset_response.json)、[step 样例](examples/step_response.json)与[终止样例](examples/terminal_response.json)：保留的协议数据示例。
- [probe_combat.py](../scripts/probe_combat.py)：本项目摘要与日志的产生方式。

更新游戏或原生工具后，重新核对版本、字段和真实响应。本文的战斗终止语义不能直接套到上游完整 run 模式；完整 run 中打赢一场战斗后还可能进入奖励或地图阶段。

## 14 当前模型输入 32 维

[observation.py](../../observation.py) 提供 `get_observation(state)`，返回形状为 `(32,)` 的 NumPy `float32` 数组。`feature_names` 按同样顺序列出所有名称；`observation_size` 为 32。mask 保持单独传入动作选择函数。

| 索引 | 内容 | 缩放 |
|---|---|---|
| 0 | 回合数 | 除以 10 |
| 1～2 | 当前能量、原生 MaxEnergy | 除以 3 |
| 3～4 | 玩家当前/最大生命 | 除以 80 |
| 5 | 玩家格挡 | 除以 20 |
| 6 | 是否存在存活敌人 | 0 或 1 |
| 7～8 | 敌人当前/最大生命 | 除以 50 |
| 9 | 敌人格挡 | 除以 20 |
| 10 | 敌人力量层数 | 除以 10 |
| 11 | 敌人易伤层数 | 除以 3 |
| 12 | 是否有攻击意图 | 0 或 1 |
| 13 | 攻击意图总伤害，即单次伤害乘次数 | 除以 20 |
| 14 | 攻击次数 | 除以 5 |
| 15～16 | 是否有防御、增益意图 | 各为 0 或 1 |
| 17～31 | 五个牌堆，每堆依次统计 Strike、Defend、Bash 数量 | 三种牌分别除以 5、4、1 |

牌堆顺序固定为 `Hand`、`DrawPile`、`DiscardPile`、`ExhaustPile`、`PlayPile`。例如初始手牌数量 `[2, 2, 1]` 编码成 `[0.4, 0.5, 1.0]`。只统计数量，牌堆的排列和内部实例 ID 不影响编码。

这些除数只是缩放尺度，不是硬上限，不把结果裁剪到 `[0, 1]`。例如能量为 6 时对应特征为 2。终止后没有存活敌人时，敌人和意图部分均填零。

当前编码器只支持既定角色、单 Nibbit、三种未改动基础牌、无遗物药水、无玩家状态，以及敌人的力量和易伤。未知卡牌、状态、意图或额外选择会报错，不静默忽略。

它是供当前小任务使用的可见观测，不宣称是完整 Markov 状态；更一般敌人的隐藏行动历史等信息可能需要历史观测。
