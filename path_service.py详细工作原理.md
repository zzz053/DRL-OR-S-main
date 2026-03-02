# path_service.py 详细工作原理

## 📋 概述

`path_service.py` 是一个独立的 TCP 服务器，接收来自控制器的路径计算请求，使用训练好的 DRL（深度强化学习）模型计算网络中的最优路径。

---

## 🏗️ 架构设计

```
┌──────────────┐         TCP Socket (8889)        ┌──────────────────┐
│              │ ←──────────────────────────────→ │                  │
│  控制器      │   路径计算请求 + 响应             │  path_service.py │
│              │                                   │  (DRL 服务)      │
└──────────────┘                                   └──────────────────┘
                                                           │
                                                           │ 使用
                                                           ↓
                                              ┌──────────────────────┐
                                              │  训练好的 DRL 模型   │
                                              │  (agent0.pth)        │
                                              └──────────────────────┘
```

---

## 🔧 核心组件

### 1. DRLPathService 类

主要的服务类，包含三个核心方法：
- `__init__()`: 初始化环境、加载模型
- `compute_path_with_drl()`: 使用 DRL 模型计算路径
- `run()`: 运行 TCP 服务器，接收请求

---

## 📝 详细工作流程

### 阶段 1：初始化（`__init__`）

#### 1.1 设置随机种子
```python
random.seed(1)
np.random.seed(1)
torch.manual_seed(1)
```
**目的**：确保结果可复现，每次运行得到相同的路径（在相同网络状态下）

---

#### 1.2 初始化网络环境
```python
args = argparse.Namespace()
args.use_mininet = False  # 不连接 Mininet
args.simu_port = 5000
self.env = NetEnv(args)
num_agent, num_node, ... = self.env.setup(topo_name)
```

**作用**：
- 创建 `NetEnv` 对象（网络环境模拟器）
- 加载拓扑信息（Abi 拓扑：11 个节点，14 条链路）
- 初始化网络状态（链路容量、利用率、丢包率等）
- 获取关键参数：
  - `num_agent`: Agent 数量（11，每个节点一个）
  - `num_node`: 节点数量（11）
  - `node_state_dim`: 节点状态维度（16）
  - `agent_to_node`: Agent 到节点的映射
  - `edge_indexs`: 图的边索引（用于 GNN）
  - `adj_masks`: 邻接矩阵掩码

**关键数据结构**：
```python
agent_to_node = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]  # Agent i 部署在节点 i
edge_indexs[0] = [[0,1], [1,0], [0,2], [2,0], ...]  # 节点 0 的邻居边
adj_masks[0] = [0, 1, 1, 0, 1, ...]  # 节点 0 的邻居掩码（1=可达，0=不可达）
```

---

#### 1.3 加载 DRL 模型（参数共享策略）

```python
self.actor_critic = Policy(node_state_dim, num_node, num_type, base_kwargs={})
self.actor_critic.load_state_dict(torch.load("agent0.pth", map_location='cpu'))
self.actor_critic.to(self.device)
self.actor_critic.eval()
```

**关键点：参数共享（Parameter Sharing）**

虽然网络中有 **11 个 Agent**（每个节点一个），但它们**共享同一个神经网络模型**！

**工作原理**：
- ✅ **只加载一个模型文件**：`agent0.pth`
- ✅ **所有 Agent 使用相同的网络参数**
- ✅ **不同 Agent 的区别在于输入数据**：
  - 观察向量不同（`obses[agent]`）：每个 Agent 看到不同的网络状态
  - 图结构不同（`edge_indexs[agent_to_node[agent]]`）：每个 Agent 的邻居不同
  - 邻接掩码不同（`adj_masks[agent_to_node[agent]]`）：每个 Agent 的可达节点不同

**为什么这样设计？**

1. **减少参数数量**：11 个 Agent 只需要 1 个模型，而不是 11 个
2. **提高训练效率**：所有 Agent 的经验可以共享，加速学习
3. **更好的泛化**：模型学习通用的路由策略，而不是节点特定的策略
4. **模型名称体现**：`sharepolicy` 表示共享策略

**参考代码**（`main.py:80-88`）：
```python
# for parameter sharing
actor_critic = Policy(...)  # 只创建一个模型
actor_critic.load_state_dict(torch.load("agent0.pth", ...))  # 只加载 agent0.pth

for i in range(num_agent):  # 11 个 Agent
    actor_critics.append(actor_critic)  # 所有 Agent 共享同一个模型对象！
```

**模型结构**：
- **输入**：网络状态观察（observation）、图结构（edge_index）、条件状态（当前路径）
- **输出**：下一个跳的选择（action）
- **架构**：GNN（图神经网络）+ Actor-Critic
- **共享方式**：所有 Agent 使用相同的网络权重，但输入数据不同

---

### 阶段 2：路径计算（`compute_path_with_drl`）

这是核心算法，逐步计算从源节点到目标节点的路径。

#### 2.1 创建请求对象
```python
request = Request(src_node, dst_node, 0, 100, 100, 0)
self.env._request = request
```

**参数说明**：
- `src_node`: 源节点 ID（0-based，如 0）
- `dst_node`: 目标节点 ID（0-based，如 5）
- `0`: 开始时间
- `100`: 结束时间
- `100`: 流量需求（带宽，单位：Kbps）
- `0`: 请求类型（0=延迟敏感，1=带宽敏感，2=可靠性敏感，3=混合）

---

#### 2.2 更新环境状态
```python
self.env._update_state()
```

**作用**：计算当前网络的完整状态，包括：

1. **链路利用率** (`_link_usage[i][j]`)
   - 每条链路上已使用的带宽
   - 用于判断链路是否拥塞

2. **最短路径距离** (`_SHR_dist[i][j]`)
   - 从节点 i 到目标节点的最短跳数
   - 用于启发式路由

3. **可用容量** (`_SHR_availcapa[i][j]`)
   - 从节点 i 到目标节点的路径上的最小可用容量
   - 用于带宽约束路径计算

4. **链路丢包率** (`_SHR_linkloss[i][j]`)
   - 从节点 i 到目标节点的路径上的累积丢包率
   - 用于可靠性计算

5. **观察向量** (`obses[agent]`)
   - 每个 Agent 的观察（网络状态的编码）
   - 包含：源/目标 one-hot、邻居距离、链路利用率、丢包率等

---

#### 2.3 获取第一个 Agent
```python
curr_agent, initial_path = self.env.first_agent()
```

**工作原理**（参考 `simenv.py:384-399`）：

```
从源节点开始，沿着最短路径前进，直到遇到第一个部署了 Agent 的节点

示例：
  源节点: 0
  目标节点: 5
  节点 0 有 Agent → 返回 agent=0, path=[0]
  
  如果节点 0 没有 Agent：
    找到节点 0 的邻居中，到目标节点 5 距离最短的节点（比如节点 2）
    继续前进，直到找到有 Agent 的节点
```

**返回值**：
- `curr_agent`: 第一个 Agent 的索引（如 0），如果源节点就是目标则返回 `None`
- `initial_path`: 从源节点到第一个 Agent 节点的路径（如 `[0]` 或 `[0, 2]`）

---

#### 2.4 DRL 模型路径计算循环

这是最核心的部分，使用 DRL 模型逐步选择下一个跳。

```python
while curr_agent is not None and agents_flag[curr_agent] != 1:
    # 1. 标记当前 Agent 已访问
    agents_flag[curr_agent] = 1
    
    # 2. 构建模型输入
    condition_state = torch.tensor(curr_path, ...)  # 当前路径的 one-hot 编码
    edge_index = torch.tensor(edge_indexs[...], ...)  # 图的边索引
    obs = torch.tensor(obses[curr_agent], ...)       # 当前 Agent 的观察
    inputs = Data(x=obs, edge_index=edge_index)      # GNN 输入
    adj_mask = torch.tensor(adj_masks[...], ...)     # 邻居掩码
    
    # 3. 使用模型计算 action
    value, action, _ = self.actor_critic.act(
        inputs, condition_state, node_index, rtype, adj_mask, 
        deterministic=True  # 测试模式：选择概率最大的 action
    )
    
    # 4. 获取下一个 Agent 和路径段
    next_agent, path_segment = self.env.next_agent(curr_agent, action)
    
    # 5. 更新路径
    path.extend(path_segment)
    
    # 6. 检查是否到达目标
    if dst_node in path:
        break
    
    curr_agent = next_agent
```

---

#### 2.5 模型推理详解（`actor_critic.act`）

**输入参数**：

1. **`inputs`** (`Data` 对象)
   - `x`: 节点特征矩阵 `[1, num_node, node_state_dim]`
     - 包含：源/目标 one-hot、邻居距离、链路利用率、丢包率等
   - `edge_index`: 图的边索引 `[2, num_edges]`
     - 用于 GNN 的消息传递

2. **`condition_state`** `[1, num_node, 1]`
   - 当前路径的 one-hot 编码
   - `curr_path[i] = 1` 表示节点 i 在路径上

3. **`node_index`** (标量)
   - 当前 Agent 所在的节点 ID

4. **`type_index`** `[1]`
   - 请求类型（0/1/2/3）

5. **`adj_mask`** `[num_node]`
   - 邻居掩码，`1` 表示可达，`0` 表示不可达

**模型处理流程**：

```
1. GNN Base 处理：
   inputs (图数据) → GNN → 节点嵌入 [num_node, hidden_dim]

2. Actor 网络：
   节点嵌入 + condition_state → Actor → 动作概率分布

3. 应用掩码：
   动作概率 × adj_mask → 过滤不可达节点

4. 选择动作：
   deterministic=True: 选择概率最大的节点
   deterministic=False: 按概率采样

5. 返回：
   value: 状态价值（用于训练，这里不使用）
   action: 选择的下一跳节点 ID
   action_log_prob: 动作的对数概率（用于训练）
```

**示例**：
```
当前节点: 0
邻居: [1, 2, 3]
模型输出概率: [0.1, 0.7, 0.2]  (对应节点 1, 2, 3)
deterministic=True → 选择节点 2 (概率最大)
action = 2
```

---

#### 2.6 获取下一个 Agent（`env.next_agent`）

**工作原理**（参考 `simenv.py:357-376`）：

```
1. 从当前 Agent 的节点开始
2. 根据 action 选择下一个节点（直接跳转）
3. 如果下一个节点有 Agent：
   → 返回该 Agent 和路径段
4. 如果下一个节点没有 Agent：
   → 沿着最短路径继续前进，直到找到有 Agent 的节点
   → 返回该 Agent 和完整路径段
```

**示例**：
```
当前 Agent: 0 (节点 0)
action: 2 (选择节点 2)
节点 2 有 Agent → 返回 agent=2, path_segment=[2]

如果节点 2 没有 Agent：
  找到节点 2 到目标的最短路径上的下一个节点（比如节点 4）
  节点 4 有 Agent → 返回 agent=4, path_segment=[2, 4]
```

---

#### 2.7 路径更新和终止条件

```python
if path_segment:
    for node in path_segment:
        if node not in path:
            path.append(node)
            curr_path[node] = 1  # 更新路径标记
    
    if dst_node in path:
        break  # 到达目标，退出循环
```

**终止条件**：
1. ✅ **到达目标节点**：`dst_node in path`
2. ✅ **没有下一个 Agent**：`curr_agent is None`
3. ✅ **循环检测**：`agents_flag[curr_agent] == 1`（已访问过）

---

#### 2.8 路径完整性检查

```python
# 确保路径包含源和目标
if path[0] != src_node:
    path.insert(0, src_node)
if path[-1] != dst_node:
    # 如果未到达目标，使用最短路径补充
    remaining_path = self.env.calcSHR(path[-1], dst_node)
    if len(remaining_path) > 1:
        path.extend(remaining_path[1:])
```

**作用**：
- 确保路径从源节点开始
- 如果 DRL 模型未能到达目标，使用最短路径算法补充剩余部分

---

### 阶段 3：TCP 服务器（`run`）

#### 3.1 创建 Socket 服务器
```python
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', self.port))
s.listen(1)
```

**作用**：
- 创建 TCP 服务器
- 绑定到 `127.0.0.1:8889`
- 监听来自控制器的连接

---

#### 3.2 接收和处理请求

```python
while True:
    conn, addr = s.accept()  # 接受连接
    msg = conn.recv(4096)     # 接收请求
    request = json.loads(msg.decode())  # 解析 JSON
```

**请求格式**：
```json
{
    "type": "path_request",
    "src_node": 0,
    "dst_node": 5,
    "src_dpid": 1,
    "dst_dpid": 6,
    "request_id": "uuid-string"
}
```

---

#### 3.3 计算路径并返回

```python
if request.get('type') == 'path_request':
    src_node = request['src_node']
    dst_node = request['dst_node']
    request_id = request.get('request_id')
    
    path = self.compute_path(src_node, dst_node)  # 调用路径计算
    
    response = {
        'type': 'path_response',
        'status': 'ok',
        'path': path,  # [0, 2, 3, 5]
        'request_id': request_id
    }
    
    conn.send(json.dumps(response).encode())  # 发送响应
    conn.close()
```

**响应格式**：
```json
{
    "type": "path_response",
    "status": "ok",
    "path": [0, 2, 3, 5],
    "request_id": "uuid-string"
}
```

---

## 🔄 完整工作流程示例

### 场景：计算从节点 0 到节点 5 的路径

```
1. 控制器发送请求：
   {"type": "path_request", "src_node": 0, "dst_node": 5, ...}

2. path_service 接收请求

3. 创建请求对象：
   request = Request(0, 5, 0, 100, 100, 0)

4. 更新环境状态：
   - 计算链路利用率
   - 计算最短路径距离
   - 生成观察向量

5. 获取第一个 Agent：
   first_agent() → agent=0, path=[0]

6. DRL 模型循环：
   
   迭代 1:
   - 当前 Agent: 0 (节点 0)
   - 模型输入: obs[0], edge_index[0], condition_state=[1,0,0,...]
   - 模型输出: action = 2 (选择节点 2)
   - next_agent(0, 2) → agent=2, path_segment=[2]
   - 更新路径: path = [0, 2]
   
   迭代 2:
   - 当前 Agent: 2 (节点 2)
   - 模型输入: obs[2], edge_index[2], condition_state=[1,0,1,0,...]
   - 模型输出: action = 3 (选择节点 3)
   - next_agent(2, 3) → agent=3, path_segment=[3]
   - 更新路径: path = [0, 2, 3]
   
   迭代 3:
   - 当前 Agent: 3 (节点 3)
   - 模型输入: obs[3], edge_index[3], condition_state=[1,0,1,1,0,...]
   - 模型输出: action = 5 (选择节点 5)
   - next_agent(3, 5) → agent=None, path_segment=[5]
   - 更新路径: path = [0, 2, 3, 5]
   - 检查: dst_node (5) in path → True → 退出循环

7. 返回路径：
   {"status": "ok", "path": [0, 2, 3, 5]}

8. 控制器接收路径，转换为 dpid: [1, 3, 4, 6]
   安装流表，数据包按此路径转发
```

---

## 🎯 关键设计特点

### 1. 分布式 Agent 架构（参数共享）

- **每个节点一个 Agent**：11 个节点 = 11 个 Agent
- **共享同一个模型**：所有 Agent 使用相同的神经网络参数（`agent0.pth`）
- **不同的输入数据**：每个 Agent 看到不同的网络状态和邻居信息
- **逐步路由**：每个 Agent 只负责选择下一跳
- **协作完成**：多个 Agent 协作完成端到端路径

**参数共享的优势**：
- ✅ 减少内存占用（1 个模型 vs 11 个模型）
- ✅ 加速训练（所有 Agent 经验共享）
- ✅ 更好的泛化能力（学习通用路由策略）

### 2. 图神经网络（GNN）

- **拓扑感知**：模型理解网络拓扑结构
- **消息传递**：节点之间交换信息
- **全局视野**：每个 Agent 能看到整个网络状态

### 3. 状态编码

观察向量包含：
- 源/目标节点 one-hot 编码
- 邻居节点到目标的最短距离
- 链路利用率
- 链路丢包率
- 可用容量
- 请求类型

### 4. 确定性策略

- **测试模式**：`deterministic=True`
- **选择最优**：总是选择概率最大的 action
- **可复现**：相同输入得到相同输出

---

## ⚠️ 错误处理

### 1. 模型加载失败
```python
if model_file not exists:
    self.actor_critic = None
    # 回退到最短路径算法
```

### 2. 路径计算失败
```python
try:
    # DRL 路径计算
except Exception as e:
    # 回退到最短路径
    return self.env.calcSHR(src_node, dst_node)
```

### 3. 未到达目标
```python
if path[-1] != dst_node:
    # 使用最短路径补充
    remaining_path = self.env.calcSHR(path[-1], dst_node)
    path.extend(remaining_path[1:])
```

---

## 📊 性能特点

- **计算时间**：50-100ms（取决于网络大小和硬件）
- **内存占用**：模型约 10-50MB（取决于网络大小）
- **并发处理**：当前实现是单线程，每次处理一个请求
- **扩展性**：可以改为多线程处理多个并发请求

---

## 🔍 调试技巧

### 1. 查看模型输出
```python
print(f"Action probabilities: {dist.probs}")
print(f"Selected action: {action}")
```

### 2. 查看路径计算过程
```python
print(f"Current agent: {curr_agent}, Path: {path}")
```

### 3. 检查网络状态
```python
print(f"Link usage: {self.env._link_usage}")
print(f"Observations: {obses[curr_agent]}")
```

---

## ✅ 总结

`path_service.py` 是一个**智能路径计算服务**，它：

1. ✅ **加载训练好的 DRL 模型**
2. ✅ **维护网络环境状态**
3. ✅ **使用 GNN 理解网络拓扑**
4. ✅ **逐步计算最优路径**
5. ✅ **通过 TCP Socket 提供服务**
6. ✅ **自动错误处理和回退**

这使得控制器可以为所有网络流量（ICMP、TCP、UDP）选择考虑网络状态的**最优路径**，而不仅仅是跳数最少的最短路径。🚀
