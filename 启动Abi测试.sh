#!/bin/bash

# Abi 拓扑测试快速启动脚本
# 使用新控制器和 DRL-OR-S 训练好的模型

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="/d/DRL-OR-S-main"
CONDA_ENV="ryu_drl"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Abi 拓扑测试启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# 检查 conda 环境
echo -e "${YELLOW}[1/5] 检查 conda 环境...${NC}"
if ! conda env list | grep -q "$CONDA_ENV"; then
    echo -e "${RED}错误: conda 环境 '$CONDA_ENV' 不存在${NC}"
    echo "请先创建环境: conda create -n $CONDA_ENV python=3.6"
    exit 1
fi
echo -e "${GREEN}✓ conda 环境存在${NC}"
echo ""

# 检查模型文件
echo -e "${YELLOW}[2/5] 检查模型文件...${NC}"
MODEL_DIR="$PROJECT_ROOT/drl-or-s/model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2"
if [ ! -d "$MODEL_DIR" ]; then
    echo -e "${RED}错误: 模型目录不存在: $MODEL_DIR${NC}"
    exit 1
fi

MODEL_COUNT=$(ls -1 "$MODEL_DIR"/agent*.pth 2>/dev/null | wc -l)
if [ "$MODEL_COUNT" -lt 11 ]; then
    echo -e "${RED}错误: 模型文件不完整（期望 11 个，实际 $MODEL_COUNT 个）${NC}"
    exit 1
fi
echo -e "${GREEN}✓ 模型文件完整（$MODEL_COUNT 个）${NC}"
echo ""

# 检查拓扑文件
echo -e "${YELLOW}[3/5] 检查拓扑文件...${NC}"
TOPO_DIR="$PROJECT_ROOT/topology/Abi"
if [ ! -f "$TOPO_DIR/Topology.txt" ] || [ ! -f "$TOPO_DIR/TM.txt" ]; then
    echo -e "${RED}错误: 拓扑文件不存在${NC}"
    exit 1
fi
echo -e "${GREEN}✓ 拓扑文件存在${NC}"
echo ""

# 检查端口占用
echo -e "${YELLOW}[4/5] 检查端口占用...${NC}"
check_port() {
    local port=$1
    if netstat -tuln 2>/dev/null | grep -q ":$port "; then
        echo -e "${RED}警告: 端口 $port 已被占用${NC}"
        return 1
    else
        echo -e "${GREEN}✓ 端口 $port 可用${NC}"
        return 0
    fi
}

check_port 5001 || echo "提示: 如果 Mininet 已在运行，这是正常的"
check_port 5000 || echo "提示: 如果 Mininet 已在运行，这是正常的"
check_port 8888 || echo "提示: 如果控制器已在运行，这是正常的"
echo ""

# 显示启动选项
echo -e "${YELLOW}[5/5] 选择启动方式:${NC}"
echo ""
echo "1. 启动 Mininet（终端 1）"
echo "2. 启动新控制器（终端 2）"
echo "3. 启动 DRL Agent（终端 3）"
echo "4. 启动全部（需要 3 个终端）"
echo "5. 退出"
echo ""
read -p "请选择 (1-5): " choice

case $choice in
    1)
        echo -e "${GREEN}启动 Mininet...${NC}"
        cd "$PROJECT_ROOT/testbed"
        sudo python3 testbed.py Abi
        ;;
    2)
        echo -e "${GREEN}启动新控制器...${NC}"
        cd "$PROJECT_ROOT/new"
        conda run -n $CONDA_ENV ryu-manager controller.py
        ;;
    3)
        echo -e "${GREEN}启动 DRL Agent...${NC}"
        cd "$PROJECT_ROOT/drl-or-s"
        conda run -n $CONDA_ENV python3 main.py --mode test --use-gae --num-mini-batch 1 --use-linear-lr-decay --num-env-steps 50000 --env-name Abi --log-dir ./log/test --model-save-path ./model/test --model-load-path ./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2 --num-pretrain-epochs 0 --use-mininet
        ;;
    4)
        echo -e "${GREEN}启动全部组件...${NC}"
        echo ""
        echo -e "${YELLOW}注意: 需要手动在 3 个终端中分别运行以下命令:${NC}"
        echo ""
        echo -e "${GREEN}终端 1 (Mininet):${NC}"
        echo "cd $PROJECT_ROOT/testbed && sudo python3 testbed.py Abi"
        echo ""
        echo -e "${GREEN}终端 2 (控制器):${NC}"
        echo "conda activate $CONDA_ENV && cd $PROJECT_ROOT/new && ryu-manager controller.py"
        echo ""
        echo -e "${GREEN}终端 3 (DRL Agent):${NC}"
        echo "conda activate $CONDA_ENV && cd $PROJECT_ROOT/drl-or-s && ./run.sh"
        echo ""
        echo -e "${YELLOW}按顺序启动: 1 → 2 → 3${NC}"
        ;;
    5)
        echo "退出"
        exit 0
        ;;
    *)
        echo -e "${RED}无效选择${NC}"
        exit 1
        ;;
esac
