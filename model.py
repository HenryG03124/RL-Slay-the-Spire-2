import numpy as np
import torch
import torch.nn as nn

class ActorNet(nn.Module):
    def __init__(self):
        super().__init__()

        self.L1 = nn.Linear(32 , 64)
        self.L2 = nn.Linear(64 , 4)
        self.relu = nn.ReLU(inplace = True)

    def forward(self , obs): #[B , 32]
        out = self.L1(obs)
        out = self.relu(out)
        out = self.L2(out) #[B , 4]

        return out

class CandidateActorNet(nn.Module):
    def __init__(self):
        super().__init__()

        self.L1 = nn.Linear(42 , 64)
        self.L2 = nn.Linear(64 , 1)
        self.relu = nn.ReLU(inplace = True)

    def forward(self , obs , action_features): #[B , 32] , [B , N , 10]
        N = action_features.shape[1] #len(candidate_actions)
        obs = obs.unsqueeze(1).expand(-1 , N , -1)
        out = torch.cat((obs , action_features) , dim = 2)

        out = self.L1(out)
        out = self.relu(out)
        out = self.L2(out)

        out = out.squeeze(-1)

        return out

class CriticNet(nn.Module):
    def __init__(self):
        super().__init__()

        self.L1 = nn.Linear(32 , 64)
        self.L2 = nn.Linear(64 , 1)
        self.relu = nn.ReLU(inplace = True)

    def forward(self , obs): #[B , 32]
        out = self.L1(obs)
        out = self.relu(out)
        out = self.L2(out) #[B , 1]

        return out
