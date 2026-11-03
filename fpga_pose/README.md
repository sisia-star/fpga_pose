# 基于PYNQ-Z2的人体运动姿态识别系统

## 一、项目内容介绍
本项目是一套由PYNQ-Z2开发板、USB摄像头和电脑协同运行的运动姿态识别与训练监测系统。摄像头连接开发板，用户在电脑浏览器中打开训练界面，选择动作、部署参考模型并开始训练。
### 1.系统支持以下功能：
动作计数：深蹲、俯卧撑、仰卧起坐、开合跳和引体向上。在用户设置的时间内，根据动作阶段的完整往返统计次数。
有效保持计时：鸟狗式、高位平板支撑和跪姿平板支撑。姿态满足条件时累计有效时间，姿态失效或暂停时停止累计。
训练交互：提供三秒准备、暂停与继续、状态显示及浏览器语音播报。
AI点评：用户可启用DeepSeek，根据运动类别、次数、时间等汇总统计生成辅助反馈；点评不改变计数、计时或硬件状态。
### 2.系统分工
电脑：人体检测、MediaPipe Lite网络前缀推理、关键点解码、训练网页、语音及可选DeepSeek助手 
开发板PS端：USB摄像头采集、数据搬运、硬件调度、定点量化、分类投票和训练状态管理 
开发板PL端：2×2均值滤波与降采样、指定关键点输出卷积的36路乘加、32路姿态距离计算和稳定Top-5选择

## 二、各目录及文件说明
fpga_pose/
├── README.md                           #项目说明与复现步骤
├── src/                                #源码阅读副本
├── sim/                                #仿真、测试与验证记录
├── build/                              #构建报告
├── board/                              #完整可部署工程
├── data/                               #动作数据和模型副本
├── skill/                              #技能包
├── report/                             #设计报告
├── checksum_manifest.json       #文件大小与SHA256校验清单
└── filename_case_changes.json   #文件名大小写调整记录

## 三、如何复现工程
### 1. 准备硬件与软件
准备PYNQ-Z2开发板、PYNQ 3.0.1系统SD卡、稳定电源、USB摄像头、网线和Windows电脑。开发板环境使用Python 3.10及PYNQ、NumPy、OpenCV；硬件构建使用Vivado 2022.2和PYNQ-Z2板卡文件。电脑需要Python 3.14，并能够同时访问开发板局域网和DeepSeek所需的互联网。
本项目使用的局域网地址示例：
开发板：192.168.2.99
电脑有线网卡：192.168.2.1
子网掩码：255.255.255.0
Jupyter：http://192.168.2.99:9090/
训练网页：http://192.168.2.99:8081/
如果开发板实际地址或Jupyter端口不同，请以本机配置为准。确认摄像头连接开发板USB Host，并能通过`/dev/video0`取图。
### 2. 使用Vivado构建硬件
需要从源码重建时，先安装PYNQ-Z2板卡文件，打开空白Vivado 2022.2会话，然后在Tcl Console中按实际工程位置执行：
source {D:/fpga_pose/board/fpga_pose_parallel_ai/start_vivado_2022_2.tcl}
如果Vivado板卡仓库位置与入口脚本中的`POSE_BOARD_REPO`不同，先修改该配置。完成后检查仿真、综合、布局布线和DRC报告，使用同一次构建生成的配套`pose_distance.bit`和`pose_distance.hwh`。重建时不需要连接开发板。也可以直接使用`board/`工程内已提供的配套位流进行部署。
### 3. 上传并启动开发板工程
将`board/fpga_pose_parallel_ai/`完整上传至开发板：
/home/xilinx/jupyter_notebooks/fpga_pose_parallel_ai
在安装前确认8081端口没有其他版本的服务占用。在开发板Jupyter的Terminal中执行：
cd /home/xilinx/jupyter_notebooks/fpga_pose_parallel_ai
sudo /usr/local/share/pynq-venv/bin/python3 autostart/board_manage.py install \
  --python /usr/local/share/pynq-venv/bin/python3
检查服务并获取电脑助手所需的配对令牌：
sudo systemctl status fpga-pose-parallel-ai.service --no-pager
sudo /usr/local/share/pynq-venv/bin/python3 autostart/board_manage.py show-token
若Jupyter实际使用的PYNQ解释器路径不同，应替换为可导入PYNQ、NumPy和OpenCV的解释器。配对令牌应妥善保存，不要放入公开报告或源码。
### 4. 配置Windows电脑助手
在Windows电脑上进入完整工程目录，例如：
cd D:\fpga_pose\board\fpga_pose_parallel_ai
py -3.14 -m venv .conv-venv
.\.conv-venv\Scripts\python.exe -m pip install -r requirements-conv.txt
首次配置AI助手时，运行下面的脚本，根据提示输入自己的DeepSeek API Key和开发板配对令牌。输入内容会被隐藏：
powershell -NoProfile -ExecutionPolicy Bypass -File autostart\windows_setup.ps1
然后安装电脑卷积助手：
powershell -NoProfile -ExecutionPolicy Bypass -File autostart\windows_conv_setup.ps1
电脑卷积助手用于本版姿态识别，AI助手用于可选DeepSeek点评。若电脑已有同名计划任务，应检查任务指向的工程路径；换电脑或Windows用户时需要重新配置凭据。
### 5. 打开网页并进行训练
确保板端服务和电脑卷积助手运行，在电脑浏览器打开：
http://192.168.2.99:8081/
（1）选择需要的动作，按网页提示加载并部署参考模型。
（2）设置训练时间或目标保持时间，按需开启语音。
（3）调整机位，使该动作所需的身体关节入镜。
（4）点击开始，等待三秒准备结束后完成动作，查看次数或有效保持时间。
（5）结束当前训练后再切换动作或重新部署模型。
服务启动后，可通过`board/`工程中的`pynq_fpga_pose_parallel_ai.ipynb`检查硬件、摄像头及助手状态；不要在另一个Notebook中重复启动8081服务。
### 6. 验证与复现记录
文件检查和板端硬件验证的入口、操作与范围见`skill/checks.md`。运行独占硬件的验证程序前，应先正常停止实时服务，并确认没有其他Notebook占用硬件；验证结束后再恢复服务。保留位流、硬件描述、模型文件的哈希及测试日志，便于核对版本。
数据集样本级留出准确率、硬件整数一致性和真人运动计数结果属于不同测试，不能互相替代。`build/reports_conv/`为布局布线记录；本整理副本尚未重新完成Vivado构建、全新设备部署或真人动作测试。
本包不含API密钥、配对令牌、虚拟环境或旧版位流。
