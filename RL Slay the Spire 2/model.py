import numpy as np
import torch
import torch.nn as nn

class ActorNet(nn.Module):
    def __init__(self):
        super().__init__()

        self.L1 = nn.Linear(32 , 64)
        self.L2 = nn.Linear(64 , 4)
        self.relu = nn.ReLU(inplace = True)

    def forward(self , obs):  #[B , 32]
        out = self.L1(obs)
        out = self.relu(out)
        out = self.L2(out)

        return out

class CriticNet(nn.Module):
    def __init__(self):
        super().__init__()

        self.L1 = nn.Linear(32 , 64)
        self.L2 = nn.Linear(64 , 1)
        self.relu = nn.ReLU(inplace = True)

    def forward(self , obs):  #[B , 32]
        out = self.L1(obs)
        out = self.relu(out)
        out = self.L2(out)

        return out
