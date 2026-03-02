# test.sh 使用训练模型进行路径规划：模块全链路详解

本文只围绕 `test.sh` 这条链路，完整梳理“使用已训练模型进行路径规划”时会经过的模块、调用关系与关键端口。

---

## 1. `test.sh` 的职责与启动顺序

`test.sh` 做三件核心事：

1. 清理旧进程与 Mininet 残留。
2. 先启动 DRL 路径服务（`drl-or-s/path_service.py`，端口 `8889`）。
3. 再启动 Ryu 控制器（`new/controller.py`，OpenFlow 端口 `5001`）。
4. 最后启动 Mininet 测试床（`testbed/testbed.py`，内部监听 `5000`，并启用 CLI）。

这保证了：控制器在收到 PacketIn 时，可以即时向路径服务请求 DRL 路径。

---

## 2. 模块调用总览（只看 test.sh 使用到的部分）

```text
Mininet(h1->h2 ping/iperf)
   |
   | PacketIn
   v
new/controller.py
   |\
   | \__ get_path(use_drl=True)
   |      |
   |      \__ _get_path_from_drl()  TCP 127.0.0.1:8889
   |             |
   |             v
   |        drl-or-s/path_service.py
   |             |
   |             |-- 加载 Policy + agent0.pth
   |             |-- 调 NetEnv.setup(topo)
   |             |-- compute_path_with_drl() 逐跳决策
   |             \-- 失败回退 calcSHR(最短路)
   |
   \__ install_flow_entry() 下发流表

(可选并行通道)
drl-or-s/main.py --use-mininet
   |-- NetEnv.sim_interact()
   |-- 发路径安装到 controller:8888
   \-- 发流量请求到 testbed:5000
```

> 说明：`test.sh` 当前启动的是“路径服务 + 控制器 + Mininet”模式。`drl-or-s/main.py` 并不会被 `test.sh` 直接启动。

---

## 3. 每个模块到底做什么

### 3.1 `test.sh`

- 使用临时 wrapper 激活 conda 环境后在三个终端中分别启动：
  - `python3 path_service.py --topo Abi --port 8889 --model ...`
  - `ryu-manager --ofp-tcp-listen-port 5001 controller.py`
  - `sudo python3 testbed.py Abi --cli`
- 启动前会 `pkill` 和 `mn -c` 做环境清理。

---

### 3.2 `drl-or-s/path_service.py`（模型推理服务）

- 启动时创建 `DRLPathService`：
  - 初始化 `NetEnv`（通过补丁避免真的连接 Mininet）。
  - `env.setup(topo)` 读取拓扑状态维度。
  - 加载 `Policy` 与 `agent0.pth`。
- 对外提供 TCP 服务（`127.0.0.1:8889`）。
- 收到 `path_request` 后执行：
  1. 构造 `Request(src_node, dst_node, ...)`
  2. 逐跳调用 `actor_critic.act(...)` 选下一跳
  3. 直到到达目标或触发保护逻辑
  4. 失败时回退 `calcSHR`（最短路径）
- 返回 JSON：`{"type":"path_response","status":"ok","path":[...]}`。

---

### 3.3 `new/controller.py`（实际转发控制）

控制器里有两条 DRL 相关通道：

1. **按需请求路径（本链路核心）**
   - `get_path(..., use_drl=True)` 优先调用 `_get_path_from_drl(...)`。
   - `_get_path_from_drl` 连接 `127.0.0.1:8889`，发送 `path_request`。
   - 返回的是节点 ID（0-based），控制器会转成 dpid（1-based）。
   - 若超时/失败，回退 `nx.shortest_path`。

2. **主动接收 DRL Agent 下发路径（兼容旧流程）**
   - `_drl_path_receiver` 监听另一路（用于 DRL Agent 主动推送安装）。
   - 收到安装请求后走 `_install_drl_path`，最终复用 `install_flow_entry`。

在常规 ping/业务流中，控制器在 PacketIn 时识别源/目的主机，调用 `get_path` 计算路径并 `install_flow_entry` 安装双向流表。

---

### 3.4 `testbed/testbed.py`（Mininet执行层）

- 创建交换机+主机拓扑，控制器地址为 `127.0.0.1:5001`。
- `--cli` 下可手工执行 `pingall/h1 ping h2/iperf`。
- 该脚本也实现了一个 `5000` socket 服务用于和 `NetEnv.sim_interact` 通信（更多用于 `drl-or-s/main.py` 在线测试链路）。

---

### 3.5 `drl-or-s/net_env/simenv.py`（被 path_service 复用的环境）

- 定义 `NetEnv` 与 `Request`。
- 包含拓扑加载、状态构造、动作执行、最短路 `calcSHR` 等能力。
- 在 `path_service.py` 模式下：
  - 用补丁绕过真实 socket 连接。
  - 主要复用拓扑状态与 agent 逐跳推进逻辑。

---

### 3.6 `drl-or-s/a2c_ppo_acktr/model.py`（策略网络）

- `Policy.act(...)` 是路径推理核心接口。
- 内部 `GNNBase + MultiTypeAttentionDist`：
  - 输入：图结构特征 + 条件状态（已走过节点）+ 流类型
  - 输出：下一跳动作分布
- `path_service` 在推理时使用 `deterministic=True`，即按最大概率选下一跳。

---

## 4. 端口与协议一览（避免排障时混淆）

- `5001`：OpenFlow（Mininet 交换机 -> Ryu 控制器）
- `8889`：路径计算 RPC（controller -> path_service）
- `5000`：仿真反馈通道（NetEnv/main.py <-> testbed）
- `8888`：DRL Agent 主动下发路径到控制器的通道（兼容流程）

> 你当前“只想用训练模型算路”的最短闭环，是：`Mininet -> controller -> path_service(8889)`。

---

## 5. 你关心的“完整模块清单”

按 `test.sh` 当前路径，建议重点阅读顺序：

1. `test.sh`
2. `new/controller.py`（`get_path`、`_get_path_from_drl`、`install_flow_entry`）
3. `drl-or-s/path_service.py`
4. `drl-or-s/a2c_ppo_acktr/model.py`（`Policy.act`）
5. `drl-or-s/net_env/simenv.py`（`NetEnv` 关键方法）
6. `testbed/testbed.py`（理解 Mininet 与控制器关系）

如果只做“模型路径规划验证”，前 1~4 就足够完成闭环。

---

## 6. 完整测试步骤（可直接照做）

> 目标：验证“训练好的模型是否真的参与了路径规划”，而不是只走最短路径回退。

### Step 0：准备与自检

1. 打开仓库根目录，确认 `test.sh` 中路径配置正确：
   - `PROJECT_ROOT`
   - `CONDA_ENV`
   - `DRL_MODEL_DIR`
2. 确认模型文件存在（至少 `agent0.pth`）：

```bash
cd /你的工程目录/DRL-OR-S-main/drl-or-s
ls -lh ./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2/agent0.pth
```

3. 确认关键端口空闲（若不空闲先停进程）：

```bash
ss -ltnp | egrep ':5001|:8889|:5000|:8888' || true
```

### Step 1：一键启动

```bash
cd /你的工程目录/DRL-OR-S-main
bash test.sh
```

期望弹出三个终端：
- `PathService`
- `Ryu-Controller`
- `Mininet-Testbed`

### Step 2：检查 PathService 是否正常

在 `PathService` 窗口看是否出现以下关键信号：
- NetEnv 补丁生效（跳过 Mininet 连接）
- 模型加载成功（`agent0.pth`）
- 监听端口 `8889`

若模型加载失败，会回退最短路；此时本轮测试不算通过。

### Step 3：检查控制器是否正常

在 `Ryu-Controller` 窗口看：
- 交换机持续注册（拓扑起来后能看到多台）
- 收到路径请求日志（含 `request_id`）
- 能打印 `【DRL路径】src -> dst: [...]`

若只出现“DRL 服务未启动/超时”，说明其实在走最短路回退。

### Step 4：在 Mininet CLI 触发流量

在 `Mininet-Testbed` 窗口执行：

```bash
mininet> pingall
mininet> h1 ping -c 3 h6
mininet> h2 ping -c 3 h9
mininet> iperf h1 h6
```

每执行一次，观察控制器与 PathService 日志是否同步出现：
- 控制器发送 `path_request`
- PathService 返回 `path_response`
- 控制器安装流表

### Step 5：验证“确实用了模型”而非回退

建议至少做以下 2 项：

1. **正向证据**：控制器日志有 `【DRL路径】...`。
2. **反向证据**：临时停掉 path_service 后再次 ping，控制器应出现超时并回退最短路。

可用方式：
- 在 `PathService` 窗口 `Ctrl+C` 停掉服务；
- 再在 Mininet 执行一次 `h1 ping -c 1 h6`；
- 看控制器日志是否出现“连接失败/超时 + 最短路径”。

这能证明控制器确实在调用 DRL 服务。

### Step 6：测试结束与清理

在 Mininet CLI 输入：

```bash
mininet> exit
```

然后在宿主机清理：

```bash
sudo mn -c
pkill -f "ryu-manager.*controller.py" || true
pkill -f "path_service.py" || true
```

---

## 7. 测试时必须关注的问题（高频坑位）

### 7.1 路径与环境问题

1. `test.sh` 的 `PROJECT_ROOT` 默认是作者本地路径，通常需要你手动改。
2. conda 环境名若不一致，服务会起不来。
3. `gnome-terminal` 在纯服务器/无桌面环境不可用（需改成 tmux/screen/前台多窗口）。

### 7.2 端口与进程冲突

1. `5001/8889/5000/8888` 任一被占都会导致链路断裂。
2. 旧进程残留最常见，先 `mn -c + pkill` 再启动。

### 7.3 模型与拓扑一致性

1. `--topo Abi` 必须与模型训练拓扑一致。
2. 模型目录必须包含 `agent0.pth`，否则会静默回退最短路。

### 7.4 你看到“能 ping 通”不代表 DRL 生效

ping 通只说明有可用路径，不代表用的是 DRL。
必须结合日志确认：
- 有路径服务请求/响应；
- 有 `【DRL路径】` 打印；
- 关闭 path_service 后行为明显变化。

### 7.5 控制器双通道不要混淆

本测试主链路用的是：
- controller 主动请求 path_service（`8889`）

不是必须依赖：
- DRL Agent 主动下发路径（`8888`）

两条链路可共存，但你这次验证重点在 `8889` 请求-响应模式。

---

## 8. 建议的验收标准（Done 定义）

满足以下全部条件才算“训练模型路径规划闭环验证通过”：

1. 三组件均成功启动（PathService / Controller / Mininet）。
2. Mininet 触发业务后，控制器可稳定输出 `【DRL路径】`。
3. PathService 稳定返回 `path_response`，无连续异常。
4. 至少一次 `ping` 和一次 `iperf` 验证通过。
5. 关闭 PathService 后控制器能明显回退（可观测差异）。
