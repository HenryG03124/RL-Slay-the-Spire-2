import json
import torch

from model import ActorNet
from observation import get_observation
from action_space import get_actions , action_names
from game.environment import CombatEnv

device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
max_steps = 100
test_seeds = list(range(0 , 1000 , 50))
policies = ["actor" , "attack_first"]
action_priority = [2 , 0 , 1 , 3] #Bash , Strike , Defend , End turn

actor = ActorNet().to(device)
weights = torch.load("weights.pth" , map_location = device , weights_only = True)
actor.load_state_dict(weights["actor"])
actor.eval()
print("current device:" , device)

results = []
summary = {}

with torch.no_grad():
    for seed in test_seeds:
        for policy in policies:
            with CombatEnv(max_actions = max_steps) as env:
                state = env.reset(seed = seed)
                initial = {"observation" : state["observation"] , "legal_actions" : state["legal_actions"]}
                if policy == "actor":
                    initial_state = initial
                elif initial != initial_state:
                    raise ValueError(f"Different initial states for seed {seed}.")

                actions = []
                early_end = []
                excess_defend = 0

                for step in range(max_steps):
                    if state["terminated"]:
                        break

                    obs = torch.tensor(get_observation(state) , dtype = torch.float32 , device = device).unsqueeze(0) #[B , 32]
                    mask , action_ids = get_actions(state)
                    if policy == "actor":
                        mask_tensor = torch.tensor(mask , dtype = torch.bool , device = device).unsqueeze(0) #[B , 4]
                        logits = actor(obs) #[B , 4]
                        logits = logits.masked_fill(~mask_tensor , float("-inf"))
                        action = logits.argmax(dim = 1).item()
                    else:
                        for index in action_priority:
                            if mask[index]:
                                action = index
                                break

                    combat = state["observation"]["combat"]
                    for creature in combat["creatures"]:
                        if creature["side"] == "Player":
                            player = creature
                    incoming_damage = 0
                    for creature in combat["creatures"]:
                        if creature["side"] == "Enemy" and creature["alive"]:
                            for intent in (creature.get("next_move") or {}).get("intents" , []):
                                if intent["intent_type"] == "Attack":
                                    incoming_damage += intent["damage"] * intent["repeats"]

                    if action == 3 and any(mask[0 : 3]):
                        playable = []
                        for index in range(3):
                            if mask[index]:
                                playable.append(action_names[index])
                        early_end.append({
                            "step" : step + 1 , "turn" : combat["turn"] , "energy" : combat["energy"] ,
                            "hp" : player["hp"] , "block" : player["block"] ,
                            "incoming_damage" : incoming_damage , "playable" : playable
                        })
                    if action == 1 and player["block"] >= incoming_damage:
                        excess_defend += 1

                    actions.append(action_names[action])
                    state = env.step(action_ids[action])

                for creature in state["observation"]["combat"]["creatures"]:
                    if creature["side"] == "Player":
                        final_hp = creature["hp"]
                result = {
                    "policy" : policy , "seed" : seed , "victory" : state["victory"] ,
                    "terminated" : state["terminated"] , "truncated" : env.truncated ,
                    "final_hp" : final_hp , "steps" : len(actions) , "actions" : actions ,
                    "early_end" : early_end , "excess_defend" : excess_defend
                }
            results.append(result)
            print("policy:" , policy , "seed:" , seed , "victory:" , result["victory"] ,
                  "hp:" , final_hp , "steps:" , len(actions) , flush = True)

for policy in policies:
    policy_results = []
    for result in results:
        if result["policy"] == policy:
            policy_results.append(result)
    wins = sum(result["victory"] for result in policy_results)
    summary[policy] = {
        "episodes" : len(policy_results) , "wins" : wins ,
        "win_rate" : wins / len(policy_results) ,
        "mean_hp" : sum(result["final_hp"] for result in policy_results) / len(policy_results) ,
        "mean_steps" : sum(result["steps"] for result in policy_results) / len(policy_results) ,
        "truncated" : sum(result["truncated"] for result in policy_results) ,
        "early_end" : sum(len(result["early_end"]) for result in policy_results) ,
        "excess_defend" : sum(result["excess_defend"] for result in policy_results)
    }

with open("test log.json" , "w" , encoding = "utf-8") as f:
    json.dump({"seeds" : test_seeds , "max_steps" : max_steps ,
               "action_priority" : action_priority , "summary" : summary , "episodes" : results} ,
              f , ensure_ascii = False , indent = 2)
print(json.dumps(summary , ensure_ascii = False , indent = 2))
print("test log saved: test log.json")
