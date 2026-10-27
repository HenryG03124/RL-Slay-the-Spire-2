import numpy as np
import torch

action_names = ["Strike" , "Defend" , "Bash" , "End turn"]
card_names = ["STRIKE_IRONCLAD" , "DEFEND_IRONCLAD" , "BASH"]
card_costs = [1 , 1 , 2]


def get_actions(state):
    action_ids = [None , None , None , None]
    if state["terminated"]:
        return [False , False , False , False] , action_ids

    combat = state["observation"]["combat"]
    enemies = []
    for creature in combat["creatures"]:
        if creature["side"] == "Enemy" and creature["alive"]:
            enemies.append(creature)
    if len(enemies) != 1:
        raise ValueError("This action mapping requires exactly one living enemy.")

    hand = None
    for pile in combat["piles"]:
        if pile["name"] == "Hand":
            hand = pile["cards"]
            break
    if hand is None:
        raise StopIteration
    cards = {}
    for card in hand:
        cards[card["instance_id"]] = card

    #Merge only equivalent base cards.
    for card in hand:
        if card["model_id"] not in card_names:
            raise ValueError(f"Unsupported card: {card['model_id']}")
        index = card_names.index(card["model_id"])
        if (card["upgrades"] != 0 or card["energy_cost"] != card_costs[index]
                or card.get("native_state") not in (None , {})):
            raise ValueError("Modified cards need a more detailed action mapping.")

    #Use native legal actions.
    for action in state["legal_actions"]:
        if action["kind"] == "end_turn":
            index = 3
        elif action["kind"] == "play_card":
            card = cards[action["parameters"]["instance_id"]]
            index = card_names.index(card["model_id"])
            target = action["parameters"].get("target_id")
            if index in (0 , 2) and target != enemies[0]["combat_id"]:
                raise ValueError("Attack target differs from the sole living enemy.")
        else:
            raise ValueError(f"Unsupported action kind: {action['kind']}")
        #Keep the first legal copy.
        if action_ids[index] is None:
            action_ids[index] = action["action_id"]

    mask = []
    for action_id in action_ids:
        mask.append(action_id is not None)
    if not any(mask):
        raise ValueError("Nonterminal state has no supported legal actions.")
    return mask , action_ids

def select_action(logits , mask): #[B , 4] , [B , 4]
    logits = logits.masked_fill(~mask , float("-inf")) #[B , 4]

    distributions = torch.distributions.Categorical(logits = logits)
    action = distributions.sample() #[B]
    log_prob = distributions.log_prob(action) #[B]

    return action , log_prob
