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
