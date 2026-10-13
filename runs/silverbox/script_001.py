import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from scipy.io import loadmat

def solve():
    # 1. Load and Preprocess Data
    data = loadmat("SNLS80mV.mat")
    u_full = data["V1"][0].astype(np.float32)
    y_full = data["V2"][0].astype(np.float32)

    # Slice [42650 : 46746]
    u_slice = u_full[42650:46746]
    y_slice = y_full[42650:46746]

    # Center signals
    u_centered = u_slice - np.mean(u_slice)
    y_centered = y_slice - np.mean(y_slice)

    # Split into training (3072) and test (1024)
    u_train = u_centered[:3072]
    y_train = y_centered[:3072]
    u_test = u_centered[3072:]
    y_test = y_centered[3072:]

    fs = 610.35
    dt = 1.0 / fs

    # 2. Define Network
    class AccelerationNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(3, 64),
                nn.Tanh(),
                nn.Linear(64, 64),
                nn.Tanh(),
                nn.Linear(64, 64),
                nn.Tanh(),
                nn.Linear(64, 1)
            )
        def forward(self, x):
            return self.net(x)

    model = AccelerationNet()
    optimizer = optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.MSELoss()

    # 3. RK4 Integration Logic
    def rk4_step(model, s, u_val, dt):
        # s = [y, v]
        def f(state, u):
            # state: [y, v]
            # input to NN: [y, v, u]
            inp = torch.cat([state, u])
            accel = model(inp)
            return torch.stack([state[1], accel.squeeze()])

        u_tensor = torch.tensor([u_val], dtype=torch.float32)
        
        k1 = f(s, u_tensor)
        k2 = f(s + 0.5 * dt * k1, u_tensor) # Simplified u handling for stability
        k3 = f(s + 0.5 * dt * k2, u_tensor)
        k4 = f(s + dt * k3, u_tensor)
        
        return s + (dt / 6.0) * (k1 + 2*k2 + 2*k3 + k4)

    # 4. Training Loop
    # To avoid vanishing gradients and long rollouts, we train on shorter segments
    # as suggested by the scientist (512 samples).
    epochs = 200 # Reduced for execution time, but enough to show convergence
    batch_size = 512
    
    model.train()
    for epoch in range(epochs):
        # We use a simple approach: optimize the network to match the acceleration 
        # derived from the data (finite difference) to bootstrap, then refine.
        # However, the plan asks to minimize difference between integrated y and measured y.
        # For efficiency in this script, we'll use a "shooting" approach on small windows.
        
        total_loss = 0
        for i in range(0, 3072, batch_size):
            # Segment
            u_seg = torch.tensor(u_train[i:i+batch_size], dtype=torch.float32)
            y_seg = torch.tensor(y_train[i:i+batch_size], dtype=torch.float32)
            
            # Initial state for segment
            s = torch.tensor([y_seg[0], (y_seg[1] - y_seg[0])/dt], requires_grad=True)
            
            pred_y = []
            curr_s = s
            for j in range(len(u_seg)):
                curr_s = rk4_step(model, curr_s, u_seg[j].item(), dt)
                pred_y.append(curr_s[0])
            
            pred_y = torch.stack(pred_y)
            loss = criterion(pred_y, y_seg)
            
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        
        if (epoch + 1) % 50 == 0:
            print(f"Epoch {epoch+1}/{epochs}, Loss: {total_loss/ (3072/batch_size):.6f}")

    # 5. Free-Run Test
    model.eval()
    with torch.no_grad():
        # Initial conditions
        y0 = torch.tensor(y_centered[0], dtype=torch.float32)
        v0 = torch.tensor((y_centered[1] - y_centered[0]) / dt, dtype=torch.float32)
        s = torch.stack([y0, v0])
        
        full_u = u_centered
        predictions = []
        
        for i in range(4096):
            s = rk4_step(model, s, full_u[i], dt)
            predictions.append(s[0].item())
        
        predictions = np.array(predictions)
        
        # Split predictions
        train_pred = predictions[:3072]
        test_pred = predictions[3072:]
        
        # Calculate RMSE
        train_rmse = np.sqrt(np.mean((train_pred - y_train)**2))
        test_rmse = np.sqrt(np.mean((test_pred - y_test)**2))
        
        print(f"Training Free-Run RMSE: {train_rmse:.6f}")
        print(f"Test Free-Run RMSE: {test_rmse:.6f}")

if __name__ == "__main__":
    solve()
