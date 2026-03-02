# Abi 拓扑测试步骤（新控制器 + DRL-OR-S 模型）

## 📋 测试环境准备

### 前置条件检查

1. **确认模型文件存在**
   ```bash
   ls -la drl-or-s/model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2/
   ```
   应该看到 11 个模型文件：`agent0.pth` 到 `agent10.pth`

2. **确认拓扑文件存在**
   ```bash
   ls -la topology/Abi/
   ```
   应该看到：`Topology.txt`, `TM.txt`, `link_weight.json`

3. **确认 Python 环境**
   ```bash
   # 检查 conda 环境
   conda env list
   
   # 激活 ryu_drl 环境（用于运行 Ryu 和 DRL）
   conda activate ryu_drl_s
   
   # 验证依赖
   python3 -c "import torch; import ryu; print('OK')"
   ```

---

## 🚀 测试步骤（按顺序执行）

### 步骤 1：启动 Mininet 测试床

**终端 1**：
```bash
# 进入 testbed 目录
cd /d/DRL-OR-S-main/testbed

# 使用 sudo 启动 Mininet（需要 root 权限）
sudo python3 testbed.py Abi
```

**预期输出**：
```
testbed initializing ...
topoinfo loading finished.
*** Creating network
*** Adding controller
*** Adding hosts:
 h1 h2 h3 h4 h5 h6 h7 h8 h9 h10 h11
*** Adding switches:
 s1 s2 s3 s4 s5 s6 s7 s8 s9 s10 s11
*** Adding links:
...
*** Starting network
*** Configuring hosts
*** Starting controller
*** Starting switches
...
waiting to simenv
Connection address: ('127.0.0.1', xxxxx)
```

**关键检查点**：
- ✅ 看到 "waiting to simenv"（等待 DRL Agent 连接）
- ✅ 看到 "Connection address"（DRL Agent 已连接）
- ✅ 11 个交换机（s1-s11）和 11 个主机（h1-h11）都已创建

**保持此终端运行！**

---

### 步骤 2：启动新控制器

**终端 2**（新开一个终端）：
```bash
# 激活 conda 环境
conda activate ryu_drl

# 进入 new 目录
cd /d/DRL-OR-S-main/new

# 启动新控制器
ryu-manager controller.py
```

**预期输出**：
```
loading app controller
instantiating app controller of TopoAwareness
============================================================
等待 DRL Agent 连接 (端口 8888)...
============================================================
Register datapath: 0000000000000001, the ip address is ...
Register datapath: 0000000000000002, the ip address is ...
...
Register datapath: 000000000000000b, the ip address is ...
✓ 拓扑就绪 (11 个交换机)，开始接收 DRL 路径
```

**关键检查点**：
- ✅ 看到 "等待 DRL Agent 连接 (端口 8888)"
- ✅ 看到 11 个交换机注册（dpid 1-11）
- ✅ 看到 "拓扑就绪 (11 个交换机)"

**保持此终端运行！**

---

### 步骤 3：启动 DRL Agent

**终端 3**（新开一个终端）：
```bash
# 激活 conda 环境
conda activate ryu_drl

# 进入 drl-or-s 目录
cd /d/DRL-OR-S-main/drl-or-s

# 启动 DRL Agent（测试模式）
python3 main.py --mode test --use-gae --num-mini-batch 1 --use-linear-lr-decay --num-env-steps 50000 --env-name Abi --log-dir ./log/test --model-save-path ./model/test --model-load-path ./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2 --num-pretrain-epochs 0 --use-mininet
```

**或者使用 run.sh**：
```bash
# 确保 run.sh 中的测试命令正确
cat run.sh
# 应该看到：--use-mininet 参数

# 执行
./run.sh
```

**预期输出**：
```
✓ 已连接到控制器 DRL 接口 (端口 8888)
✓ 已连接到 Mininet 测试床 (端口 5000)
loading topoinfo finished
...
开始测试循环...
```

**关键检查点**：
- ✅ 看到 "✓ 已连接到控制器 DRL 接口 (端口 8888)"
- ✅ 看到 "✓ 已连接到 Mininet 测试床 (端口 5000)"
- ✅ 看到 "loading topoinfo finished"
- ✅ 开始输出测试日志（delay, throughput, loss 等）

**保持此终端运行！**

---

## 🔍 验证测试是否正常工作

### 1. 检查控制器日志（终端 2）

**应该看到**：
```
✓ DRL Agent 已连接: ('127.0.0.1', xxxxx)
→ 收到 DRL 路径: path=[0, 3, 5], 10.0.0.1:10001 -> 10.0.0.6:10002
【DRL流表】开始安装: 路径=[1, 4, 6], 10.0.0.1:10001 -> 10.0.0.6:10002, 协议=17
【流表】多交换机流表安装完成: 源IP=10.0.0.1, 目标IP=10.0.0.6, 路径=[1, 4, 6]
```

**如果看到错误**：
- ❌ "无法找到源主机端口" → 检查主机学习是否正常
- ❌ "交换机不存在" → 检查 dpid 映射（应该是 node_id + 1）
- ❌ "无法找到输出端口" → 检查链路发现是否正常

---

### 2. 检查流表安装（在 Mininet CLI 中）

**在终端 1 的 Mininet CLI 中**：
```bash
# 进入 Mininet CLI（如果还没有）
mininet> 

# 查看交换机 s1 的流表
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

**应该看到**：
```
priority=10, nw_src=10.0.0.1, nw_dst=10.0.0.6, udp_src=10001, udp_dst=10002, actions=output:2
priority=10, nw_src=10.0.0.6, nw_dst=10.0.0.1, udp_src=10002, udp_dst=10001, actions=output:1
priority=1, nw_src=10.0.0.1, nw_dst=10.0.0.6, actions=...
```

**关键检查点**：
- ✅ 看到 `priority=10` 的流表（DRL 流表）
- ✅ 看到五元组匹配（`udp_src`, `udp_dst`）
- ✅ 看到双向流表（正向和反向）

---

### 3. 检查 DRL Agent 日志（终端 3）

**应该看到**：
```
step: 0, delay: 12.3, throughput: 9.8, loss: 0.01
step: 1, delay: 11.5, throughput: 10.2, loss: 0.00
...
```

**关键检查点**：
- ✅ 延迟值合理（通常 < 50ms）
- ✅ 吞吐量 > 0
- ✅ 丢包率 < 0.1

---

### 4. 测试双向通信（在 Mininet CLI 中）

**在终端 1 的 Mininet CLI 中**：
```bash
# 测试 h1 到 h6 的通信
mininet> h1 ping h6 -c 5
```

**应该看到**：
```
PING 10.0.0.6 (10.0.0.6) 56(84) bytes of data.
64 bytes from 10.0.0.6: icmp_seq=1 ttl=64 time=12.345 ms
64 bytes from 10.0.0.6: icmp_seq=2 ttl=64 time=11.234 ms
...
5 packets transmitted, 5 received, 0% packet loss
```

**关键检查点**：
- ✅ ping 成功（说明双向流表都安装了）
- ✅ 延迟合理（< 50ms）

---

## 📊 查看测试结果

### 1. 查看 DRL 日志文件

**在终端 3 中**（Ctrl+C 停止 DRL Agent 后）：
```bash
# 查看延迟日志
cat drl-or-s/log/test/delay_type0.log | head -20

# 查看吞吐量日志
cat drl-or-s/log/test/throughput_type0.log | head -20

# 查看全局奖励
cat drl-or-s/log/test/globalrwd.log | head -20
```

---

### 2. 统计性能指标

```bash
# 计算平均延迟
awk '{sum+=$1; count++} END {print "平均延迟:", sum/count, "ms"}' drl-or-s/log/test/delay_type0.log

# 计算平均吞吐量达成率
awk '{sum+=$1; count++} END {print "平均吞吐量达成率:", sum/count}' drl-or-s/log/test/throughput_type0.log

# 计算平均丢包率
awk '{sum+=$1; count++} END {print "平均丢包率:", sum/count}' drl-or-s/log/test/loss_type0.log
```

---

## ⚠️ 常见问题排查

### 问题 1：DRL Agent 无法连接控制器

**症状**：
```
Connection refused: 127.0.0.1:8888
```

**解决方法**：
1. 检查控制器是否已启动（终端 2）
2. 检查端口 8888 是否被占用：
   ```bash
   netstat -tulpn | grep 8888
   ```
3. 检查控制器日志，确认监听已启动

---

### 问题 2：控制器无法找到主机端口

**症状**：
```
【DRL】无法找到源主机端口: 10.0.0.1，使用None
```

**解决方法**：
1. 等待主机学习完成（通常需要 3-5 秒）
2. 检查 ARP 表：
   ```bash
   # 在 Mininet CLI 中
   mininet> h1 ping h2 -c 1
   ```
3. 检查控制器日志中的主机学习信息

---

### 问题 3：流表未安装

**症状**：
```
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
# 没有看到 priority=10 的流表
```

**解决方法**：
1. 检查控制器日志，确认收到路径
2. 检查路径安装日志：
   ```
   【DRL流表】开始安装: ...
   【流表】多交换机流表安装完成: ...
   ```
3. 检查交换机 dpid 是否正确（应该是 1-11）

---

### 问题 4：Mininet 无法连接控制器

**症状**：
```
Unable to contact the remote controller at 127.0.0.1:5001
```

**解决方法**：
1. 确认控制器已启动（终端 2）
2. 检查 OpenFlow 端口（默认 5001）：
   ```bash
   netstat -tulpn | grep 5001
   ```
3. 检查控制器日志，确认交换机连接

---

## 🛑 停止测试

### 正常停止顺序

1. **停止 DRL Agent**（终端 3）：
   ```bash
   Ctrl+C
   ```

2. **停止控制器**（终端 2）：
   ```bash
   Ctrl+C
   ```

3. **停止 Mininet**（终端 1）：
   ```bash
   mininet> exit
   # 或者
   Ctrl+D
   ```

---

## 📝 测试检查清单

在开始测试前，确认以下项：

- [ ] 模型文件存在（11 个 agent*.pth 文件）
- [ ] 拓扑文件存在（Topology.txt, TM.txt）
- [ ] conda 环境已激活（ryu_drl）
- [ ] 端口 5001（OpenFlow）未被占用
- [ ] 端口 5000（Mininet-DRL）未被占用
- [ ] 端口 8888（DRL-Controller）未被占用
- [ ] 有 sudo 权限（运行 Mininet）

---

## 🎯 成功标准

测试成功的标志：

1. ✅ **连接成功**：
   - DRL Agent 连接到控制器（端口 8888）
   - DRL Agent 连接到 Mininet（端口 5000）
   - Mininet 连接到控制器（端口 5001）

2. ✅ **路径安装成功**：
   - 控制器日志显示收到路径
   - 流表成功安装（priority=10）
   - 双向流表都存在

3. ✅ **通信成功**：
   - ping 测试成功
   - DRL Agent 收到性能反馈
   - 日志文件正常生成

4. ✅ **性能合理**：
   - 延迟 < 50ms
   - 吞吐量 > 0
   - 丢包率 < 0.1

---

## 📚 下一步

测试成功后，可以：

1. **对比性能**：对比 DRL 路由 vs 原控制器路由的性能
2. **分析日志**：分析延迟、吞吐量、丢包率的变化
3. **优化参数**：调整超时时间、优先级等参数
4. **扩展测试**：测试其他拓扑（GEA, DialtelecomCz）

---

## 💡 提示

- **保持终端顺序**：Mininet → 控制器 → DRL Agent
- **查看日志**：遇到问题时，先查看各终端的日志输出
- **耐心等待**：拓扑发现和主机学习需要几秒钟
- **检查端口**：确保所有端口未被占用

祝测试顺利！🚀
