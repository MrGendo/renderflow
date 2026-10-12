# TASK4 — RenderFlow W4：前端管理控制台（React+TS+Vite）

你是资深前端工程师。工作目录 `/Users/Admin/Downloads/project/written_test`。后端已完成（W1-W2），API 运行在 http://127.0.0.1:8900（vite dev 代理到它）。先读 `docs/PLAN.md`。**只创建 frontend/ 目录内容与根级说明，不动 backend/，不 git commit。**

## 技术约束
- `npm create vite@latest frontend -- --template react-ts`（node 24 可用）；原生 CSS（CSS 变量做主题，支持亮/暗，默认亮色，右上角切换按钮）。
- 不引入 UI 组件库、不引入状态管理库；fetch 封装 + React hooks 足够。
- TypeScript strict；`npm run build` 必须通过、tsc 无错。

## 后端 API 契约（真实实现，字段名必须一字不差）
统一包装：成功 `{"ok":true,"data":...}`；错误 `{"ok":false,"error":{"code","message"}}`。时间字段是 `{iso,epoch}` 对象。
- POST /api/tasks body: `{input:{assets:[...]}, step_durations?:{"generate_clips":1200}, fault_injection?:{...}}` → data 含 task_id、steps 概要
- GET /api/tasks?page=&pageSize=&status= → data:{items,total}；item 含 task_id,status,derived_status,reason_code,progress_percent,succeeded_steps,total_steps,error_summary,created_at{iso,epoch},started_at,finished_at,duration_ms(如有,若无则前端用 epoch 计算)
- GET /api/tasks/{id} → data 含上表全部 + cancel_requested,fault_injection,workflow_version,steps:[{step_id,step_type,status,attempt_count,max_attempts,error_code}]
- GET /api/tasks/{id}/steps → data 为数组：step_id,task_id,step_type,status,dependencies[](step_type 数组),input_hash,step_version,config_hash,output_ref,output_checksum,attempt_count,max_attempts,next_retry_at{iso,epoch},worker_id,heartbeat_at,lease_until,lease_version,started_at,finished_at,error_code,error_message
- GET /api/steps/{step_id}/attempts → data 数组：attempt_id,step_id,attempt_no,worker_id,status,started_at,finished_at,duration_ms,error_code,error_message,log_ref,output_ref
- GET /api/tasks/{id}/events?limit=100 → data 数组：event_id,event_type,step_id,attempt_id,worker_id,status,duration_ms,error_code,detail,created_at
- POST /api/steps/{step_id}/retry → 409 场景 code=DEPENDENCY_NOT_SATISFIED / INVALID_STATE；成功返回新状态
- POST /api/tasks/{id}/cancel → 409 若已终态
- GET /api/health、/api/ready

## 页面 A 任务列表（路由 /）
- 顶部：标题 + 暗/亮切换 + 「新建任务」折叠表单（assets 多行文本一行一个 + 可选 JSON 高级输入粘贴完整 body）+ 状态筛选下拉（全部/PENDING/RUNNING/RETRY_WAIT/SUCCEEDED/FAILED/CANCELLED）+ 手动刷新按钮 + 最后刷新时间
- 表格列：任务ID(前8位+title悬浮全量)/创建时间(本地时区 HH:MM:SS)/状态徽章(色码)/进度条(percent 数字)/步骤(succeeded/total)/耗时(运行中显示已用时,终态显示总时长)/失败摘要(error_summary 截断)/「详情→」
- 轮询：存在非终态任务时 2s 间隔，全部终态时 10s；tab 不可见时暂停(document.visibilityState)
- 空状态/加载骨架/接口错误横幅(带重试按钮)都要有
- 创建成功后跳详情页

## 页面 B 任务详情（路由 /tasks/:id）
- 头部卡：状态徽章+reason_code、进度条、创建/开始/结束时间、总耗时、workflow_version、cancel_requested 标记、取消按钮(终态禁用,点击有确认+结果反馈)
- DAG 视图：6 节点，parse_assets 顶 → analyze_scences → 左 generate_clips 右 generate_audio → compose_video → export_result；CSS grid/absolute 定位，节点=状态色边框圆角块(步骤名+状态+attempt n/m)；连线用 CSS 边框或 SVG 皆可，简单清晰即可
- 步骤表：step_type/状态/依赖/尝试次数(attempt_count/max_attempts)/next_retry_at/最近 error_code+error_message(截断,title 全文)/output_ref(截断)/缓存命中标记(attempt 里 status=CACHE_HIT 或事件 CACHE_HIT → 显示"缓存命中"徽章)/「重试」按钮(仅 FAILED 且非依赖阻塞时可用;DISABLE 原因用 title 提示;点击后刷新)
- 点击步骤行展开该步骤 attempts 表(attempt_no/worker_id 截断/status/duration_ms/error_code/「查看日志」链接打开尝试详情)
- 事件时间线(最近100条倒序):时间+event_type 色点+step_type+worker+error_code
- 轮询：非终态 2s，终态 10s 后停止
- 404 有专门展示(任务不存在提示+返回列表按钮)

## 页面 C 尝试详情（路由 /attempts/:attemptId）
- 从步骤展开区或事件时间线可进入
- 全字段表格：attempt_id/step_id/attempt_no/worker_id/status/started/finished/duration_ms/error_code/error_message
- log_ref 内容渲染：`GET /api/attempts/{attempt_id}/log`（后端已有静态文件? 没有——则改为前端直接显示 log_ref 路径 + 调 `/api/steps/{step_id}/attempts` 已含字段; 若后端无日志读取接口,用 fetch 访问 `http://127.0.0.1:8900/files/{log_ref}`? 后端未挂静态目录——**妥协方案**:显示 log_ref 相对路径文本 + 提示从服务器 data/ 目录查看;或调 GET /api/tasks/{task_id}/events?limit=1000 过滤该 attempt_id 的事件作为执行记录**。选后者(事件即结构化日志),标注「执行记录来自事件表」
- output_ref 显示路径 + sha256 截断

## 通用
- fetch 封装：超时 8s、统一错误提取 error.code/message、接口异常分类(网络错误/404/409/5xx)
- 所有按钮操作后 toast/横幅反馈(成功绿/失败红,3s 自动消失)
- 状态色约定：PENDING 灰 READY 蓝 RUNNING 黄(带脉动动画) RETRY_WAIT 橙 FAILED 红 SUCCEEDED 绿 SKIPPED 白纹 CANCELLED 深灰
- vite.config.ts 配 server.proxy '/api' → http://127.0.0.1:8900

## 完成标准
1. `cd frontend && npm install && npm run build` 通过（tsc -b && vite build 零错误）。
2. `npm run dev` 起来后与真实后端联调的说明写入 frontend/README.md（含截图位说明,不必真截图）。
3. 汇报：文件清单、build 输出原文、与后端联调时发现的契约不一致(如有,列出但不要自行改后端)。
