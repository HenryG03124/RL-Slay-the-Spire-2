import torch
import time

from model import ActorNet
from observation import get_observation
from action_space import get_actions , action_names
from game.visible_environment import VisibleCombatEnv

device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
max_steps = 100
step_delay = 1.0
result_delay = 10

actor = ActorNet().to(device)
weights = torch.load("C:/Projects/Reinforcement Learning/RL Slay the Spire 2/weights.pth" , map_location = device , weights_only = True)
actor.load_state_dict(weights["actor"])
actor.eval()
print("current device:" , device)

with VisibleCombatEnv(max_actions = max_steps) as env:
    state = env.reset()

    with torch.no_grad():
        for step in range(max_steps):
            if state["terminated"]:
                break

            obs = torch.tensor(get_observation(state) , dtype = torch.float32 , device = device).unsqueeze(0) #[B , 32]
            mask , action_ids = get_actions(state)
            mask = torch.tensor(mask , dtype = torch.bool , device = device).unsqueeze(0) #[B , 4]

            logits = actor(obs) #[B , 4]
            logits = logits.masked_fill(~mask , float("-inf"))
            action = logits.argmax(dim = 1).item()
            print("step:" , step + 1 , "action:" , action_names[action] , flush = True)

            time.sleep(step_delay)
            state = env.step(action_ids[action])

    if state["terminated"]:
        print("victory:" , state["victory"] , flush = True)
    else:
        print("action limit reached" , flush = True)
    time.sleep(result_delay)
