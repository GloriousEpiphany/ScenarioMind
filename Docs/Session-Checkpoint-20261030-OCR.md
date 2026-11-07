# 2026-10-30 OCR 续办检查点（执行中）

用户要求：先以可靠 OCR 补齐 150 页扫描件，保留原始 PDF 绑定并逐页核验，再处理历史可见性、长期留出样本和事件概率，推进预测模拟真实全流程。不需要券商交易资格，不接入下单；保留全部未提交改动，使用 `.tmp/advisory-venv/Scripts/python.exe`。本轮没有收到额度即将耗尽的明确信号。

## 已完成的工程与真实准备

已读路线/进度/运行手册和上一轮检查点，基线全量为 445 passed。当前未提交代码、用户原有文档均保留。扫描件原始 SHA-256 保持 `4c80edda691ef6acefbe103aa3cffb323c1f3d40166beb4b4f9f80914150bcf8`，150 页、22,711,577 字节，无文字层，PDF 没有修复/加密。原字节只读提取到 `.tmp/disclosure-ocr-1030/1224805758.original.pdf`，没有修改 PDF。

按 PDF 技能渲染并查看了原件封面/正文。新增 `src/stock_app/pdf_ocr.py`：固定 PP-OCRv5 server 三模型、200/300 DPI 双渲染、原文/图像/文字框/分数/协议哈希、逐页检查点与失败工件、完整页序检查、数字及规范化正文一致性。低分或分歧进入复核；修订必须绑定原文字行、原图及裁切像素哈希，不能靠 approved 标记通过。原 OCR 结果不覆盖，修订另存。

新增 `disclosure_ocr_binding.py` 及来源绑定分支：只有全部页及原始证据重放通过，才生成 OCR 正文工件和新覆盖；原来的采集失败与旧覆盖保持不变。CLI 新增 `ocr-disclosure-pdf` / `certify-disclosure-ocr`。这些工程能力尚不等于真实全文认证。

模型来自维护者 v3.10.0 清单，三个文件均通过发布 SHA-256 校验：

- detector：`0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad`
- recognizer：`e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a`
- classifier：`7d3c02ef6c7da8ae08b4347cc7695b2081aae68c325d64375724ecf39c99e743`

模型目录 `.tmp/ocr-models-ppocrv5-server/`，下载清单 `manifest.json`。安装固定 RapidOCR 3.10.0；CPU ONNX Runtime 1.23.2 真实探针速度过慢，已验证进程身份后停止该探针并卸载 CPU 包，正在安装同版本 `onnxruntime-gpu[cuda,cudnn]`。本机 RTX 4060 Laptop 8 GB，驱动 610.62。权重不变，CUDA 路径关闭 TF32，必须验证实际 provider 且禁用会话自动回退；不自动切换弱模型或 CPU。安装日志 `.tmp/ocr-gpu-install-1030.log`。

PyPI 原配置镜像 TLS 请求失败，已明确使用官方 `https://pypi.org/simple`，证书校验保持开启；没有改系统网络。`pip check` 报告全局既有 aiobotocore/botocore 版本冲突，与 OCR 安装无关，未擅自修改。

## 执行状态（需在续办时核对，不可当作最终结果）

截至这一检查点，GPU 依赖仍在下载，真实 150 页 OCR **尚未开始**。CPU 探针没有产出合格结果，错误/停止日志保留。`.tmp/probe_ocr_1030.py` 用正文第 3 页验证双分辨率；`.tmp/run_ocr_live_1030.py` 计划在独立副本 `.tmp/quant-ocr-1030.db` 中逐页处理。前者通过后再运行后者；不要启动重复执行器。

专项测试日志 `.tmp/pytest-ocr-integration-1030.log`：20 passed in 20.41s。均为工程夹具，不代表真实全文或市场预测验收。原 219 PDF / 218 可解析正文 / 1 扫描件的真实状态暂未改变。

## 后续顺序

1. 完成固定 GPU 运行库，核实 provider 和 FP32 配置，运行真实正文页探针；任何失败保留原原因，不换弱模型。
2. 跑完 150 页双渲染，保留所有页/失败/分歧；逐页核验并对疑点绑定裁切图像修订。不能删除难页、数字分歧或印章覆盖的正文。
3. 独立重放全文证据并生成新公告覆盖，核对旧库所有工件未变；再运行来源面板、历史重建和独立评分，真实失败也落账。
4. OCR 不解决历史原点可见性；今天下载的数据不能倒填。363 个标签也不满足 60 日期限的 50 个非重叠留出窗口；事件分支概率不得用未校准权重替代。
5. 最终用指定虚拟环境跑全量测试，更新进度与本检查点。不得声称 P0–P5、智能投顾正式发布或本轮真实预测全流程已完成。

官方资料：[RapidOCR 模型清单](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml)、[RapidOCR 使用说明](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/)、[ONNX Runtime CUDA 与 FP32 配置](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)。
