#Visible features for the fixed Ironclad vs Nibbit combat.

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
]
for pile_name in pile_names:
    for card_name in card_names:
        feature_names.append(f"{pile_name}_{card_name}")
observation_size = len(feature_names) #32


def get_observation(state):
    obs = state["observation"]
    combat = obs["combat"]
    if obs.get("outstanding_choice") is not None:
        raise ValueError("Card/option choices need a different observation encoder.")
    if not state["terminated"] and combat["phase"] != "Play":
        raise ValueError("Expected a player decision in the Play phase.")
    inventory = obs["inventory"]
    orbs = combat.get("orbs" , {})
    has_potion = False
    for potion in inventory["potions"]:
        if potion is not None:
            has_potion = True
            break
    if (inventory["relics"] or has_potion
            or combat["stars"] != 0 or orbs.get("entries")):
        raise ValueError("This encoder supports no relics, potions, stars or orbs.")

    players = []
    for creature in combat["creatures"]:
        if creature["side"] == "Player":
            players.append(creature)
    if len(players) != 1 or players[0]["model_id"] != "IRONCLAD":
        raise ValueError("Expected exactly one Ironclad player.")
    player = players[0]
    if player["powers"]:
        raise ValueError("Player powers need additional observation features.")

    enemies = []
    for creature in combat["creatures"]:
        if (creature["side"] not in ("Player" , "Enemy") or
                (creature["side"] == "Enemy" and creature["model_id"] != "NIBBIT")):
            raise ValueError("This encoder supports only the Nibbit encounter.")
        if creature["side"] == "Enemy" and creature["alive"]:
            enemies.append(creature)
    if len(enemies) > 1 or (not state["terminated"] and len(enemies) != 1):
        raise ValueError("Expected one living enemy before termination.")

    enemy = enemies[0] if enemies else None
    enemy_hp = 0
    enemy_max_hp = 0
    enemy_block = 0
    powers = {}
    intents = []
    if enemy is not None:
        enemy_hp = enemy["hp"]
        enemy_max_hp = enemy["max_hp"]
        enemy_block = enemy["block"]
        for power in enemy["powers"]:
            powers[power["model_id"]] = power["amount"]
        next_move = enemy.get("next_move") or {}
        intents = next_move.get("intents" , [])
    for power_name in powers:
        if power_name not in ("STRENGTH_POWER" , "VULNERABLE_POWER"):
            raise ValueError("Unknown enemy powers need additional observation features.")

    attacks = []
    intent_defend = False
    intent_buff = False
    for intent in intents:
        if intent["intent_type"] == "Attack":
            attacks.append(intent)
        elif intent["intent_type"] == "Defend":
            intent_defend = True
        elif intent["intent_type"] == "Buff":
            intent_buff = True
        else:
            raise ValueError("Unknown enemy intent.")
    if len(attacks) > 1:
        raise ValueError("Expected at most one fully described attack intent.")
    intent_damage = 0
    intent_hits = 0
    for attack in attacks:
        if attack.get("damage") is None or attack.get("repeats") is None:
            raise ValueError("Expected at most one fully described attack intent.")
        intent_damage += attack["damage"] * attack["repeats"]
        intent_hits += attack["repeats"]

    #Scale features without clipping.
    values = [
        combat["turn"] / 10 , combat["energy"] / 3 , combat["max_energy"] / 3 ,
        player["hp"] / 80 , player["max_hp"] / 80 , player["block"] / 20 ,
        float(enemy is not None) ,
        enemy_hp / 50 , enemy_max_hp / 50 , enemy_block / 20 ,
        powers.get("STRENGTH_POWER" , 0) / 10 , powers.get("VULNERABLE_POWER" , 0) / 3 ,
        float(bool(attacks)) , intent_damage / 20 , intent_hits / 5 ,
        float(intent_defend) , float(intent_buff) ,
    ]

    #Count cards without reading their order.
    piles = {}
    for pile in combat["piles"]:
        piles[pile["name"]] = pile["cards"]
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
        for index in range(len(card_counts)):
            values.append(counts[index] / card_counts[index])
    if total_counts != card_counts:
        raise ValueError("Expected the fixed deck: 5 Strike, 4 Defend, 1 Bash.")
    return np.array(values , dtype = np.float32)
