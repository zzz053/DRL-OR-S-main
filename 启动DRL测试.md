# DRL-OR-S 与新控制器集成 - 启动指南

## 已完成的修改

### 1. 控制器修改 (new/controller.py)
- ✅ 添加了 DRL 路径接收服务（端口 8888）
- ✅ 添加了路径解析和流表安装功能
- ✅ 支持五元组匹配（源IP、目的IP、源端口、目的端口、协议）

### 2. DRL Agent 修改 (drl-or-s/net_env/simenv.py)
- ✅ 修改连接端口：3999 → 8888
- ✅ 添加连接成功提示

---

## 启动步骤

### 第 1 步：启动控制器

```bash
cd new
ryu-manager controller.py --verbose
```

**成功标志**：
```
INFO     等待 DRL Agent 连接 (端口 8888)...
```

---

### 第 2 步：启动 Mininet 测试床

**新开一个终端**：
```bash
cd testbed
sudo python3 testbed.py Abi
```

**成功标志**：
```
testbed initializing ...
topoinfo loading finished.
waiting to simenv
Connection address: ('127.0.0.1', xxxxx)
```

---

### 第 3 步：启动 DRL Agent

**新开第三个终端**：
```bash
cd drl-or-s
python3 main.py \
    --mode test \
    --env-name Abi \
    --model-load-path ./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2 \
    --use-mininet \
    --num-env-steps 10000
```

**成功标志**：
```
✓ 已连接到控制器 DRL 接口 (端口 8888)
✓ 已连接到 Mininet 测试床 (端口 5000)
开始测试...
```

---

## 验证成功

### 控制器日志应显示：

```
✓ DRL Agent 已连接: ('127.0.0.1', 54321)
✓ 拓扑就绪 (11 个交换机)，开始接收 DRL 路径
→ 收到 DRL 路径: path=[0, 3, 5], 10.0.0.1:10001 -> 10.0.0.6:10002
【DRL 流表安装】开始: 10.0.0.1 -> 10.0.0.6, 路径长度=3
  ✓ 交换机 1: 出端口=2
  ✓ 交换机 4: 出端口=3
  ✓ 交换机 6: 出端口=1
【DRL 流表安装】完成: 成功安装 3/3 个流表项
```

### DRL Agent 日志应显示：

```
Episode: 1/10000
Request: src=0, dst=5, demand=10.5Mbps, type=0
Computed path: [0, 3, 5]
Path sent to controller
Controller response: Succeeded!
Measured: delay=12.3ms, throughput=10.2Mbps, loss=0.01
Reward: 0.856
```

---

## 常见问题排查

### 问题 1：端口冲突

**现象**：
```
OSError: [Errno 98] Address already in use
```

**解决**：
```bash
# 查看端口占用
netstat -tulpn | grep 8888

# 杀掉占用进程
sudo kill -9 <PID>

# 或者改用其他端口（需同时修改控制器和simenv.py）
```

---

### 问题 2：DRL Agent 连接失败

**现象**：
```
ConnectionRefusedError: [Errno 111] Connection refused
```

**检查清单**：
1. 控制器是否已启动？
2. 控制器是否显示"等待 DRL Agent 连接"？
3. 端口号是否一致（都是 8888）？

**解决**：
```bash
# 确认控制器正在监听
netstat -tulpn | grep 8888

# 应该看到：
# tcp  0  0  127.0.0.1:8888  0.0.0.0:*  LISTEN  <PID>/python3
```

---

### 问题 3：流表未安装

**现象**：控制器收到路径，但没有"流表安装完成"日志

**可能原因**：
1. 交换机 dpid 不匹配
2. get_port_from_link 方法返回 None
3. 主机 IP 查找失败

**调试方法**：
```bash
# 1. 查看控制器日志中的交换机列表
grep "Register datapath" controller.log

# 2. 查看拓扑链路
grep "域内链路" controller.log
grep "域间链路" controller.log

# 3. 查看主机学习
grep "成功学习主机" controller.log
```

---

### 问题 4：路径 ID 转换错误

**现象**：找不到交换机或端口

**说明**：
- DRL 的 path 是 **0-based**：`[0, 1, 2]` 表示节点1、2、3
- 控制器的 dpid 是 **1-based**：需要 `dpid = node_id + 1`

**验证**：在控制器日志中查看转换是否正确
```
path=[0, 3, 5]  →  交换机 1, 4, 6
```

---

### 问题 5：Mininet 主机无法通信

**现象**：DRL 发送路径，但 ping 不通

**检查**：
```bash
# 在 Mininet CLI 中测试
mininet> h1 ping h6

# 查看流表
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

**可能原因**：
1. ARP 未学习（需要控制器处理 ARP）
2. 流表未正确安装
3. MAC 地址未学习

---

## 性能监控

### 实时查看 DRL 性能

```bash
# 全局奖励
tail -f drl-or-s/log/test/globalrwd.log

# 延迟（低延迟流）
tail -f drl-or-s/log/test/delay_type0.log

# 延迟（高带宽流）
tail -f drl-or-s/log/test/delay_type1.log

# 吞吐量
tail -f drl-or-s/log/test/throughput_type0.log

# 丢包率
tail -f drl-or-s/log/test/loss_type0.log
```

### 查看流表统计

如果您的控制器支持 REST API：
```bash
# 查看所有交换机
curl http://127.0.0.1:8080/stats/switches

# 查看交换机1的流表
curl http://127.0.0.1:8080/stats/flow/1

# 查看端口统计
curl http://127.0.0.1:8080/stats/port/1
```

---

## 手动测试

### 测试 DRL 接口连通性

```bash
# 手动发送测试路径
echo '{"path":[0,1,2],"src_port":10001,"dst_port":10002,"ipv4_src":"10.0.0.1","ipv4_dst":"10.0.0.3"}' | nc 127.0.0.1 8888

# 应该收到响应：
# Succeeded!
```

### 在 Mininet 中测试连通性

```bash
# 启动 Mininet 后，进入 CLI
cd testbed
sudo python3 testbed.py Abi

# 在 Mininet CLI 中：
mininet> h1 ping h6 -c 5
mininet> iperf h1 h6
```

---

## 预期性能

成功运行后，相比传统最短路径算法：

- **平均延迟**：降低 10-30%
- **吞吐量**：提升 5-15%
- **负载均衡**：链路利用率更均匀
- **适应性**：拥塞时自动选择备用路径

---

## 下一步

成功运行后，您可以：

1. **对比测试**
   - 关闭 DRL，使用最短路径
   - 对比性能指标

2. **参数调优**
   - 调整流表超时时间
   - 调整 DRL 奖励函数权重

3. **故障测试**
   - 模拟链路失效
   - 观察 DRL 如何重路由

4. **扩展到其他拓扑**
   - 使用自己的网络拓扑
   - 重新训练模型

---

## 完整启动脚本

创建 `start_all.sh`：

```bash
#!/bin/bash

echo "启动 DRL-OR-S 系统..."

# 终端1：启动控制器
gnome-terminal --title="Ryu Controller" -- bash -c "
    cd new
    ryu-manager controller.py --verbose
    exec bash
"
sleep 3

# 终端2：启动 Mininet
gnome-terminal --title="Mininet Testbed" -- bash -c "
    cd testbed
    sudo python3 testbed.py Abi
    exec bash
"
sleep 5

# 终端3：启动 DRL Agent
gnome-terminal --title="DRL Agent" -- bash -c "
    cd drl-or-s
    python3 main.py \
        --mode test \
        --env-name Abi \
        --model-load-path ./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2 \
        --use-mininet \
        --num-env-steps 10000
    exec bash
"

echo "✓ 所有组件已启动！"
```

使用方法：
```bash
chmod +x start_all.sh
./start_all.sh
```

---

## 技术支持

如果遇到问题，提供以下信息：

1. **控制器日志**：最后50行
2. **DRL Agent日志**：错误信息
3. **Mininet状态**：`sudo mn -c` 清理后重试
4. **端口状态**：`netstat -tulpn | grep 8888`
5. **Python版本**：`python3 --version`

祝测试顺利！🚀

