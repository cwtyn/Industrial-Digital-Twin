# EMOS 车间数据订阅逻辑说明（每滚床车身号 + 雪橇号）

> 适用：Line_1 / Line_2 / Line_3 辊床/横移链输送系统。本文档把「读车间 EMOS 设备数据」的完整链路整理成标准流程。代码实现见 `codes/workshop_source.py`，运行入口见 `codes/run_runtime.py`。

---

## 1. 数据源

| 项 | 值 |
|---|---|
| 服务地址 | `ws://<EMOS_HOST>/ws/emosweb`（车间 EMOS 网关，端口 80）|
| 网络 | 车间内网 / 双网卡 wifi 内网可直连；**无需登录、无认证** |
| 订阅内容 | 每台滚床两条：**车身号**（`M1003_BodyID`）+ **雪橇号**（`I1030_RBDataSkidNo`）|

---

## 2. 总体链路

```text
EMOS 网关 (ws://<EMOS_HOST>/ws/emosweb)
   │  ① 连接
   │  ② 心跳 2s: {"type":"info","info":"alive"}
   │  ③ 逐条订阅车点: {"id":"","type":"advise","plc":"L3Lx","tag":"<点位tag>"}
   │       每台滚床发两条——车身号 + 雪橇号
   ▼
服务器立即回推每个点位当前值（=初始占用快照）
   ▼
逐帧回调 translate_message() →  place / clear / skid 统一事件
   ▼
runtime_state.VehicleState 占用表（键=滚床节点ID）→ 落盘 vehicle_state.json
   ▼
make_line_viz.py 可视化(灰=空 / 绿=有车 / 悬停显示车辆+雪橇信息)
```

---

## 3. 报文协议

### 3.1 心跳（每 ≤2s 一次，保持连接）
```json
{"type": "info", "info": "alive"}
```

### 3.2 订阅车点（每台滚床两条）
```json
{"id": "","type":"advise","plc":"L3L1","tag":".L3L1_1A_1A020RB_1A020RB.IL.SD.M1003_BodyID"}       # 车身号
{"id": "","type":"advise","plc":"L3L1","tag":".L3L1_1A_1A020RB_1A020RB.IL.SD.I1030_RBDataSkidNo"}  # 雪橇号
```
> 服务器收到后立即回推每个 tag 当前值。

### 3.3 服务器回推（数据帧)
```json
{"type":"dataChange","id":"","tag":"L3L1.L3L1_1A_1A020RB_1A020RB.IL.SD.M1003_BodyID",
 "ts":"2026-10-20T14:19:45.558+08:00","value":"782026843013792N54Y2T2TP_A000-" }
```
- 车身号点位：`value` 为空（`---`/`...`/全横杠/全空白）＝ 滚床释放（clear）。
- 雪橇号点位：`value` 即雪橇号（空车时回 `0`）。

### 3.4 控制帧（忽略，无需处理）
- `ActiveCasServer`、`Session`、`DatabaseConnection` 等 `{type:"general"}` 帧：信息性，跳过。
- `WSS.Error hasError:true`：**表示订阅了不存在的点位 tag（设备号或点位名写错）**，不是认证失败。

---

## 4. 点位 tag 格式（核心）

### 4.1 结构（分支 `.IL.` 默认 / `.ILO.` 成组）
```
.{PLC代号}_{段号}[_{mid}]_{X1}_{X2}[.IL|.ILO].SD.{点位名}
```
> `_{mid}` 仅少数**非标准**点位才有（如 Line_1 355/390 的 `14AS`/`15AS`），正常点位无此段。
| 段 | 含义 | 规则 |
|---|---|---|
| 前导 `.` | 固定 | EMOS tag 以点开头 |
| `PLC代号` | `L3` + 短线代号 | `L3L1`/`L3L2`/`L3L3`；**L3 与 Line_x 之间无点** |
| 段号 | 区段 | 如 `1A`、`1D` |
| **X1** | 实际停靠设备 | 纯滚床=RB；升降台=LT；转台=TT；跨层=SL；横移车=TC；提升机=HE |
| **X2** | 该位置的 RB | **恒为 RB**——RB 依附在 X1 设备体上（纯RB时与 X1 同号）|

> **X1 原则**：TAG 的第一个部位总是"设备体"（RB/LT/TT/SL/TC/HE）；凡节点带这些设备类型，X1 就是它，X2 固定为该位置的 RB。纯 RB 时 X1=X2。
| `SD` | 固定 | |
| `IL`/`ILO` | 台体分支 | **默认 `.IL.`**（单点台）；**与前后滚床"同进同出"（联动/成组）的滚床用 `.ILO.`**（多点台）。判定准则见下。|
| 点位名 | 车身号 / 雪橇号 | `M1003_BodyID` / `I1030_RBDataSkidNo` |

> **IL(单) vs ILO(成组) 分支判定准则（2026-10-27 用户定夺）：**
> - **普通、独立传送的滚床** → `.IL.`（默认）。绝大部分滚床都是这一个。
> - **与前后滚床"同进同出"（联动一起走、成组传送）的滚床** → `.ILO.`。
> - 误用分支时服务器不回该点位数据（订阅到不存在的 tag，回 `WSS.Error`），所以分支必须按现场逐台定准。
> - 实现：`config/deviceConfig/<线>.json` 里给该滚床 roller 加 `"branch": "ILO"`，`workshop_source` 据此生成 `.ILO.` 的三条点位（body/skid/target）。已知 `.ILO.`：Line_4 `1C275RB`、`1D235RB`。

### 4.2 示例（每台两条）
```text
纯RB 位置 020 —(独立传送, .IL.)
  车身: .L3L1_1A_1A020RB_1A020RB.IL.SD.M1003_BodyID
  雪橇: .L3L1_1A_1A020RB_1A020RB.IL.SD.I1030_RBDataSkidNo
Line_4 1C275RB —(与前后滚床同进同出, .ILO.)
  车身: .L3L4_1C_1C275RB_1C275RB.ILO.SD.M1003_BodyID
  雪橇: .L3L4_1C_1C275RB_1C275RB.ILO.SD.I1030_RBDataSkidNo
LT   位置 010 —
  车身: .L3L1_1A_1A010LT_1A010RB.IL.SD.M1003_BodyID
  TT   位置 135 —
  车身: .L3L1_1D_1D135TT_1D135RB.IL.SD.M1003_BodyID   # 节点名是 RB, 但拓扑把该位置标成 TT, X1 按其类型
  SL   位置 390（非标准: 段号后多插 15AS）—
  车身: .L3L1_1E_15AS_1E390SL_1E390RB.IL.SD.M1003_BodyID
  SL   位置 355（非标准: 段号后多插 14AS）—
  车身: .L3L1_1E_14AS_1E355SL_1E355RB.IL.SD.M1003_BodyID
  TC   承载体 402 —
  车身: .L3L1_1F_1F402TC_1F402RB.IL.SD.M1003_BodyID
```
> **实测（2026-10-20，230 台全量）：全部 230 台 body+skid 均回 dataChange。**
> 订阅集 = 所有**可停靠**的 RB 位置（纯 RB + **LT 工位** + 被拓扑标成 TT/SL 的 RB 名称节点 + **TC 承载体**），**排除过渡辊床**（`can_park=False`）。X1 取设备体类型（RB/LT/TT/SL/TC/HE）、X2 恒为该位置 RB，雪橇号与车身号走同一设备路径。
> - **LT 工位（21 台）**：X1=LT（如 `1A010LT_1A010LT`）。实测 `1A010RB_1A010RB` 这类纯 RB 写法**不存在**，必须用 LT。**2026-10-28 起 X1=X2=LT（`LT_LT`）**：`LT_RB` 与 `LT_LT` 都存在，但 LT_LT 才回真位置（`LT+LT`），弃 LT_RB。改于 `node_device_x1x2`（workshop_source.py）。
> - **TC 承载体（3 台）**：Line_1 `402`、Line_2 `485`/`530`，X1=TC（`cfg.TC['dev']` / `cfg.TC_DISPLAY[].dev`）。
> - **非标准写法**：Line_1 `355`/`390` 在 PLC 程序里段号后多插了一段 `14AS`/`15AS`（编程笔误），即 `.L3L1_1E_14AS_1E355SL_1E355RB.IL...`；表中维护于 `run_runtime.SPECIAL_TAG_MID`。

### 4.3 服务器规范化的回推 tag
服务器会把订阅 tag 规范化后回推（带 PLC 前缀、去前导点）：
`L3L1.L3L1_1A_1A020RB_1A020RB.IL.SD.M1003_BodyID`
→ 代码用「设备路径子串匹配」定位节点，与前后缀无关。

### 4.4 位信号（设备状态/位置，非车辆数据）— 2026-10-20 实测保留

设备的位置/状态信号走**另一条分支**，与车身号点位不同：

```
.{PLC代号}_{段号}[_{mid}]_{X1}_{X2}.SB.{点位名}
```

| 差异点 | 车身号点位 | 位信号 |
|---|---|---|
| 分支 | `.IL.SD.M1003_BodyID` | `.SB.{点位名}`（无 `IL`/`SD`）|
| X2 | 该位置的 **RB** | **与 X1 相同的设备体** |
| 值 | 30 位车身号 / 空 | `'1'` / `'0'`（字符串）|

实测样例（Line_1 390SL 下降位）：
```text
订阅: .L3L1_1E_15AS_1E390SL_1E390SL.SB.S1012_PDown
回推: L3L1.L3L1_1E_15AS_1E390SL_1E390SL.SB.S1012_PDown
      value='1'   ts=2026-10-20T16:18:26.516+08:00     # 1=在下降位
```
- **X2 必须用设备体**：`1E390SL_1E390RB` 实测回 `WSS.Error`（点位不存在）。
- 同一条连接可批量订阅多个点位；探测工具 `codes/probe_tag.py`：
  ```bash
  python codes/probe_tag.py --seconds 10 \
      --tag ".L3L1_1E_15AS_1E390SL_1E390SL.SB.S1012_PDown"
  ```
- 用途（后续）：TC 横移车 / TT 转台 / SL 剪式升降台的**确切位置与到位状态**，可把"车在哪个工位、机构是否到位"补进运行时状态。

### 4.5 TC 位置信号 — 2026-10-21 实测加入（无统一规则的典型）

TC 横移车在 **R 位（链6 侧承载体格）** 与 **F 位（链5 侧虚拟格）** 之间往返。布两张布局格各一条位置信号，`'1'`=TC 目前在该位：

| PLC | 承载体 | R 位(链6) tag | F 位(链5) tag | 布局格 |
|---|---|---|---|---|
| Line_1 `402TC` | `Line_1_1F402RB` | `.L3L1_1F_1F402TC_1F402TC.SB.S1069_PRev` | `.L3L1_1F_1F402TC_1F402TC.SB.S1068_PFwd` | R=`Line_1_1F402RB`, F=`Line_1_F402RB_TC_F` |
| Line_2 `485TC` | `Line_2_1F485RB` | `.L3L2_1F_1F485TC_1F485TC.SB.S1421_DrPos1` | `.L3L2_1F_1F485TC_1F485TC.SB.S1422_DrPos2` | R=`Line_2_1F485RB`, F=`Line_2_F485RB_TC_F` |
| Line_2 `530TC` | `Line_2_1G530RB` | `.L3L2_1G_1G530TC_1G530TC.SB.S1421_DrPos1` | `.L3L2_1G_1G530TC_1G530TC.SB.S1422_DrPos2` | R=`Line_2_1G530RB`, F=`Line_2_F530RB_TC_F` |

**要点（务必遵守）**：
1. **点位名没有统一规则**。Line_1 用 `S1069_PRev / S1068_PFwd`，Line_2 两个承载体都叫 `S1421_DrPos1 / S1422_DrPos2`（靠段号 `1F`/`1G` 区分）。**每次只能由 PLC/现场给出**，代码不推导、不猜。
2. **R 与 F 共享同一设备前缀**（`1F402TC_1F402TC`），仅点位后缀（`PRev`/`PFwd`、`DrPos1`/`DrPos2`）不同——因此**必须按整条 tag（含后缀）匹配**，前缀子串匹配分不开两者。`workshop_source.make_extra_resolver` 就是按整条 tag 落到 config 里显式给的 node。
3. 登记方式：在**该线**的 `config/deviceConfig/<线>.json` 的 `points[]` 加一条（每信号一条）：
   ```json
   {"node": "Line_1_1F402RB", "tag": ".L3L1_1F_1F402TC_1F402TC.SB.S1069_PRev",
    "kind": "tc_pos", "node_role": "R位(链6承载体格)"}
   ```
   `node` = 布局位置格（R=承载体 RB 节点；F=虚拟格 `<线>_F<dev>RB_TC_F`），`kind` 任取非 body/skid 值（如 `tc_pos`）。探测真实 tag 用 `codes/probe_tag.py`。
4. **显示**：位置格 `tccell` 节点由 `POSITIONS[node]=='1'` 驱动——在 → 有车绿(`#4CAF50`)/空灰(`#9E9E9E`)；不在 → 暗灰(`#616161`)。`positions` 与车辆占用一并写进 `vehicle_state.json`（`{"vehicles":…, "positions":…}`）。
5. `node_role` 字段仅作人工备注，无功能。

---

## 5. 车身号解析（30 位）

```
PIN[0:14]  车型[14:19]  颜色[19:23]  平台[23:26]  Code[26:29]
```
例：值 `782026843013792N54Y2T2TP_A000-`
| 字段 | 位置 | 值 |
|---|---|---|
| PIN | `[0:14]` | `78202684301379` |
| 车型 | `[14:19]` | `2N54Y` |
| 颜色 | `[19:23]` | `2T2T`（4 位码，直接展示，不翻译中文）|
| 平台 | `[23:26]` | `P_A` |
| Code | `[26:29]` | `000` |

> 车型 FIS 2 位用于车型/产量统计 = `value[14:16]`。
> 空值判定：`---`/`...`/全横杠（如 27 个 `-`）/全空白 ＝ 滚床释放（clear）。

---

## 6. 事件与占用表

| 统一事件 | 触发 | 效果 |
|---|---|---|
| `place(node_id, vehicle)` | 车身号回推有效值 | 该滚床占用该车 |
| `clear(node_id)` | 车身号回推为空 | 释放该滚床 |
| `skid(node_id, sled_no)` | 雪橇号回推 | 更新该车雪橇号 |
| `pos(node_id, value)` | 附加位信号回推（SL/TT/TC 位置，见 §4.4/§4.5）| 记入 `positions[node]=value` |
| `snapshot` | 全量复位 | 重置整表 |
| `print` | 心跳/连接/日志 | 仅打印 |

占用表以**滚床节点ID**为键（如 `Line_1_1D325RB`）。TC/TT 本身不出数据点，车辆信息挂在其所在位置的 **RB 节点**上展示。落盘的 `vehicle_state.json` 现在是 `{"vehicles":{node:车}, "positions":{node:'1'/'0'}}` 双段结构；`load_snapshot` 与可视化均兼容旧版只含车辆映射的格式。

---

## 7. 运行方式

### 离线回放（无需车间服务器，验证链路）
```bash
python codes/run_runtime.py --source replay \
    --replay codes/samples/ws_replay_demo.jsonl \
    --points config/deviceConfig --live
```

### 实时接入（含网页实时刷新）
```bash
python codes/run_runtime.py --source workshop \
    --ws-url <EMOS_HOST>/ws/emosweb \
    --points config/deviceConfig --live --serve 8765
```
浏览器打开 **`http://127.0.0.1:8765/conveyor_visual.html`**：
- 页面每 **2 秒** `fetch('vehicle_state.json')`，就地更新滚床颜色（灰=空 / 绿=有车）、悬停卡片（车身/颜色/车型/雪橇）、右侧车辆清单与统计条，**不重载页面**（缩放/拖拽/选中都保留）。
- **必须用 HTTP 打开**：`file://` 下浏览器禁止 `fetch` 本地文件，页面会退化为生成时的静态快照。
- `--serve` 不起时仍可用 `python -m http.server` 在项目根目录自行起服务。

> `--points` 缺省按拓扑为 230 台可停滚床生成占位点（每台 body+skid 两条，统一 `.IL.`）。接真机建议用经校对的真实 config 覆写：`config/deviceConfig/` 目录（每线一个文件，已实测 230 台 body+skid + SL/TC 位信号全通）。
> 实时源 `consume()` 不返回，因此页面在开跑前先生成一次；随后靠前端轮询刷新。

---

## 8. 代码模块关系

| 文件 | 职责 |
|---|---|
| `codes/workshop_source.py` | `WorkshopSource`(实时)/`ReplaySource`(离线)/`translate_message`/`VehicleId`/tag 生成(body+skid)与解析 |
| `codes/runtime_state.py` | `Vehicle`/`VehicleState` 占用表/`consume()` 事件循环(含 skid)/`VehicleSource` 接口 |
| `codes/run_runtime.py` | 运行入口，选定数据源并驱动 consume 落盘+重生成可视化 |
| `codes/make_line_viz.py` | 生成 `conveyor_visual.html` 叠加车辆+雪橇显示 |
| `codes/probe_tag.py` | 单连接探测任意点位 tag（验证是否存在、回值），位信号排查用 |

---

## 9. 关键经验（踩坑记录）

1. **WSS.Error ≠ 认证**。是订阅到不存在的点位（设备号/点位名错）。本项目无需登录。
2. **分支默认 `.IL.`**；仅"与前后滚床同进同出/成组"的滚床用 `.ILO.`（见 §4.1 判定准则）。误用分支＝该席位收不到数据（回 `WSS.Error`），需按现场逐台定准。
3. 每台滚床**两条订阅**：车身号 `M1003_BodyID` + 雪橇号 `I1030_RBDataSkidNo`。
4. tag 里 **L3 与 Line_x 之间无点**；整串以 `.` 开头。
5. 服务器会规范化 tag（`L3L1.` 前缀），节点定位按设备路径子串，别做整串精确匹配。
6. 车身号是 30 位（含 Code 与尾字符），不是 26 位；空车值可能是 27 个连字符。

---

## 10. 总体架构与分层 —— WebSocket / EMOS / OPC UA / PLC（理解模型）

> 说明：EMOS↔PLC 内部的具体采集方案目前**没有现场实测证据**，下面是结合该车间（西门子 PLC + 点位 tag）最可能的典型结构。核心骨架（PLC 自主扫描 → 中间翻译层 → 上层订阅变化分发）对这类车间是稳的；若日后拿到 EMOS 部署配置（OPC 服务器地址/IP/端口）可把中间段定为实锤。

**关键认知：WebSocket 不是直接连 PLC。**

```
[浏览器/本机 run_runtime]  ←WebSocket→  [EMOS 服务器]  ←OPC UA→  [OPC Server / 数据采集网关]  ←S7协议→  [PLC]
```

WebSocket 是「网页服务器 ↔ 浏览器」的协议；PLC 是工业控制器，讲 S7/Profinet。两者语言不同，中间隔着把 PLC 点位翻译成标准 tag 的 OPC Server。

| 层 | 角色 | 干活方式 |
|---|---|---|
| **PLC** | 数据源头 | **自己周期扫描**（内部循环刷存储区），**不是被人叫醒** |
| **OPC Server** | 翻译/代理 | 周期从 PLC 读点位（或 PLC 变化触达），翻译成标准 tag，检测变化 |
| **EMOS** | OPC 客户端 + WebSocket 服务器 | 订阅 OPC 的 tag，翻译成 WebSocket 报文推到浏览器 |
| **本机 run_runtime** | WebSocket 客户端 | 订阅 EMOS 的 tag，记进 `vehicle_state` → 落盘 |

**两层订阅嵌套**：用户订 EMOS 的车点 tag（`M1003_BodyID`…）；EMOS 自己也是"订阅者"，它面向 OPC 订 PLC 底层 tag。用户发一条订阅 → 透传到 EMOS → EMOS 挂在 OPC 的对应 tag 上 → OPC 与 PLC 同步。

**变化检测在 OPC Server 层**：OPC↔PLC 之间即使"无变化"也在默默保持同步（周期读最新值），**只有值真正变化才逐层向上冒**（OPC 通知 → EMOS → WebSocket → 你）。这就是"平时静、变化才推"的底层机制；上层的静默建立在下层仍在保持同步之上。

---

## 11. 推送机制、快照残留与关机/开机行为

### 11.1 订阅是"点到点"，不是全车间广播
给每个 tag 的订阅绑定你的连接；服务器**按订阅清单私发**给各客户端，不广播给所有人。工作量主要由"有变化的瞬间"产生，平时安静。

### 11.2 "本机看到的数据存在本地"，不是服务器实时
现场不推时，数据**不流动**，而是驻留在**本机**：`run_runtime` 在内存维护 `VehicleState` 表并落盘 `vehicle_state.json`（项目根）。页面每 2s `fetch` 该文件。服务器**只在变化时推，不存现场全貌**——"本机账本记最后一次变化的结果"，所以现场静悄悄、页面仍显示最后一次变化后的状态。

### 11.3 关机 = 断连，服务器既不推、也不存历史
- 关机 → WebSocket 断开 → 服务器删除该连接的订阅清单，**停止推送**。
- 服务器**只推变化、不存历史**：关机期间现场的变化（车来车走）**不会替你攒着、开机补发**。推出去无人收即丢弃。

### 11.4 开机重连 = 订阅回推当前值，刷成"当下的实况"
- 启动连上服务器后，对每条订阅**回推一次当前值**（初始快照），布局图随即刷成**当前现场**，而非关机前。
- **唯一旧数据残留窗口**：脚本刚启动、首次快照到达前那 1~2 秒，`vehicle_state.json` 还是关机前留下的旧文件 → 页面短暂显示旧状态，随后被实况覆盖。这是"重连回推"的固有瞬时态。

### 11.5 与现场一致的前提与局限
「布局图 = 现场」只在两条成立：数据链路没断、起始快照完整。若订阅中断/重连丢了中间变化，本机账本可能停在旧状态。靠"末次快照 + 变化推送"无法做到逐车精确对账；若要门对门核对"现在到底有几台、各在哪"，需**周期性主动全量拉一次快照/对账**，而非只等变化推送。

---

## 12. 未来方向（规划中，未实现）

- **车辆路径查询**：记录每车经过的节点序列（从入口到出口/当前位置），支持"某车从入口到某位置经过哪些路径"回溯。需在 `place` 事件时追加车辆轨迹记录（按 body_no 维护路径），落盘一份历史轨迹文件。
- **自然语言/对话框查询**（替换左下角"搜索设备"框）：见 README 增补——《AI 对话框查询》（§规划）。核心是把已标准化的 `vehicle_state.json`（车辆+雪橇+位置）作为问答上下文，典型问法：雪橇号/车身号/PIN 定位 → 反查该车所在滚床、头像卡片；车辆路径回溯 → 查历史轨迹。数据都在本机，无需新增车间订阅。