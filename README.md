# 基于本地大模型与数字孪生的汽车产线智能调度系统

> **声明：注：本项目为作者个人技术展示。为了合规，仓库内的产线布局、设备号、车型数据均为模拟泛化数据，不代表任何真实工厂环境。**

<img width="3032" height="1377" alt="123451" src="https://github.com/user-attachments/assets/271ad056-d0f5-40db-bf98-f792e2c381ef" />
> 🎯 实时鹰眼追踪：地图精准锁定车辆（如雪橇 2393），随车移动的绿色高亮旗标。

> **FJSP-T · 辊床 / 横移链输送系统实时数字孪生与智能问答**
>
> 以车间实时数据网关（示例称 EMOS）为底座，把 Line_1–Line_4 四条输送线的拓扑、车辆位置、
> 历史轨迹与决策表映射成可交互的产线数字孪生；叠加本地大模型（Qwen 2.5 / Ollama）实现
> 多轮问答、路径规划、历史统计与“鹰眼”双车追踪，可完全离线运行。

---

## 目录

- [项目定位](#项目定位)
- [系统架构](#系统架构)
- [核心技术栈](#核心技术栈)
- [项目亮点](#项目亮点)
- [目录结构](#目录结构)
- [安装与启动](#安装与启动)
- [配置说明](#配置说明)
- [常用命令](#常用命令)
- [安全与数据合规](#安全与数据合规)
- [许可与致谢](#许可与致谢)

---

## 项目定位

汽车总装车间的辊床（RB）、升降机（LT）、横移车（TC）、旋转台（TT/SL）、色线（CP）构成
一张**有向加权图**。本项目解决三个现场问题：

1. **看得见**——把 PLC/OPC UA/数据网关里的开关量还原成实时产线图：哪台车停在哪个滚床、
   哪些缓存区已满、跨线接口是否拥堵。
2. **问得清**——调度员用自然语言（含现场通用叫法）问“Line_2 有多少台车”“雪橇 2192 到注蜡出口要多久”
   “最近半小时哪里最堵”，系统给出可核对的数字、路径与图表。
3. **追得住**——对首件车、返修车做 1–2 台“鹰眼追踪”，旗标随车在长线上滑动，
   断点/丢包不误杀，恢复后自动续追。

---

## 系统架构

```
 PLC / 现场设备
      │  S7
      ▼
 OPC Server / 数据采集网关
      │  OPC UA
      ▼
 车间数据网关（示例：EMOS，订阅 / 变化推送）
      │  WebSocket（advise / dataChange）
      ▼
 codes/workshop_source.py ──► runtime_state.py ──► vehicle_state.json
      │                                              │
      │                                              ├─► conveyor_visual.html（数字孪生页面）
      │                                              │      vis.js 图 + 双车追踪 + 路径推演
      │                                              │
      └─ traj_store.py ──► MySQL conveyor_traj.vehicle_events
                                 │
                                 ├─► traj_query.py（命令行回溯）
                                 └─► traj_api.py :8766（历史统计 / 堵点 / ECharts）
                                          ▲
                                          │ HTTP
                                 conveyor_visual.html（多轮问答 + 图表）
                                          ▲
                                          │ HTTP (localhost:11434)
                                    Ollama · Qwen 2.5 3B
```

> 说明：本项目在应用层通过 **WebSocket 订阅**接入网关；OPC UA 位于网关与 PLC 之间，
> 是本系统所依赖的现场数据链路（详见 `docs/EMOS订阅逻辑说明.md` §10）。

---

## 核心技术栈

| 层次 | 技术 |
|---|---|
| 实时数据接入 | Python、WebSocket（网关订阅）、OPC UA 数据链路、订阅/心跳/断线重连 |
| 拓扑与建模 | 有向加权图、四线配置化建图（`config_Line_1~Line_4.py`）、跨线接口注册表 |
| 路径规划 | Dijkstra + **Yen K-Shortest Path**、决策表剪枝、堵塞耗时模型 |
| 持久化 | **MySQL 8**（`vehicle_events` / `vehicle_runs` / `system_meta`）、高水位增量物化 |
| 本地大模型 | **Qwen 2.5 3B（Ollama）**、Few-Shot 意图解析、多轮上下文继承、规则兜底 |
| 前端可视化 | **vis.js（vis-network）**、**ECharts**、原生 JS 单页应用、DOM 覆盖层 |
| 服务 | Flask（历史统计 API）、Python 内置 HTTP（实时页面与快照） |

---

## 项目亮点

### 1. 现场叫法语义解析（本地大模型 + 规则双通道）
- 区域术语表 `glossary.md` 注入 Prompt：*连廊 / 车顶储存线 / 注蜡1线 / 门板机器人区域* 等
  自动归一到 Line_1/Line_2/Exit_1/Line_4。
- Few-Shot 约束意图（route / count / vehicle / history_*），**代词与省略句**继承上一轮
  （“这辆车”“刚才的路径”“用了多少时间”）。
- 模型不可用或置信度低时自动回退本地正则，问答不会卡死；追踪等高频指令走本地即时响应，
  不等待大模型。

### 2. 数字对账防幻觉
- 历史统计的每个数字先由 SQL 聚合，再用 `histSummaryOk` 与前端汇总逐项对账；
  文字结论、图表数值、明细三者必须一致，不一致即降级为“仅给数据、不给结论”。
- 时间范围严格解析为 `ts_epoch` 条件（`30m/24h/今天`），**拒绝全表扫描式“估算”**；
  查不到就明确说查不到，不编造轨迹与停留时间。

### 3. 历史轨迹还原与拓扑补齐
- 轨迹查询**直接扫原始事件表**，不依赖已闭合的 `vehicle_runs`，刚进站/跨扫描区间的车辆
  也能立即出轨迹。
- 理解“过渡辊床不停车”的现场语义：跳点处按拓扑补齐节点链，并在话术中说明
  “短轨迹可能因服务重启前已进厂或经过不停车过渡辊床”，避免调度员误判。
- `vehicle_runs` 采用**事件 ID 高水位增量物化**（`system_meta.last_processed_event_id`），
  定时任务秒级完成；跨扫描未闭合车辆合并处理，轨迹不被截断。

### 4. 双车鹰眼追踪（目标锁定）
- 同时锁定最多 **2 台**车，🟢 霓虹绿 / 🔵 电光蓝双色光环 + 车号旗标，随 1s 实时刷新滑行。
- **抗抖动宽限期**：连续 15 次（约 15 秒）无位置才解锁；第 4–14 秒旗标半透明闪烁提示
  “数据延迟”，但保留在最后位置；信号恢复瞬间回实心，并在控制台打印“追踪信号已恢复”。
- **严格出口判定**：只有真实到达 Line_4 出口节点（Exit_1/2/3）才提示“已离开监控区域”；
  Line_1/Line_2 节点丢失只会提示“位置更新中断，可重新跟踪”，绝不误报离场。
- 零输入入口：地图上点选有车设备浮出 `🎯 追踪 6134` 按钮；对话车辆卡片带一键追踪按钮；
  也支持“跟踪雪橇 1234”“跟踪这台车”“取消全部跟踪”等指令，自动继承上一轮车号。

### 其他能力
- 实时数字孪生：流程式布局（入口左/出口右）、线过滤、LT 折叠、双击跨线最短路、
  占用绿/空闲灰、DEC 决策点（Line_4）。
- 历史分析：节点过车量、堵点排行（MAX−MIN 停留、离群过滤）、趋势图（ECharts）、
  车辆轨迹橙色高亮。
- 布局编辑器 `layout_editor/`：可视化建线、规格文件唯一源、坐标三段式、复合 LT 一次配置。
- 决策表演示 `demo_line_4/`：单线实时决策表订阅与前端展示（示例数据）。

---

## 目录结构

```
Industrial-Digital-Twin/
├─ codes/                    核心代码
│  ├─ config_Line_1~Line_4.py      各线拓扑/坐标/决策点（模拟数据）
│  ├─ build_line.py          建图引擎（config → 图/CSV）
│  ├─ common.py              共享建图逻辑
│  ├─ layout_flow.py         流程式布局（纯渲染层）
│  ├─ make_line_viz.py       生成数字孪生单页 conveyor_visual.html
│  ├─ runtime_state.py       车辆状态机与快照落盘
│  ├─ workshop_source.py     数据网关 WebSocket 源 / 离线回放源
│  ├─ run_runtime.py         运行时入口（订阅 / mock / replay / 落库 / HTTP）
│  ├─ traj_store.py          轨迹落 MySQL（后台批量写）
│  ├─ traj_query.py          命令行轨迹回溯
│  ├─ traj_api.py            历史统计 / 堵点 / 轨迹 API（:8766）
│  ├─ traj_health_check.py   轨迹数据体检
│  └─ samples/               订阅拓扑、设备配置与回放样例（模拟数据）
├─ config/
│  ├─ deviceConfig/          各线订阅点配置（Line_1~Line_4.json）
│  ├─ edge_model.json        边耗时类别基准表（权威）
│  ├─ carModelPlatform.json  车型/平台字典（模拟数据）
│  └─ db.example.json        数据库连接模板
├─ layout_editor/            可视化布局编辑器（自包含）
├─ demo_line_4/              Line_4 决策表演示（自包含）
├─ lines/                    各线说明与导出 CSV
├─ docs/                     接口/订阅/经验/待办文档
├─ interfaces.json           跨线接口权威注册表
├─ glossary.md               区域术语表
└─ README.md
```

> 运行期产物（`logs/`、`vehicle_state.json`、`edge_time_stats.json`、`conveyor_visual.html`）
> 与本地凭据（`config/db.json`）已在 `.gitignore` 中排除。可视化页面由程序自动生成。

---

## 安装与启动

### 0. 环境要求
- Windows 10/11，Python **3.10+**（开发环境 3.13）
- MySQL **8.x**（历史轨迹与统计）
- 可选：[Ollama](https://ollama.com/) + `qwen2.5:3b`（自然语言问答；不装则走本地规则兜底）
- 可选：Node.js（仅前端脚本语法自检）

### 1. 获取代码与依赖
```bash
git clone https://github.com/cwtyn/Industrial-Digital-Twin.git
cd Industrial-Digital-Twin
python -m pip install -r requirements.txt
```

### 2. 初始化数据库
```sql
-- MySQL 8，账号需有建库权限；库名与 config/db.json 保持一致
CREATE DATABASE IF NOT EXISTS conveyor_traj
  DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
```
复制示例配置并填写本机密码（`config/db.json` 不入库）：
```bash
copy config\db.example.json config\db.json
python codes\run_runtime.py --traj-init      # 幂等建表
```

### 3. 启动本地大模型（可选）
```bash
ollama pull qwen2.5:3b
ollama serve
```
默认地址 `http://localhost:11434`；不可用时页面自动回退本地正则解析。

### 4. 启动实时孪生页面
```bash
# 离线自测（不连现场网关）
python codes\run_runtime.py --source mock --cars 8 --steps 40

# 实时订阅 + 轨迹落库 + 本地页面
python codes\run_runtime.py ^
  --source workshop ^
  --points config\deviceConfig ^
  --ws-url <GATEWAY_HOST>/ws/emosweb ^
  --traj --serve 8765
```
浏览器打开：<http://127.0.0.1:8765/conveyor_visual.html>

> `--ws-url` 请填实际的数据网关地址（也可用环境变量 `EMOS_WS_URL`）；启动时程序会自动重生成可视化页面。

### 5. 启动历史统计 API（可选）
```bash
python codes\traj_api.py --host 127.0.0.1 --port 8766
python codes\traj_api.py --build-runs        # 增量物化 vehicle_runs（可挂定时任务）
```
健康检查：<http://127.0.0.1:8766/api/health>

---

## 配置说明

| 文件 | 用途 | 是否入库 |
|---|---|---|
| `config/db.json` | MySQL 连接（host/port/user/password/database） | 否（`.gitignore`） |
| `config/db.example.json` | 连接配置模板（无真实密码） | 是 |
| `config/deviceConfig/Line_1~Line_4.json` | 各线订阅点与决策表展开 | 是 |
| `config/edge_model.json` | 边耗时类别基准表（RB/LT/CP/TT/TC/跨线） | 是 |
| `config/carModelPlatform.json` | 车型、平台、颜色字典（模拟数据） | 是 |
| `glossary.md` | 区域术语 → 区域代码（注入 Prompt） | 是 |
| `interfaces.json` | 跨线接口权威源（build_line 校验/导出） | 是 |

密码也可通过环境变量提供：`CONVEYOR_DB_PASSWORD`（代码内不保存任何真实凭据）。

---

## 常用命令

```bash
# 建线图 / 导出 CSV / 连通性演示
python codes\build_line.py --all --demo --csv
python codes\build_line.py --verify --export          # 校验接口并导出 line_meta.csv

# 重生成数字孪生页面
python codes\make_line_viz.py

# 离线回放样例报文
python codes\run_runtime.py --source replay --replay codes\samples\ws_replay_demo.jsonl

# 轨迹回溯（PIN / 雪橇号 / 车身号前缀）
python codes\traj_query.py --pin <PIN>

# 轨迹体检 & 订阅覆盖审计
python codes\traj_health_check.py
python codes\sub_coverage_audit.py

# 历史统计增量物化（建议 Windows 任务计划每 30 分钟）
python codes\traj_api.py --build-runs
```

---

## 安全与数据合规

- **项目性质**：本项目为作者个人技术展示，仓库内的产线布局、设备号、车型与平台数据
  均为模拟泛化数据，不代表任何真实工厂环境。
- **凭据**：`config/db.json` 被忽略；`codes/traj_store.py` 不内置密码，仅从配置或
  `CONVEYOR_DB_PASSWORD` 读取。
- **脱敏**：内网地址统一以 `<GATEWAY_HOST>` 占位（代码支持环境变量 `EMOS_WS_URL`）；
  Office 文档/表格与临时提取文本因含第三方作者元数据不入库；
  区域代号 `Line_1`–`Line_4`、`Exit_1`–`Exit_3` 为匿名编号；车型/平台为虚构名称。
- **生产数据**：实时快照、日志、轨迹体检报告与生成的页面均不入库；
  回放样例使用虚构车号与设备号。
- **使用建议**：如需对接真实产线，请自行替换 `config/` 下的模拟数据，并确认现场数据授权。

---

## 许可与致谢

- 本仓库采用 [MIT License](LICENSE) 许可协议。欢迎学习、交流和二次开发。
- 感谢 vis.js、ECharts、Flask、PyMySQL、websocket-client、networkx、Ollama/Qwen 等开源项目。
