# 本地运行证据（Local Run Evidence）

生成时间：2026-10-12 14:4x ~ 16:4x（CST）｜生成方式：真实进程运行 + CDP 无头浏览器截图，非 mock。

## 演示模式与批次检查点（W6 新增，已真实验证）

**w6_batch_recovery_evidence.json + 14~17 截图**
任务 55a99fb3（2026-10-12 17:51）：6 张 Safebooru 真实动漫图 × batch_pause_ms=2000（演示模式，非 sleep 模拟——每批做真实字节统计+PNG 解码+缩略图产物）→ 第 3 批完成后真实 SIGKILL Worker → 任务正确停留 RUNNING → 新 Worker 接管（LEASE_EXPIRED+STEP_RECOVERED）→ **BATCH_REUSED×3：已完成批次零重跑**（task_batches 检查点表 V3 迁移）→ 其余批次续跑 → 6/6 SUCCEEDED。
前端三标签：输入/输出（6 张原图以原始分辨率预览，1536×1714 等真实尺寸）、批次进度（每批状态/输入名/亮度/字节数）、执行日志（run.log 原文，BATCH_REUSED 可见）。
批次级故障注入：fail_at_batch（一次性失败→步骤重试→前序批次复用）与 crash_at_batch（批次 2 真实进程崩溃→恢复后批次 1 复用）均有自动化测试（tests/test_batches.py 3 项）。

## 真实动漫素材全链路（Safebooru 真图 + 真实 GLM，已验证）

**anime_llm_evidence.json + 12_anime_llm_detail.png + 13_anime_llm_report.png + anime_llm_report_sample.html**
任务 4f23c1d6（2026-10-12 16:57）：从 Safebooru 收集 3 张真实动漫图（夜晚城市 PNG 1800×1800 / 黄昏天空 JPG 5143×6000 / 海洋 JPG），上传跑全链路 → glm-4-flash 真实分析（8986ms，279+204=483 tokens）。
关键真实性证据：**模型输出「动画城市夜景，亮度较低」配 0.11 亮度——与 parse_assets 对该 PNG 字节级实测的 0.1108 完全吻合**（模型真实消费了 prompt 中的解析统计）；三张图的描述逐一对应真实素材。
过程中真实暴露并修复一个 bug：GLM 偶尔把数值包成字符串/混中文（"4到5秒"）导致 OUTPUT_INVALID 重试耗尽——修复为「结构严格校验 + 数值宽松抽取（str/嵌套/中文混合均降级安全默认）」，修复后同素材一次通过。已知边界（如实）：JPG 仅记录字节数不做像素解析（解码器仅支持 PNG），对应场景亮度回落默认值 0.5。

## 真实 LLM 调用验证（glm-4-flash，已验证）

**llm_real_call_evidence.json + 10_llm_real_report.png + 11_llm_real_frontend.png + llm_real_report_sample.html**
任务 2104ce06（2026-10-12 16:43）：上传真实 PNG（亮/暗各一张）→ analyze_scenes 调用 GLM 真实端点 →
模型元数据：llm_source=real_model、model=glm-4-flash、耗时 6010ms、**Token 用量 257+189=446（API 真实返回）**；
模型输出真实理解了图片内容：「海滩场景，画面明亮」(0.80/3s) vs「夜晚场景，画面昏暗」(0.14/5s)；
下游 generate_clips 实际消费了 LLM 时长估值；HTML 报告与前端步骤展开均展示真实模型块；run.log 有 LLM_CALL_OK 且密钥未泄漏。
（Mock 与真实模式的区别在任何界面恒定可见：llm_source=mock 输出带 [MOCK] 前缀且无 Token 用量。）

## 真实素材与真实产物（新增）

素材不再只是文件名：控制台「新建任务」可直接上传真实文件（PNG 按字节真实解码测亮度）；管线每步产出真实可打开的产物——parse_assets 出素材总览 PNG、analyze_scenes 出镜头亮度分布图（数值来自真实素材解析）、generate_clips/compose_video 出真实 GIF 动画、generate_audio 出可播放 WAV（音高由镜头亮度映射）、export_result 出自包含 HTML 报告（内嵌全部中间产物）。全部纯 Python 标准库实现（artifacts.py：自带 PNG 编解码/GIF LZW 压缩/WAV 合成），无外部 API、无 API key。每步详情展开即见「输入与处理统计 → 输出产物（可预览/下载）」。证据：08/09 截图 + e2e 脚本输出（亮图 0.76/暗图 0.16 实测亮度传导到镜头分析）。

## 长时间运行案例（新增，最有代表性）

**docs/evidence/long_task_recovery_case.json + 07_long_task_recovery_detail.png**
任务 433a4498：6 步 × 15s（约 90s 工作量），生产默认租约 15s / 心跳 5s，总耗时 389s。

| 时刻 | 真实事件 |
|---|---|
| 15:23:41 | parse_assets RUNNING；两次采样 6s 间隔确认心跳续租生效（heartbeat_at/lease_until 前进） |
| 15:24:11 | generate_clips 执行 30s 时对 Worker 进程真实 SIGKILL |
| 15:24:26 | 租约到期（15s 宽限期不误杀——不凭单次心跳缺失判死） |
| 15:24:26→15:29:34 | 边界案例：全机无存活 Worker，任务停在 RUNNING 2/6——状态可查、已成功的 parse_assets/analyze_scenes 零重跑；恢复需要至少一个存活 Worker 进程（如实记录） |
| 15:29:34 | 重启 Worker → 立即回收：LEASE_EXPIRED×1 + STEP_RECOVERED×1；悬挂 attempt#1 补记 FAILED/WORKER_INTERRUPTED（298s 含悬挂期） |
| 15:29:34-15:30:10 | attempt#2 重执行 15s 成功 → 后续 3 步续跑 → SUCCEEDED 6/6 |

JSON 内含完整事件流（22 条）、generate_clips 双 attempt 历史、各步骤 worker/lease_version 明细。

## 运行形态


生产单域名形态：`uvicorn renderflow.api:app --port 8920`（RENDERFLOW_STATIC_DIR 托管前端 dist）+ 独立 `python -m renderflow.worker` 进程。SQLite/产物在 /tmp/rf_localrun（当次运行目录）。

## run_report.json — 8/8 检查通过

| # | 检查 | 结果 |
|---|---|---|
| 1 | SPA 首页由后端托管（单域名生产形态） | PASS |
| 2 | 正常任务 SUCCEEDED（6/6 步，progress 100%） | PASS |
| 3 | 首败重试恢复（generate_clips 2 次尝试：1 FAILED + 1 SUCCEEDED） | PASS |
| 4 | 相同输入缓存命中（CACHE_HIT ×6） | PASS |
| 5 | 运行中取消（CANCELLED + cancel_requested） | PASS |
| 6 | 真实 SIGKILL 执行中 Worker（exit=-9） | PASS |
| 7 | 杀后恢复 SUCCEEDED（attempt#1 WORKER_INTERRUPTED → attempt#2 SUCCEEDED） | PASS |
| 8 | 恢复事件链（LEASE_EXPIRED + STEP_RECOVERED 各 1） | PASS |

## 截图（2880×1800 @2x，Chrome headless 真实渲染）

| 文件 | 内容 |
|---|---|
| 01_task_list_light.png | 任务列表（亮色）：6 任务、状态徽章、进度条、分页 |
| 02_task_detail_dag_light.png | 任务详情（亮色）：6 节点 DAG 状态图 + 步骤明细 + 事件时间线 |
| 03_task_detail_dark.png | 任务详情（暗色主题） |
| 04_task_running_detail.png | 运行中任务详情：DAG 节点黄/绿状态混合（真实执行中间态） |
| 05_recovered_attempts_dark.png | SIGKILL 恢复任务的 generate_audio 尝试历史展开（含 WORKER_INTERRUPTED） |
| 06_task_list_dark.png | 任务列表（暗色） |
| 07_long_task_recovery_detail.png | 长任务案例详情页：generate_clips 双 attempt（WORKER_INTERRUPTED → SUCCEEDED）展开 |

## 日志与快照

- api.log：uvicorn 全程访问日志
- worker1_killed.log：被 SIGKILL 的 Worker（日志终止于 generate_audio 执行中）
- worker2_recovered.log：接管恢复的 Worker（可见回收与续跑）
- api_snapshot.json / task_ids.json：各验证任务 ID 与终态
- run_report.json：上表机器可读版

## 复现方式

```
cd backend && uv run pytest -q          # 26 项自动化测试
make demo-3                             # 真实杀进程恢复演示（DEMO OK 输出）
# 或完整本地生产形态（README §14 方案 B 的非容器等价形式）：
RENDERFLOW_STATIC_DIR=../frontend/dist uv run uvicorn renderflow.api:app --port 8920 &
uv run python -m renderflow.worker &
```
