"""
path_service_patched.py - 修复版本的路径计算服务
主要修复：通过环境变量强制 NetEnv 不连接 Mininet socket

使用方法：
    export SKIP_MININET_CONNECT=1
    python3 path_service_patched.py --topo Abi --port 8889 --model ./model/...
"""

import argparse
import json
import os
import random
import socket
import sys

import numpy as np
import torch
from torch_geometric.data import Data

# 设置环境变量，告诉 NetEnv 不要连接 Mininet
os.environ['SKIP_MININET_CONNECT'] = '1'

# 确保可以导入本目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 尝试导入并打补丁
try:
    from net_env import simenv
    
    # 保存原始的 NetEnv.__init__
    _original_netenv_init = simenv.NetEnv.__init__
    
    def _patched_netenv_init(self, args):
        """打补丁的 NetEnv.__init__，跳过 socket 连接"""
        # 调用原始初始化
        try:
            # 临时保存 socket 模块
            import socket as socket_module
            original_socket = socket_module.socket
            
            # 创建一个假的 socket 类
            class FakeSocket:
                def connect(self, addr):
                    print(f"[PATCH] 跳过 socket.connect({addr})")
                    pass
                def close(self):
                    pass
                def send(self, data):
                    return len(data)
                def recv(self, size):
                    return b''
            
            # 如果设置了环境变量，替换 socket
            if os.getenv('SKIP_MININET_CONNECT') == '1':
                socket_module.socket = lambda *args, **kwargs: FakeSocket()
            
            # 调用原始初始化
            _original_netenv_init(self, args)
            
            # 恢复 socket
            socket_module.socket = original_socket
            
        except Exception as e:
            print(f"[PATCH] 警告：初始化时出现异常: {e}")
            # 恢复 socket
            import socket as socket_module
            if hasattr(socket_module, 'socket'):
                socket_module.socket = original_socket
            raise
    
    # 应用补丁
    simenv.NetEnv.__init__ = _patched_netenv_init
    print("[PATCH] ✓ NetEnv 补丁已应用")
    
except Exception as e:
    print(f"[PATCH] ✗ 无法应用补丁: {e}")
    print("[PATCH] 将尝试正常加载...")

from net_env.simenv import NetEnv, Request  # noqa: E402
from a2c_ppo_acktr.model import Policy      # noqa: E402


class DRLPathService:
    """
    路径计算服务（补丁版本）
    """

    def __init__(self, topo_name="Abi", port=8889, model_path=None):
        self.port = port
        self.topo_name = topo_name
        self.model_path = model_path

        print(f"[初始化] 拓扑: {topo_name}, 端口: {port}")

        # 固定随机种子
        random.seed(1)
        np.random.seed(1)
        torch.manual_seed(1)

        # 初始化环境
        print("[初始化] 正在创建 NetEnv...")
        args = argparse.Namespace()
        args.use_mininet = False
        args.simu_port = 5000
        
        try:
            self.env = NetEnv(args)
            print("[初始化] ✓ NetEnv 创建成功")
        except ConnectionRefusedError as e:
            print(f"[初始化] ✗ 连接失败: {e}")
            print("[初始化] 提示：请确保已应用补丁或修改了 simenv.py")
            raise

        print("[初始化] 正在加载拓扑信息...")
        (
            num_agent,
            num_node,
            observation_spaces,
            action_spaces,
            num_type,
            node_state_dim,
            agent_to_node,
            edge_indexs,
            adj_masks,
        ) = self.env.setup(topo_name)

        self.num_agent = num_agent
        self.num_node = num_node
        self.node_state_dim = node_state_dim
        self.num_type = num_type
        self.agent_to_node = agent_to_node
        self.edge_indexs = edge_indexs
        self.adj_masks = adj_masks

        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.actor_critic = None

        # 加载 DRL 模型
        if model_path:
            print(f"[模型] 正在加载模型: {model_path}")
            try:
                self.actor_critic = Policy(node_state_dim, num_node, num_type, base_kwargs={})
                model_file = os.path.join(model_path, "agent0.pth")
                if os.path.exists(model_file):
                    state_dict = torch.load(model_file, map_location="cpu")
                    self.actor_critic.load_state_dict(state_dict)
                    self.actor_critic.to(self.device)
                    self.actor_critic.eval()
                    print(f"[模型] ✓ DRL 模型已加载: {model_file}")
                else:
                    print(f"[模型] ✗ 模型文件不存在: {model_file}")
                    print("[模型] 将使用最短路径算法")
                    self.actor_critic = None
            except Exception as e:
                print(f"[模型] ✗ 加载失败: {e}")
                print("[模型] 将使用最短路径算法")
                self.actor_critic = None
        else:
            print("[模型] 未指定模型路径，将使用最短路径算法")

        print(
            f"[初始化] ✓ 完成！拓扑: {topo_name}, "
            f"节点数: {num_node}, Agent数: {num_agent}"
        )



    def _sanitize_path(self, path, src_node, dst_node):
        """
        清洗 DRL 生成路径，避免环路/断链导致的重复包和高时延。
        规则：
        1) 保证起点/终点正确
        2) 消除环（保留简单路径）
        3) 校验相邻节点是否存在物理链路
        4) 任一步失败则回退最短路径
        """
        if not path:
            return self.env.calcSHR(src_node, dst_node)

        # 强制起点
        if path[0] != src_node:
            path = [src_node] + [n for n in path if n != src_node]

        # 消环：如果再次遇到旧节点，删除中间成环片段
        simple_path = []
        pos = {}
        for node in path:
            if node in pos:
                keep = pos[node] + 1
                simple_path = simple_path[:keep]
                pos = {n: i for i, n in enumerate(simple_path)}
            else:
                pos[node] = len(simple_path)
                simple_path.append(node)

        path = simple_path

        # 强制终点：若未到达则补最短路尾段
        if path[-1] != dst_node:
            tail = self.env.calcSHR(path[-1], dst_node)
            if tail and len(tail) > 1:
                path.extend(tail[1:])

        # 最终校验：必须是有效简单路径，且相邻节点有链路
        if len(path) != len(set(path)):
            return self.env.calcSHR(src_node, dst_node)

        for u, v in zip(path[:-1], path[1:]):
            if v not in self.env._link_lists[u]:
                return self.env.calcSHR(src_node, dst_node)

        if path[0] != src_node or path[-1] != dst_node:
            return self.env.calcSHR(src_node, dst_node)

        return path

    def compute_path_with_drl(self, src_node, dst_node):
        """使用 DRL 模型计算路径"""
        if self.actor_critic is None:
            return self.env.calcSHR(src_node, dst_node)

        try:
            _tmp_req, obses = self.env.reset()
            request = Request(src_node, dst_node, 0, 100, 100, 0)
            self.env._request = request

            path = [src_node]
            curr_path = [0] * self.num_node
            curr_path[src_node] = 1

            curr_agent, initial_path = self.env.first_agent()
            if initial_path:
                path = initial_path.copy()
                for k in initial_path:
                    curr_path[k] = 1

            if dst_node in path:
                return self._sanitize_path(path, src_node, dst_node)

            agents_flag = [0] * self.num_agent
            deterministic = True

            while curr_agent is not None and agents_flag[curr_agent] != 1:
                agents_flag[curr_agent] = 1

                condition_state = (
                    torch.tensor(curr_path, dtype=torch.float32)
                    .unsqueeze(-1)
                    .to(self.device)
                )

                edge_index = (
                    torch.tensor(
                        self.edge_indexs[self.agent_to_node[curr_agent]],
                        dtype=torch.long,
                    )
                    .t()
                    .contiguous()
                    .to(self.device)
                )

                obs = (
                    torch.tensor(obses[curr_agent], dtype=torch.float32)
                    .unsqueeze(0)
                    .to(self.device)
                )

                inputs = Data(x=obs, edge_index=edge_index)

                adj_mask = torch.tensor(
                    self.adj_masks[self.agent_to_node[curr_agent]],
                    dtype=torch.float32,
                ).to(self.device)
                rtype = torch.tensor([request.rtype], dtype=torch.long).to(self.device)

                with torch.no_grad():
                    value, action, action_log_prob = self.actor_critic.act(
                        inputs,
                        condition_state.unsqueeze(0),
                        self.agent_to_node[curr_agent],
                        rtype,
                        adj_mask,
                        deterministic=deterministic,
                    )

                next_agent, path_segment = self.env.next_agent(curr_agent, action)

                if path_segment:
                    for node in path_segment:
                        if node not in path:
                            path.append(node)
                            curr_path[node] = 1

                    if dst_node in path:
                        break

                curr_agent = next_agent

            if not path:
                path = [src_node]
            if path[0] != src_node:
                path.insert(0, src_node)
            if path[-1] != dst_node:
                remaining = self.env.calcSHR(path[-1], dst_node)
                if remaining and len(remaining) > 1:
                    path.extend(remaining[1:])

            return self._sanitize_path(path, src_node, dst_node)

        except Exception as e:
            print(f"[DRL] ✗ 计算失败: {e}")
            import traceback
            traceback.print_exc()
            return self.env.calcSHR(src_node, dst_node)

    def compute_path(self, src_node, dst_node):
        """对外接口"""
        if self.actor_critic is not None:
            return self.compute_path_with_drl(src_node, dst_node)
        return self.env.calcSHR(src_node, dst_node)

    def run(self):
        """启动 TCP 服务"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(1)
        print(f"[服务] ✓ 已启动，监听端口 {self.port}")
        print("[服务] 等待控制器的路径计算请求...")

        while True:
            conn = None
            try:
                conn, addr = s.accept()
                print(f"[连接] 收到来自 {addr} 的连接")

                msg = conn.recv(4096)
                if not msg:
                    conn.close()
                    continue

                request = json.loads(msg.decode("utf-8"))

                if request.get("type") == "path_request":
                    src_node = int(request["src_node"])
                    dst_node = int(request["dst_node"])
                    request_id = request.get("request_id")

                    print(
                        f"[请求] {src_node} → {dst_node} "
                        f"(ID: {request_id})"
                    )

                    path = self.compute_path(src_node, dst_node)

                    response = {
                        "type": "path_response",
                        "status": "ok",
                        "path": path,
                        "request_id": request_id,
                    }
                    print(f"[响应] ✓ 路径: {path}")
                else:
                    response = {
                        "type": "path_response",
                        "status": "error",
                        "error": "未知的请求类型",
                        "request_id": request.get("request_id"),
                    }

                conn.send(json.dumps(response).encode("utf-8"))
                conn.close()

            except json.JSONDecodeError as e:
                print(f"[错误] JSON 解析失败: {e}")
                if conn is not None:
                    conn.close()
            except Exception as e:
                print(f"[错误] 处理请求失败: {e}")
                import traceback
                traceback.print_exc()
                if conn is not None:
                    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DRL 路径计算服务（补丁版）")
    parser.add_argument("--topo", default="Abi", help="拓扑名称")
    parser.add_argument("--port", type=int, default=8889, help="监听端口")
    parser.add_argument(
        "--model",
        default="./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2",
        help="DRL 模型路径",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("DRL 路径计算服务（补丁版）")
    print("=" * 60)

    service = DRLPathService(args.topo, args.port, args.model)
    try:
        service.run()
    except KeyboardInterrupt:
        print("\n[服务] 已停止")