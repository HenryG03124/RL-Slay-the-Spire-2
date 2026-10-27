import numpy as np
import torch
import torch.nn as nn
import json
from pathlib import Path

from model import ActorNet , CriticNet
from observation import get_observation
from action_space import get_actions , select_action
from environment import CombatEnv

lr = 0.0003
gamma = 1.0
max_steps = 1  # 先验证一次真实动作和参数更新。
random_seed = 0

def get_reward(state):
    if state["terminated"]:
        if state["victory"]:
            return 1
        else:
            return -1
    else:
        return 0

def get_V_target(critic , next_state , next_obs , gamma):
    model = critic
    with torch.no_grad():
        predicted = model(next_obs)
    return get_reward(next_state) + gamma * (1 - float(next_state["terminated"])) * predicted

def get_loss(critic , obs , next_state , next_obs , log_prob , gamma):
    model = critic
    predicted = model(obs)
    target = get_V_target(critic , next_state , next_obs , gamma)
    critic_loss = ((target - predicted) ** 2).mean()
    advantge = target - predicted
    advantge = advantge.detach().squeeze(-1)
    actor_loss = (-log_prob * advantge).mean()

    return critic_loss , actor_loss

torch.manual_seed(random_seed)
actor = ActorNet()
critic = CriticNet()

actor_optimizer = torch.optim.Adam(actor.parameters() , lr = lr)
critic_optimizer = torch.optim.Adam(critic.parameters() , lr = lr)

records = []
with CombatEnv(max_actions = max_steps) as env:
    state = env.reset()

    for step in range(max_steps):
        if state["terminated"]:
            break

        obs = torch.tensor(get_observation(state) , dtype = torch.float32).unsqueeze(0)
        mask , action_ids = get_actions(state)
        mask = torch.tensor(mask , dtype = torch.bool).unsqueeze(0)

        logits = actor(obs)
        action , log_prob = select_action(logits , mask)
        native_action = action_ids[action.item()]

        next_state = env.step(native_action)
        next_obs = torch.tensor(get_observation(next_state) , dtype = torch.float32).unsqueeze(0)
        critic_loss , actor_loss = get_loss(critic , obs , next_state , next_obs , log_prob , gamma)
        if not torch.isfinite(critic_loss) or not torch.isfinite(actor_loss):
            raise ValueError("Non-finite training loss")

        actor_optimizer.zero_grad()
        critic_optimizer.zero_grad()
        critic_loss.backward()
        actor_loss.backward()
        critic_optimizer.step()
        actor_optimizer.step()

        record = {
            "step": step + 1 , "action": action.item() , "action_id": native_action ,
            "reward": get_reward(next_state) ,
            "critic_loss": critic_loss.item() , "actor_loss": actor_loss.item() ,
            "terminated": next_state["terminated"] , "truncated": env.truncated ,
        }
        records.append(record)
        print(json.dumps(record , ensure_ascii = False))
        state = next_state

    result = {
        "steps": len(records) , "updates": len(records) ,
        "terminated": state["terminated"] , "truncated": env.truncated ,
        "victory": state["victory"] ,
        "lr": lr , "gamma": gamma , "random_seed": random_seed , "max_steps": max_steps ,
        "transitions": records ,
    }

result_path = env.run_dir / "training.json"
result_path.write_text(json.dumps(result , ensure_ascii = False , indent = 2) + "\n" , encoding = "utf-8")
print("result:" , result_path)
