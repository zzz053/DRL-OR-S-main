@echo off
REM Abi 拓扑测试快速启动脚本（Windows 版本）
REM 使用新控制器和 DRL-OR-S 训练好的模型

chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo   Abi 拓扑测试启动脚本
echo ========================================
echo.

REM 项目根目录（根据实际情况修改）
set PROJECT_ROOT=D:\DRL-OR-S-main
set CONDA_ENV=ryu_drl

REM 检查 conda 环境
echo [1/5] 检查 conda 环境...
call conda env list | findstr "%CONDA_ENV%" >nul
if errorlevel 1 (
    echo 错误: conda 环境 '%CONDA_ENV%' 不存在
    echo 请先创建环境: conda create -n %CONDA_ENV% python=3.6
    pause
    exit /b 1
)
echo ✓ conda 环境存在
echo.

REM 检查模型文件
echo [2/5] 检查模型文件...
set MODEL_DIR=%PROJECT_ROOT%\drl-or-s\model\Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2
if not exist "%MODEL_DIR%" (
    echo 错误: 模型目录不存在: %MODEL_DIR%
    pause
    exit /b 1
)
echo ✓ 模型目录存在
echo.

REM 检查拓扑文件
echo [3/5] 检查拓扑文件...
set TOPO_DIR=%PROJECT_ROOT%\topology\Abi
if not exist "%TOPO_DIR%\Topology.txt" (
    echo 错误: 拓扑文件不存在
    pause
    exit /b 1
)
echo ✓ 拓扑文件存在
echo.

REM 显示启动选项
echo [4/5] 选择启动方式:
echo.
echo 1. 启动 Mininet（需要管理员权限）
echo 2. 启动新控制器
echo 3. 启动 DRL Agent
echo 4. 显示启动命令（手动启动）
echo 5. 退出
echo.
set /p choice=请选择 (1-5): 

if "%choice%"=="1" (
    echo 启动 Mininet...
    cd /d %PROJECT_ROOT%\testbed
    python testbed.py Abi
) else if "%choice%"=="2" (
    echo 启动新控制器...
    call conda activate %CONDA_ENV%
    cd /d %PROJECT_ROOT%\new
    ryu-manager controller.py
) else if "%choice%"=="3" (
    echo 启动 DRL Agent...
    call conda activate %CONDA_ENV%
    cd /d %PROJECT_ROOT%\drl-or-s
    python main.py --mode test --use-gae --num-mini-batch 1 --use-linear-lr-decay --num-env-steps 50000 --env-name Abi --log-dir ./log/test --model-save-path ./model/test --model-load-path ./model/Abi-heavyload-gcn-sharepolicy-SPRsafe-mininet-2penalty-test-Abi-dynamic-extra2 --num-pretrain-epochs 0 --use-mininet
) else if "%choice%"=="4" (
    echo.
    echo 注意: 需要手动在 3 个终端中分别运行以下命令:
    echo.
    echo 终端 1 (Mininet, 需要管理员权限):
    echo cd %PROJECT_ROOT%\testbed ^&^& python testbed.py Abi
    echo.
    echo 终端 2 (控制器):
    echo conda activate %CONDA_ENV% ^&^& cd %PROJECT_ROOT%\new ^&^& ryu-manager controller.py
    echo.
    echo 终端 3 (DRL Agent):
    echo conda activate %CONDA_ENV% ^&^& cd %PROJECT_ROOT%\drl-or-s ^&^& run.sh
    echo.
    echo 按顺序启动: 1 → 2 → 3
    echo.
    pause
) else if "%choice%"=="5" (
    echo 退出
    exit /b 0
) else (
    echo 无效选择
    pause
    exit /b 1
)

pause
