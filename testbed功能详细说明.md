# testbed 功能详细说明

## 📋 概述

`testbed` 目录包含一个**完整的 Mininet 网络测试床系统**，用于：
1. **创建真实的虚拟网络拓扑**（使用 Mininet）
2. **与 DRL Agent 交互**，接收流量请求并生成真实网络流量
3. **测量网络性能**（延迟、吞吐量、丢包率）并返回给 DRL Agent

---

## 🏗️ 架构图

```
┌─────────────────────────────────────────────────────────┐
│                    DRL Agent (simenv.py)                │
│                    端口 5000 (客户端)                     │
└───────────────────────┬─────────────────────────────────┘
                        │ TCP Socket (JSON)
                        │ 发送: 流量请求 {src, dst, demand, ...}
                        │ 接收: 性能指标 {delay, throughput, loss}
                        ↓
┌─────────────────────────────────────────────────────────┐
│                    testbed.py (主程序)                    │
│  ┌──────────────────────────────────────────────────┐   │
│  │  1. 创建 Mininet 网络拓扑                        │   │
│  │     - 11 个交换机 (s1-s11)                      │   │
│  │     - 11 个主机 (h1-h11)                        │   │
│  │     - 根据 topology/Abi/Topology.txt 配置链路    │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │  2. 连接到 Ryu 控制器 (127.0.0.1:5001)           │   │
│  │     - 使用 OpenFlow 1.3 协议                    │   │
│  │     - 交换机由控制器管理                         │   │
│  └──────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │  3. 监听端口 5000，等待 DRL Agent 连接           │   │
│  │     - 接收流量请求                               │   │
│  │     - 生成网络流量                               │   │
│  │     - 返回性能指标                               │   │
│  └──────────────────────────────────────────────────┘   │
└───────────────────────┬─────────────────────────────────┘
                        │
                        │ 在主机上执行
                        ↓
        ┌───────────────┴───────────────┐
        │                               │
┌───────▼────────┐            ┌─────────▼────────┐
│  client.py     │            │  server.py       │
│  (UDP 客户端)   │  UDP 流量  │  (UDP 服务器)     │
│  在源主机运行   │ ──────────>│  在目标主机运行   │
│  发送数据包     │            │  接收数据包       │
│  控制发送速率   │            │  计算性能指标     │
└────────────────┘            └──────────────────┘
```

---

## 📁 文件说明

### 1. `testbed.py` - 主程序

**功能**：
- ✅ **创建真实的 Mininet 网络**（不是模拟，是真实的虚拟网络）
- ✅ **连接到 Ryu 控制器**（端口 5001）
- ✅ **监听 DRL Agent 连接**（端口 5000）
- ✅ **生成网络流量**（调用 client.py 和 server.py）
- ✅ **测量并返回性能指标**

**关键代码解析**：

```python
# 1. 加载拓扑信息
nodeNum, linkSet, bandwidths, losses = load_topoinfo("Abi")
# 从 topology/Abi/Topology.txt 读取节点数、链路、带宽、丢包率

# 2. 创建 Mininet 网络
topo = CustomTopo(nodeNum, linkSet, bandwidths, losses)
net = Mininet(topo=topo, switch=OVSSwitch13, link=TCLink, controller=None)
net.addController('controller', controller=RemoteController, ip="127.0.0.1", port=5001)
net.start()
# 创建真实的虚拟网络，包括：
# - 11 个交换机 (s1-s11)，使用 OpenFlow 1.3
# - 11 个主机 (h1-h11)，IP: 10.0.0.1 - 10.0.0.11
# - 链路配置了带宽、延迟(5ms)、丢包率

# 3. 监听 DRL Agent 连接
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 5000))
s.listen(1)
conn, addr = s.accept()

# 4. 接收流量请求并生成流量
while True:
    data_js = json.loads(conn.recv(...))  # 接收请求
    delay, throughput, loss, popens = generate_request(
        net, 
        data_js['src'],      # 源主机索引 (0-10)
        data_js['src_port'], # 源端口
        data_js['dst'],      # 目标主机索引 (0-10)
        data_js['dst_port'], # 目标端口
        data_js['rtype'],    # 请求类型 (0=小流, 1=大流)
        data_js['demand'],   # 带宽需求 (Kbps)
        ...
    )
    # 返回性能指标
    ret = {'delay': delay, 'throughput': throughput, 'loss': loss}
    conn.send(json.dumps(ret).encode())
```

**是否生成真实的 Mininet 网络？**

✅ **是的！** `testbed.py` 使用 Mininet 创建了**真实的虚拟网络**：
- 真实的交换机（Open vSwitch）
- 真实的主机（Linux 网络命名空间）
- 真实的链路（带带宽、延迟、丢包率限制）
- 真实的流量（UDP 数据包在真实网络中传输）

这不是模拟，而是**真实的网络仿真环境**。

---

### 2. `client.py` - UDP 客户端

**功能**：
- ✅ 在**源主机**上运行（通过 `host.popen()` 执行）
- ✅ 发送 UDP 数据包到目标主机
- ✅ 控制发送速率（根据 `demand` 参数，单位：Kbps）

**关键代码解析**：

```python
# 参数：
# sys.argv[1]: server_addr (目标主机 IP，如 "10.0.0.2")
# sys.argv[2]: server_port (目标端口)
# sys.argv[3]: client_addr (源主机 IP，如 "10.0.0.1")
# sys.argv[4]: client_port (源端口)
# sys.argv[5]: demand (带宽需求，Kbps)
# sys.argv[6]: rtime (运行时间，秒)
# sys.argv[7]: rtype (请求类型：0=小流, 1=大流)
# sys.argv[8]: time_step (时间步，已弃用)

# 创建 UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((client_addr, client_port))

# 根据 demand 控制发送速率
while True:
    # 计算应该发送的数据量
    curr_bit = ind * BUFFER_SIZE * 8
    expected_bit = (time.time() - start_time) * demand * 1000
    
    if curr_bit < expected_bit:
        # 发送数据包
        msg = "%d;%d;" % (ind, int(time.time() * 1000))
        sock.sendto(msg.encode(), (server_addr, server_port))
        ind += 1
    
    # 控制发送间隔，以达到目标速率
    time.sleep(BUFFER_SIZE / (demand * 125) / 2)
```

**工作流程**：
1. 在源主机（如 h1）上启动
2. 绑定到源主机的 IP 和端口
3. 持续发送 UDP 数据包到目标主机
4. 根据 `demand` 参数控制发送速率（Kbps）
5. 每个数据包包含序号和时间戳

---

### 3. `server.py` - UDP 服务器

**功能**：
- ✅ 在**目标主机**上运行（通过 `host.popen()` 执行）
- ✅ 接收 UDP 数据包
- ✅ 计算性能指标：**延迟、吞吐量、丢包率**

**关键代码解析**：

```python
# 参数：
# sys.argv[1]: addr (服务器 IP，如 "10.0.0.2")
# sys.argv[2]: port (服务器端口)
# sys.argv[3]: rtime (运行时间，秒)
# sys.argv[4]: rtype (请求类型：0=小流, 1=大流)
# sys.argv[5]: time_step (时间步，已弃用)

# 创建 UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((addr, port))

# 接收数据包并计算性能指标
while True:
    data, addr = sock.recvfrom(BUFFER_SIZE)
    infos = str(data.decode()).split(';')[:-1]
    
    # 计算延迟：当前时间 - 数据包中的时间戳
    delay += int(time.time() * 1000) - int(infos[1])
    
    # 计算吞吐量：累计接收的数据量
    throughput += BUFFER_SIZE * 8
    
    # 计算丢包率：序号差 / 总序号
    if ind % CSTEP == 0:
        print("delay: %f ms throughput: %f Kbps loss_rate: %f" % (
            delay / CSTEP,                    # 平均延迟
            throughput / 1e3 / (time.time() - time_stamp),  # 吞吐量
            (int(infos[0]) - ind_stamp - CSTEP) / (int(infos[0]) - ind_stamp)  # 丢包率
        ))
```

**工作流程**：
1. 在目标主机（如 h2）上启动
2. 绑定到目标主机的 IP 和端口
3. 持续接收 UDP 数据包
4. 每接收 `CSTEP` 个数据包，计算并输出：
   - **延迟**：当前时间 - 数据包时间戳
   - **吞吐量**：接收速率（Kbps）
   - **丢包率**：丢失的数据包比例
5. 输出通过 `popen` 和 `pmonitor` 被 `testbed.py` 捕获

---

## 🔄 完整工作流程

### 步骤 1：启动 testbed

```bash
cd testbed
sudo python3 testbed.py Abi
```

**执行过程**：
1. 加载拓扑信息（`topology/Abi/Topology.txt`）
2. 创建 Mininet 网络（11 个交换机 + 11 个主机）
3. 连接到 Ryu 控制器（127.0.0.1:5001）
4. 启动网络
5. 监听端口 5000，等待 DRL Agent 连接

---

### 步骤 2：DRL Agent 连接并发送请求

**DRL Agent (simenv.py)** 连接到 testbed：
```python
# simenv.py 中
conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
conn.connect(("127.0.0.1", 5000))

# 发送流量请求
request = {
    'src': 0,        # 源主机索引 (h1)
    'src_port': 10001,
    'dst': 1,        # 目标主机索引 (h2)
    'dst_port': 10002,
    'rtype': 1,      # 请求类型 (1=大流)
    'demand': 100,   # 带宽需求 (100 Kbps)
    'rtime': 1000    # 运行时间 (已弃用)
}
conn.send(json.dumps(request).encode())
```

---

### 步骤 3：testbed 生成网络流量

**testbed.py** 接收请求后：
```python
# 在目标主机 (h2) 上启动 server.py
dst_host = net.hosts[1]  # h2
dst_host.popen("python3 server.py 10.0.0.2 10002 1000000 1 0")

# 在源主机 (h1) 上启动 client.py
src_host = net.hosts[0]  # h1
src_host.popen("python3 client.py 10.0.0.2 10002 10.0.0.1 10001 100 1000000 1 0")
```

**结果**：
- h1 开始向 h2 发送 UDP 数据包（速率：100 Kbps）
- h2 接收数据包并计算性能指标
- 数据包经过交换机，由 Ryu 控制器管理路由

---

### 步骤 4：testbed 测量性能并返回

**testbed.py** 使用 `pmonitor` 捕获 server.py 的输出：
```python
for host, line in pmonitor(popens):
    if host == dst_host:
        # 解析 server.py 的输出
        ret = line.split()
        delay = float(ret[1])      # 延迟 (ms)
        throughput = float(ret[4]) # 吞吐量 (Kbps)
        loss = float(ret[7])        # 丢包率

# 返回给 DRL Agent
ret = {'delay': delay, 'throughput': throughput, 'loss': loss}
conn.send(json.dumps(ret).encode())
```

---

### 步骤 5：DRL Agent 接收性能指标

**DRL Agent** 接收性能指标，用于：
- 更新环境状态
- 计算奖励（reward）
- 训练/测试 DRL 模型

---

## 🎯 关键问题解答

### Q1: testbed 是否生成真实的 Mininet 网络？

**A: 是的！** 

`testbed.py` 使用 Mininet 创建了**真实的虚拟网络**：
- ✅ 真实的交换机（Open vSwitch，支持 OpenFlow 1.3）
- ✅ 真实的主机（Linux 网络命名空间，独立的 IP 地址）
- ✅ 真实的链路（带带宽、延迟、丢包率限制）
- ✅ 真实的流量（UDP 数据包在真实网络中传输）

这不是模拟，而是**真实的网络仿真环境**，可以：
- 运行真实的网络应用（ping, iperf, 等）
- 测量真实的网络性能（延迟、吞吐量、丢包率）
- 与真实的 SDN 控制器（Ryu）交互

---

### Q2: client.py 和 server.py 的作用是什么？

**A: 它们是流量生成和性能测量工具。**

- **client.py**：
  - 在源主机上运行
  - 发送 UDP 数据包
  - 控制发送速率（根据 `demand` 参数）

- **server.py**：
  - 在目标主机上运行
  - 接收 UDP 数据包
  - 计算性能指标（延迟、吞吐量、丢包率）

**它们配合使用**：
1. DRL Agent 发送流量请求到 testbed
2. testbed 在源主机和目标主机上分别启动 client.py 和 server.py
3. client.py 发送数据包，server.py 接收并测量
4. testbed 捕获 server.py 的输出，返回性能指标给 DRL Agent

---

### Q3: testbed 与 DRL Agent 如何交互？

**A: 通过 TCP Socket (端口 5000) 进行 JSON 通信。**

**通信方向**：
```
DRL Agent (客户端) → testbed (服务器，端口 5000)
```

**消息格式**：

**请求**（DRL Agent → testbed）：
```json
{
    "src": 0,        // 源主机索引 (0-10)
    "src_port": 10001,
    "dst": 1,        // 目标主机索引 (0-10)
    "dst_port": 10002,
    "rtype": 1,      // 请求类型 (0=小流, 1=大流)
    "demand": 100,   // 带宽需求 (Kbps)
    "rtime": 1000    // 运行时间 (已弃用)
}
```

**响应**（testbed → DRL Agent）：
```json
{
    "delay": 5.2,        // 延迟 (ms)
    "throughput": 98.5,  // 吞吐量 (Kbps)
    "loss": 0.01         // 丢包率 (0-1)
}
```

---

### Q4: testbed 与控制器如何交互？

**A: 通过 OpenFlow 协议（端口 5001）。**

**连接方式**：
```python
net.addController('controller', 
                  controller=RemoteController, 
                  ip="127.0.0.1", 
                  port=5001)
```

**交互过程**：
1. testbed 创建交换机（Open vSwitch）
2. 交换机连接到 Ryu 控制器（127.0.0.1:5001）
3. 控制器管理交换机的流表
4. 当数据包到达交换机时，如果没有匹配的流表项，交换机发送 PacketIn 消息给控制器
5. 控制器计算路径并安装流表项
6. 后续数据包按流表项转发

---

## 📊 测试场景示例

### 场景 1：DRL Agent 训练/测试

```bash
# 终端 1: 启动控制器
cd new
ryu-manager --ofp-tcp-listen-port 5001 controller.py

# 终端 2: 启动 testbed
cd testbed
sudo python3 testbed.py Abi

# 终端 3: 启动 DRL Agent
cd drl-or-s
python3 main.py --mode test --use-mininet ...
```

**流程**：
1. DRL Agent 连接到 testbed (端口 5000)
2. DRL Agent 发送流量请求
3. testbed 生成流量（client.py + server.py）
4. 流量经过交换机，由控制器管理
5. testbed 测量性能并返回
6. DRL Agent 使用性能指标更新模型

---

### 场景 2：手动测试（使用 CLI）

```bash
# 启动 testbed 并启用 CLI
cd testbed
sudo python3 testbed.py Abi --cli
```

**在 Mininet CLI 中**：
```bash
mininet> h1 ping -c 3 h2
mininet> h1 iperf -c h2
mininet> h1 python3 client.py 10.0.0.2 10002 10.0.0.1 10001 100 1000 1 0
```

---

## 🔍 调试技巧

### 1. 检查网络是否创建成功

```bash
# 在 testbed 启动后，检查网络接口
sudo ip netns list
# 应该看到多个网络命名空间（每个主机一个）

# 检查交换机
sudo ovs-vsctl show
# 应该看到 11 个交换机
```

### 2. 检查 client.py 和 server.py 是否运行

```bash
# 在 Mininet CLI 中
mininet> h1 ps aux | grep client.py
mininet> h2 ps aux | grep server.py
```

### 3. 检查性能指标输出

在 testbed.py 的输出中查找：
```
<h2>: delay: 5.2 ms throughput: 98.5 Kbps loss_rate: 0.01
```

---

## 📝 总结

**testbed 是一个完整的网络测试床系统**：

1. ✅ **创建真实的 Mininet 网络**（11 个交换机 + 11 个主机）
2. ✅ **连接到 Ryu 控制器**（OpenFlow 1.3）
3. ✅ **与 DRL Agent 交互**（TCP Socket，端口 5000）
4. ✅ **生成真实网络流量**（client.py 发送，server.py 接收）
5. ✅ **测量网络性能**（延迟、吞吐量、丢包率）

**client.py 和 server.py 是流量生成和性能测量工具**：
- client.py：在源主机上发送 UDP 数据包
- server.py：在目标主机上接收并测量性能

它们配合 testbed.py 和 DRL Agent，构成了一个完整的 DRL 路由训练/测试系统。
