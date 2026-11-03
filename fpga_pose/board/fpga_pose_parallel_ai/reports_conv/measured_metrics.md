# 卷积版实测 · 2026-10-28

实现后（post-route）、xc7z020clg400-1，100 MHz。

|资源|使用量|占用率|
|---|---:|---:|
|LUT|8130 / 53200|15.28%|
|FF|7875 / 106400|7.4%|
|BRAM_36Kb_equivalent|91 / 140|65%|
|DSP|132 / 220|60%|

WNS 0.434 ns，TNS 0.0 ns；WHS 0.013 ns，THS 0.0 ns；所有用户时序约束满足。DRC：0 error、305 warning、8 advisory，具体内容见本目录原始报告。

DSP 共 132 个：距离核 95 个（32 个平方乘法、32 个累加及 31 个归约加法），图像核 1 个，卷积核 36 个。数量依据本次分层资源报告。

真实 FPGA 的 3 组极值/随机整数输入、每组 195 输出，与 int64 CPU 数值参考完全一致。公开样例图片经电脑助手及真实 PL 输出 33 点，最大字段差值 0.00014206，单次完整 RPC 100.36 ms；这不是八种动作准确率或整机提速结论。

参考记录：conv_hardware_validation.json、conv_knn_validation.json、reports_conv/hybrid_pipeline_validation.json、reports_conv/board_runtime_validation.json。较早的 reports 目录是本次修改前的历史报告。
