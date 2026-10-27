"""固定 Ironclad 对 Nibbit 战斗的可见观测；不读取 RNG 或抽牌顺序。"""

import numpy as np


card_names = ["STRIKE_IRONCLAD" , "DEFEND_IRONCLAD" , "BASH"]
card_costs = [1 , 1 , 2]
card_counts = [5 , 4 , 1]
pile_names = ["Hand" , "DrawPile" , "DiscardPile" , "ExhaustPile" , "PlayPile"]
feature_names = [
    "turn" , "energy" , "max_energy" ,
    "player_hp" , "player_max_hp" , "player_block" ,
    "enemy_present" , "enemy_hp" , "enemy_max_hp" , "enemy_block" ,
    "enemy_strength" , "enemy_vulnerable" ,
    "intent_attack" , "intent_damage_total" , "intent_hits" , "intent_defend" , "intent_buff" ,
] + [f"{pile}_{card}" for pile in pile_names for card in card_names]
observation_size = len(feature_names)  # 32


def get_observation(state):
    obs = state["observation"]
    combat = obs["combat"]
    if obs.get("outstanding_choice") is not None:
        raise ValueError("Card/option choices need a different observation encoder.")
    if not state["terminated"] and combat["phase"] != "Play":
        raise ValueError("Expected a player decision in the Play phase.")
    inventory = obs["inventory"]
    orbs = combat.get("orbs" , {})
    if (inventory["relics"] or any(p is not None for p in inventory["potions"])
            or combat["stars"] != 0 or orbs.get("entries")):
        raise ValueError("This encoder supports no relics, potions, stars or orbs.")

    players = [c for c in combat["creatures"] if c["side"] == "Player"]
    if len(players) != 1 or players[0]["model_id"] != "IRONCLAD":
        raise ValueError("Expected exactly one Ironclad player.")
    player = players[0]
    if player["powers"]:
        raise ValueError("Player powers need additional observation features.")
    if any(c["side"] not in ("Player" , "Enemy") or
           (c["side"] == "Enemy" and c["model_id"] != "NIBBIT") for c in combat["creatures"]):
        raise ValueError("This encoder supports only the Nibbit encounter.")
    enemies = [c for c in combat["creatures"] if c["side"] == "Enemy" and c["alive"]]
    if len(enemies) > 1 or (not state["terminated"] and len(enemies) != 1):
        raise ValueError("Expected one living enemy before termination.")
    enemy = enemies[0] if enemies else None
    powers = {p["model_id"]: p["amount"] for p in enemy["powers"]} if enemy else {}
    if set(powers) - {"STRENGTH_POWER" , "VULNERABLE_POWER"}:
        raise ValueError("Unknown enemy powers need additional observation features.")
    intents = (enemy.get("next_move") or {}).get("intents" , []) if enemy else []
    if any(i["intent_type"] not in ("Attack" , "Defend" , "Buff") for i in intents):
        raise ValueError("Unknown enemy intent.")
    attacks = [i for i in intents if i["intent_type"] == "Attack"]
    if len(attacks) > 1 or any(i.get("damage") is None or i.get("repeats") is None for i in attacks):
        raise ValueError("Expected at most one fully described attack intent.")

    # 除数只是缩放尺度，不做 [0, 1] 截断；超过尺度的值仍保留。
    values = [
        combat["turn"] / 10 , combat["energy"] / 3 , combat["max_energy"] / 3 ,
        player["hp"] / 80 , player["max_hp"] / 80 , player["block"] / 20 ,
        float(enemy is not None) ,
        enemy["hp"] / 50 if enemy else 0 ,
        enemy["max_hp"] / 50 if enemy else 0 ,
        enemy["block"] / 20 if enemy else 0 ,
        powers.get("STRENGTH_POWER" , 0) / 10 , powers.get("VULNERABLE_POWER" , 0) / 3 ,
        float(bool(attacks)) , sum(i["damage"] * i["repeats"] for i in attacks) / 20 ,
        sum(i["repeats"] for i in attacks) / 5 ,
        float(any(i["intent_type"] == "Defend" for i in intents)) ,
        float(any(i["intent_type"] == "Buff" for i in intents)) ,
    ]

    piles = {p["name"]: p["cards"] for p in combat["piles"]}
    if set(piles) != set(pile_names) or len(combat["piles"]) != len(pile_names):
        raise ValueError("Expected the five standard combat piles.")
    total_counts = [0 , 0 , 0]
    for pile_name in pile_names:
        counts = [0 , 0 , 0]
        for card in piles[pile_name]:
            if card["model_id"] not in card_names:
                raise ValueError(f"Unsupported card: {card['model_id']}")
            index = card_names.index(card["model_id"])
            if (card["upgrades"] != 0 or card["energy_cost"] != card_costs[index]
                    or card["costs_x"] or card.get("enchantment") is not None
                    or card.get("native_state") not in (None , {})):
                raise ValueError("Modified cards need additional observation features.")
            counts[index] += 1
            total_counts[index] += 1
        values.extend(count / scale for count , scale in zip(counts , card_counts))
    if total_counts != card_counts:
        raise ValueError("Expected the fixed deck: 5 Strike, 4 Defend, 1 Bash.")
    return np.array(values , dtype = np.float32)
