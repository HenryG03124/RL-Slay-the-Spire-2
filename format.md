state: dict
{
    observation: dict
    {
        schema_version: int
        game_build: dict（游戏版本信息）
        run: dict（种子、进阶等级、金币等对局信息）
        combat: dict
        {
            turn: int（回合数）
            phase: str（战斗阶段）
            energy: int（当前能量）
            max_energy: int（最大能量）
            stars: int（星资源）
            creatures: list[dict]（玩家和敌人）
            piles: list[dict]（各牌堆及其中的卡牌）
            orbs: dict（充能球，可缺省）
        }
        inventory: dict
        {
            relics: list[dict]（遗物）
            potions: list[dict | None]（药水槽，空槽为 None）
        }
        outstanding_choice: dict（待处理的选择，可缺省）
        decision: dict
        {
            kind: str（决策类型）
            legal_actions: list[dict]（与 state 顶层的合法动作相同）
        }
        terminal: bool（与 state 顶层的 terminated 相同）
        victory: bool
    }
    legal_actions: list[dict]
    terminated: bool
    victory: bool
    state_hash: str
    state_handle: str
    transition: dict（可缺省）
    scoring_features: dict（可缺省）
}

get_observation(state): np.ndarray，float32，形状 (32,)

obs: torch.Tensor
{
    dtype: torch.float32
    shape: [B , 32]
    [
        0: 回合数 / 10
        1: 当前能量 / 3
        2: 最大能量 / 3

        3: 玩家当前生命 / 80
        4: 玩家最大生命 / 80
        5: 玩家格挡 / 20

        6: 是否存在存活敌人（1 或 0）
        7: 敌人当前生命 / 50
        8: 敌人最大生命 / 50
        9: 敌人格挡 / 20
        10: 敌人力量 / 10
        11: 敌人易伤层数 / 3

        12: 是否有攻击意图（1 或 0）
        13: 预告的总攻击伤害 / 20
        14: 预告的攻击次数 / 5
        15: 是否有防御意图（1 或 0）
        16: 是否有增益意图（1 或 0）

        17: 手牌中 Strike 数量 / 5
        18: 手牌中 Defend 数量 / 4
        19: 手牌中 Bash 数量

        20: 抽牌堆中 Strike 数量 / 5
        21: 抽牌堆中 Defend 数量 / 4
        22: 抽牌堆中 Bash 数量

        23: 弃牌堆中 Strike 数量 / 5
        24: 弃牌堆中 Defend 数量 / 4
        25: 弃牌堆中 Bash 数量

        26: 消耗堆中 Strike 数量 / 5
        27: 消耗堆中 Defend 数量 / 4
        28: 消耗堆中 Bash 数量

        29: 出牌区中 Strike 数量 / 5
        30: 出牌区中 Defend 数量 / 4
        31: 出牌区中 Bash 数量
    ]
}

get_action_candidates(state): list[dict]，长度 N（当前合法动作数）

candidate_actions:
[
    {
        action_id: str（当前状态下唯一的动作 ID，原样传给 env.step）
        kind: str（play_card 或 end_turn，多个候选可以属于同一类别）
        card: dict | None（按 instance_id 找到的完整手牌字典，结束回合为 None）
        target: dict | None（按 combat_id 找到的完整生物字典，无显式目标为 None）
    }
]

每项对应一个合法动作，与 state["legal_actions"] 顺序相同。
同名的不同牌、同一张牌的不同目标分别保留，不合并。
target 表示操作时指定的目标，不是所有受到效果影响的生物。
结束回合的 card 和 target 都是 None；终止状态返回 []。
当前只支持 play_card 和 end_turn，其他动作类型报错。

get_action_features(candidate_actions): np.ndarray

features: np.ndarray
{
    dtype: np.float32
    shape: [N , 10]（N 是候选数，没有 batch 维）
    每行:
    [
        0: 是否结束回合
        1: 是否 Strike
        2: 是否 Defend
        3: 是否 Bash
        4: 卡牌能量费用 / 3
        5: 卡牌升级次数

        6: 是否有显式目标
        7: 目标当前生命 / 50
        8: 目标最大生命 / 50
        9: 目标格挡 / 20
    ]
}

第 i 行对应 candidate_actions[i]；是否类特征用 1 或 0。
结束回合只有第 0 项为 1，其余为 0。
target 为 None 时，第 6 到 9 项都是 0。
空列表返回形状 (0 , 10) 的数组。
当前只编码 Strike、Defend、Bash 和结束回合，其他卡种或动作类型报错。
字符串 ID 不参与数值编码，执行时使用对应候选的 action_id。
