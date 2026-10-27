import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import json

from model import ActorNet , CriticNet
from observation import get_observation
from action_space import get_actions , select_action
from game.environment import CombatEnv

lr = 0.0003
gamma = 1.0
max_steps = 100
num_episodes = 100
random_seed = 0
device = "cuda" if torch.cuda.is_available() else "mps" if torch.mps.is_available() else "cpu"

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
        predicted = model(next_obs) #[B , 1]
    return get_reward(next_state) + gamma * (1 - float(next_state["terminated"])) * predicted #[B , 1]

def get_loss(critic , obs , next_state , next_obs , log_prob , gamma):
    model = critic
    predicted = model(obs) #[B , 1]
    target = get_V_target(critic , next_state , next_obs , gamma) #[B , 1]
    critic_loss = ((target - predicted) ** 2).mean() #[]
    advantge = target - predicted #[B , 1]
    advantge = advantge.detach().squeeze(-1) #[B , 1] -> [B]
    actor_loss = (-log_prob * advantge).mean() #[]

    return critic_loss , actor_loss

torch.manual_seed(random_seed)
actor = ActorNet().to(device)
critic = CriticNet().to(device)
print("current device:" , device)

actor_optimizer = torch.optim.Adam(actor.parameters() , lr = lr)
critic_optimizer = torch.optim.Adam(critic.parameters() , lr = lr)

actor_loss_set = []
critic_loss_set = []
reward_set = []

for episode in range(num_episodes):
    with CombatEnv(max_actions = max_steps) as env:
        state = env.reset()
        records = []
        total_actor_loss = 0
        total_critic_loss = 0
        total_reward = 0

        for step in range(max_steps):
            if state["terminated"]:
                break

            obs = torch.tensor(get_observation(state) , dtype = torch.float32 , device = device).unsqueeze(0) #[32] -> [B , 32]
            mask , action_ids = get_actions(state)
            mask = torch.tensor(mask , dtype = torch.bool , device = device).unsqueeze(0) #[4] -> [B , 4]

            logits = actor(obs) #[B , 4]
            action , log_prob = select_action(logits , mask) #[B] , [B]
            native_action = action_ids[action.item()]

            next_state = env.step(native_action)
            next_obs = torch.tensor(get_observation(next_state) , dtype = torch.float32 , device = device).unsqueeze(0) #[32] -> [B , 32]
            critic_loss , actor_loss = get_loss(critic , obs , next_state , next_obs , log_prob , gamma) #[] , []

            actor_optimizer.zero_grad()
            critic_optimizer.zero_grad()
            critic_loss.backward()
            actor_loss.backward()
            critic_optimizer.step()
            actor_optimizer.step()

            record = {
                "step" : step + 1 ,
                "action" : action.item() ,
                "action_id" : native_action ,
                "reward" : get_reward(next_state) ,
                "critic_loss" : critic_loss.item() ,
                "actor_loss" : actor_loss.item() ,
                "terminated" : next_state["terminated"] ,
                "truncated" : env.truncated
            }
            records.append(record)
            total_actor_loss += record["actor_loss"]
            total_critic_loss += record["critic_loss"]
            total_reward += record["reward"]
            print(json.dumps(record , ensure_ascii = False))
            state = next_state

        avg_actor_loss = total_actor_loss / len(records)
        avg_critic_loss = total_critic_loss / len(records)
        result = {
            "steps" : len(records) ,
            "updates" : len(records) ,
            "terminated" : state["terminated"] ,
            "truncated" : env.truncated ,
            "victory" : state["victory"] ,
            "lr" : lr ,
            "gamma" : gamma ,
            "random_seed" : random_seed ,
            "max_steps" : max_steps ,
            "transitions" : records ,
            "episode" : episode + 1 ,
            "actor_loss" : avg_actor_loss ,
            "critic_loss" : avg_critic_loss ,
            "reward" : total_reward
        }

    with open(f"C:/Projects/Reinforcement Learning/RL Slay the Spire 2/train log/episode{episode + 1}training.json" , "w" , encoding = "utf-8") as f:
        json.dump(result , f , ensure_ascii = False , indent = 2)
    actor_loss_set.append(avg_actor_loss)
    critic_loss_set.append(avg_critic_loss)
    reward_set.append(total_reward)
    print(f"episode {episode + 1} saved")

weights = {"actor" : actor.state_dict() , "critic" : critic.state_dict()}
torch.save(weights , "C:/Projects/Reinforcement Learning/RL Slay the Spire 2/weights.pth")
print("\nweights saved: weights.pth")

episodes = range(1 , len(reward_set) + 1)
figure , axes = plt.subplots(1 , 3 , figsize = (18 , 5))

axes[0].plot(episodes , actor_loss_set , color = "blue" , label = "Actor Loss")
axes[0].set_xlabel("Episode")
axes[0].set_ylabel("Mean loss")
axes[0].set_title("Actor Loss Curve")
axes[0].grid(True)
axes[0].legend()

axes[1].plot(episodes , critic_loss_set , color = "orange" , label = "Critic Loss")
axes[1].set_xlabel("Episode")
axes[1].set_ylabel("Mean loss")
axes[1].set_title("Critic Loss Curve")
axes[1].grid(True)
axes[1].legend()

axes[2].plot(episodes , reward_set , color = "green" , label = "Total Reward")
axes[2].set_xlabel("Episode")
axes[2].set_ylabel("Reward")
axes[2].set_title("Episode Reward Curve")
axes[2].set_yticks([-1 , 0 , 1])
axes[2].grid(True)
axes[2].legend()

figure.tight_layout()
figure.savefig("C:/Projects/Reinforcement Learning/RL Slay the Spire 2/figure.png" , dpi = 300 , bbox_inches = "tight")
plt.close(figure)
print("figure saved: figure.png")
