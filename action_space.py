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

def get_action_candidates(state):
    if state["terminated"]:
        return []

    combat = state["observation"]["combat"]

    piles = combat["piles"] #list
    creatures = combat["creatures"] #list
    legal_actions = state["legal_actions"] #list

    for pile in piles:
        if pile["name"] == "Hand":
            cards_hand = pile["cards"] #list[dict]
            break

    cards_hand_dict = {card["instance_id"] : card for card in cards_hand}
    creatures_dict = {creature["combat_id"] : creature for creature in creatures}

    candidate_actions = []
    for action in legal_actions:
        temp = {}
        temp["action_id"] = action["action_id"]
        temp["kind"] = action["kind"]
        if temp["kind"] == "end_turn":
            temp["card"] = None
            temp["target"] = None
        elif temp["kind"] == "play_card":
            parameters = action["parameters"]
            temp["card"] = cards_hand_dict[parameters["instance_id"]]
            temp["target"] = creatures_dict[parameters["target_id"]] if parameters["target_id"] is not None else None
        else:
            raise ValueError("illegal action kind.")
        candidate_actions.append(temp)

    return candidate_actions

def get_action_features(candidate_actions):
    features = np.zeros((len(candidate_actions) , 10) , dtype = np.float32) #[N , 10]
    for index , action in enumerate(candidate_actions):
        if action["kind"] == "end_turn":
            features[index , 0] = 1
            continue
        if action["kind"] != "play_card":
            raise ValueError(f"Unsupported action kind: {action['kind']}")

        card = action["card"]
        if card["model_id"] not in card_names:
            raise ValueError(f"Unsupported card: {card['model_id']}")
        card_index = card_names.index(card["model_id"])
        features[index , card_index + 1] = 1 #Strike, Defend, Bash
        features[index , 4] = card["energy_cost"] / 3
        features[index , 5] = card["upgrades"]

        target = action["target"]
        if target is not None:
            features[index , 6] = 1
            features[index , 7] = target["hp"] / 50
            features[index , 8] = target["max_hp"] / 50
            features[index , 9] = target["block"] / 20

    return features

def select_action(logits , mask): #[B , 4] , [B , 4]
    logits = logits.masked_fill(~mask , float("-inf")) #[B , 4]

    distributions = torch.distributions.Categorical(logits = logits)
    action = distributions.sample() #[B]
    log_prob = distributions.log_prob(action) #[B]

    return action , log_prob
