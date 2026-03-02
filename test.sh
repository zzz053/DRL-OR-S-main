#!/bin/bash
# test.sh - 一键启动 Path_service + Ryu 控制器 + Abi 拓扑 (Mininet CLI 手工测试用)
# 修复版本：调整启动顺序，确保组件正确初始化

set -e

# ========================================
# 配置（按需修改）
# ========================================
# 工程根目录（在 WSL 中请改成实际路径，例如：/mnt/d/DRL-OR-S-main）
PROJECT_ROOT="$HOME/a/drl_ryu_zjh/DRL-OR-S-main"

# 使用的 conda 环境
CONDA_ENV="ryu_drl_s"

# 拓扑名称
MININET_TOPO="Abi"

# DRL 模型目录（相对 drl-or-s）
DRL_MODEL_DIR="./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2"

# ========================================
# 检测 conda 路径
# ========================================
CONDA_PATH=${CONDA_EXE:+$(dirname "$(dirname "$CONDA_EXE")")}
CONDA_PATH=${CONDA_PATH:-$(conda info --base 2>/dev/null)}
CONDA_PATH=${CONDA_PATH:-$HOME/miniconda3}

# ========================================
# Conda 激活包装脚本
# ========================================
create_wrapper() {
    local script="/tmp/activate_${CONDA_ENV}_$$.sh"
    cat > "$script" << 'EOF'
#!/bin/bash
set -e
CONDA_PATH="$1"
ENV_NAME="$2"
shift 2

# 初始化 conda
for init in "$CONDA_PATH/etc/profile.d/conda.sh" \
            "$CONDA_PATH/conda/etc/profile.d/conda.sh"; do
    [ -f "$init" ] && source "$init" && break
done

# 激活环境
conda activate "$ENV_NAME" || exit 1

# 执行命令
exec "$@"
EOF
    chmod +x "$script"
    echo "$script"
}

# ========================================
# 清理旧进程 / Mininet 残留
# ========================================
echo "清理旧进程..."
pkill -f "ryu-manager.*controller.py" 2>/dev/null || true
pkill -f "path_service.py" 2>/dev/null || true
sudo mn -c 2>/dev/null || true
sleep 1

# ========================================
# 启动各组件（修复后的顺序）
# ========================================
WRAPPER=$(create_wrapper)

echo "================================================"
echo "启动顺序："
echo "1. DRL 路径计算服务 (path_service.py)"
echo "2. Ryu 控制器 (controller.py)"
echo "3. Mininet Abi 拓扑 (testbed.py)"
echo "================================================"

# 1) 先启动 DRL 路径计算服务 (path_service.py)
#    这个服务不依赖 Mininet，可以独立运行
echo "正在启动 DRL 路径计算服务..."
gnome-terminal --title="PathService" -- bash -c "
cd '$PROJECT_ROOT/drl-or-s'
'$WRAPPER' '$CONDA_PATH' '$CONDA_ENV' \
  python3 path_service.py \
    --topo '$MININET_TOPO' \
    --port 8889 \
    --model '$DRL_MODEL_DIR'
read -p 'Path_service exited. Press Enter...'
"

# 等待 PathService 启动
sleep 5

# 2) 启动 Ryu 控制器 (new/controller.py)
echo "正在启动 Ryu 控制器..."
gnome-terminal --title="Ryu-Controller" -- bash -c "
cd '$PROJECT_ROOT/new'
'$WRAPPER' '$CONDA_PATH' '$CONDA_ENV' \
  ryu-manager --ofp-tcp-listen-port 5001 controller.py
read -p 'Ryu exited. Press Enter...'
"

# 等待 Ryu 启动
sleep 3

# 3) 最后启动 Mininet Abi 拓扑（带 CLI，手工 ping/iperf 测试）
#    注意：不使用 --cli 选项，因为这会阻塞后续的 socket 监听
echo "正在启动 Mininet 拓扑..."
gnome-terminal --title="Mininet-Testbed" -- bash -c "
cd '$PROJECT_ROOT/testbed'
sudo python3 testbed.py '$MININET_TOPO' --cli
read -p 'Mininet exited. Press Enter...'
"

echo ""
echo "================================================"
echo "所有组件已启动！"
echo "================================================"
echo "终端窗口："
echo "  - PathService: DRL 路径计算服务（端口 8889）"
echo "  - Ryu-Controller: SDN 控制器（端口 5001）"
echo "  - Mininet-Testbed: 网络拓扑（CLI 模式）"
echo ""
echo "使用说明："
echo "  1. 在 Mininet 终端中，可以使用命令测试网络："
echo "     mininet> pingall"
echo "     mininet> h1 ping h2"
echo "     mininet> iperf h1 h2"
echo "  2. 查看 Ryu 和 PathService 终端的日志输出"
echo "  3. 测试完成后，在 Mininet 终端输入 'exit' 退出"
echo "================================================"