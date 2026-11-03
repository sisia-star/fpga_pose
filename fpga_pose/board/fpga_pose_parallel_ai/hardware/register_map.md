# 图像核（v2 新增）

`image_preprocess_0`：`0x43c10000`，VLNV `pose.local:pose:pose_image_preprocess_axi:1.0`。

| 偏移 | 寄存器 | 含义 |
|---|---|---|
| 0x00 | CTRL | 写 1 启动；写 4 中止并清除；读 busy bit0、done bit1、idle bit2、fault bit3 |
| 0x04 | WIDTH | 输入宽度，偶数 2..1280 |
| 0x08 | HEIGHT | 输入高度，偶数 2..720 |
| 0x0c | ID | 0x50494d32 |
| 0x10 | INPUT_COUNT | 已接受输入像素数 |
| 0x14 | OUTPUT_COUNT | 已握手输出像素数 |
| 0x18 | LAST_CYCLES | 最后完成/中止的时钟周期数，包含流等待 |
| 0x1c | COMPLETED_COUNT | 最后输出 TLAST 握手成功的累计帧数 |
| 0x20 | ERROR | 1 尺寸，2 忙时配置，3 提前 TLAST，4 末尾缺失 TLAST，5 TKEEP，6 寄存器，7 地址未对齐 |
| 0x24 | MAX_WIDTH | 1280 |
| 0x28 | MAX_HEIGHT | 720 |

图像 DMA 控制地址为 `0x40400000`，simple 双向模式；详细格式、错误处理见 `image_register_map.json`。以下保留距离核协议，地址为 `0x43c00000`。

# RTL 硬件接口

默认 IP 为 `pose.local:pose:pose_knn_distance_axi:1.0`，实例名 `pose_distance_0`。PS 通过 AXI GP0 控制，PL 将训练样本缓存到 BRAM，再执行全部 99 维距离计算。这个版本只需要 Vivado 2022.x。

输入是 signed int16 Q4.11：`quantized = round(feature × 2048)`，可表示范围为 `[-16, 15.99951171875]`。超出范围的特征应由 PS 拒绝，避免静默截断。训练存储按 `row × 99 + feature` 排列，上限为 1024 个训练样本；每个样本包含 99 个特征。

PL 对每行输出 `uint64 sum((query_int16 − training_int16)²)`。PS 用 `sum / (99 × 2048²)` 得到 RMS 距离的平方，用于排序和与 `classRadius²` 比较；PL 不计算平方根。PS 仍负责前 5 邻居的投票和未知姿态拒绝。量化后的结果可能与原 float 模型存在小差异，尤其是在类别边界处。

| 字节偏移 | 寄存器 | 读写 | 说明 |
|---|---|---|---|
| 0x00 | CTRL | RW | 写 bit0=1 启动；读 bit0 busy、bit1 done、bit2 idle、bit3 fault |
| 0x04 | ROW_COUNT | RW | 本次计算的训练行数，启动时必须为 1～1024 |
| 0x08 | TRAIN_ADDR | RW | 训练 int16 单元索引，0～101375 |
| 0x0c | TRAIN_DATA | WO | 低 16 位写特征，成功写后 TRAIN_ADDR 自增 |
| 0x10 | QUERY_ADDR | RW | 查询特征索引，0～98 |
| 0x14 | QUERY_DATA | WO | 低 16 位写特征，成功写后 QUERY_ADDR 自增 |
| 0x18 | DIST_ADDR | RW | 输出行索引，0～1023 |
| 0x1c | DIST_LO | RO | uint64 结果的低 32 位，不增址 |
| 0x20 | DIST_HI | RO | 高 32 位；成功读握手后 DIST_ADDR 自增一次 |
| 0x24 | ID | RO | 固定 `0x504b4e31` |
| 0x28 | MAX_ROWS | RO | 固定 1024 |
| 0x2c | COMPLETED_COUNT | RO | 实际成功完成的 PL 运算次数，uint32 溢出回绕 |
| 0x30 | LAST_CYCLES | RO | 最近完成计算所用的 FCLK 周期数 |
| 0x34 | ERROR | RO | 错误代码 |
| 0x38 | DIMS | RO | 固定 99 |
| 0x3c | SCALE | RO | 固定 2048 |

先上传完整训练矩阵；每次识别上传 99 个查询特征，再写 ROW_COUNT 和 CTRL。等待 busy 清除，并同时检查 done=1、fault=0、ERROR=0、COMPLETED_COUNT 相对启动前增加一次，才能接受结果。读完一次 CTRL 不会清除 done；不处于 busy 时收到 start 请求会清除 done，有效 start 同时清除 fault 和 ERROR。

读取结果时，先写一次 `DIST_ADDR=0`，随后按行执行 `DIST_LO → DIST_HI` 并组合两个 32 位字。DIST_HI 在 `RVALID && RREADY` 时才增址，背压期间不会反复增址。最后一行读完可以把指针推进到上限；只有额外访问越界时才产生错误。训练和查询也允许最后一次合法写把指针推进到末尾，额外写会报错。

AXI 写地址和写数据可以先后到达；RTL 分别缓存，全部接收后才提交一次。写响应和读响应都会保持到握手。低 16 位数据的两个 byte strobe 分别生效；推荐 PS 使用完整 32 位写入。零 byte strobe 写入为无操作。

| ERROR | 原因 |
|---|---|
| 0 | 正常 |
| 1 | busy 时写寄存器或再次 start |
| 2 | 启动时 ROW_COUNT 无效 |
| 3 | 训练地址越界 |
| 4 | 查询地址越界 |
| 5 | 距离地址越界 |
| 6 | 无效寄存器，或对只读／只写寄存器执行相反访问 |
| 7 | AXI 字节地址没有按 4 字节对齐 |

错误返回 AXI SLVERR 并锁存 fault。busy 时的非法写不会改变模型、查询或运行参数；原计算仍可结束，但带 fault 的任务不记作成功完成，PS 必须拒绝其结果。PS 软件需要把模型上传和推理串行化。

RAM 使用同步读，不复位整个训练、查询和结果数组，以保留 BRAM 推断条件。芯片加载后不能依赖 RAM 的旧内容；PS 必须先上传数据。计算采用 READ、DIFF、SQUARE、ACC 四阶段，保守处理 signed17 差值、34 位乘方和 uint64 累加。源代码没有声称 II=1，也没有声称优于 CPU。

同目录 `register_map.json` 是协议说明；它不是 Vivado 生成的 `.hwh`，不能用来替代真实硬件元数据。
