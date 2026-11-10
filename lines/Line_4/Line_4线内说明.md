# Line_4 线内说明（本线专有 · 布局与显示）

> 正式口径 = `Line_4输送描述.md`（拓扑/机制）+ `codes/config_Line_4.py`（建图数据）+ `codes/layout_flow.py`（坐标）+ `codes/make_line_viz.py`（显示）。本文记录 Line_4 特有、其他线没有的**新构造**在可视化层如何落地，以及布局调整的历史定夺。

## 布局总览（`config_Line_4.py · LAYOUT_ROWS`）

行（泳道）驱动，8 个入口逐行与 Line_2/Line_3 出口对齐；Line_4 与 Line_2 同 band1（不移动 Line_2），`DY=-510.9` 校正入口 y。主链落在下方汇聚取出口：
- r0/r1  链1、链2（B 区入口 080/075）
- r2     链3（050→…→250TT→255 出口）
- r3/r4  链4、链5（045/025 入口，125/130 对齐）
- r5/r6  链6（220→…→345 出口 Exit_2）
- r7/r8  链8（310→…→360 出口 Exit_1）
- r9     链8 尾段

**跨行对齐要点**（用户规范）：CP120 控制 125（对 065/105）/130（对 060）；CP200 控制 205（对 150/185）→210→215→220 逐行下排；220 纵对 215、链6 跟随；310 在 305 正下方、后续等距；300/305 纯 LT 存位。

## 十字脊柱（新构造 #1：十字 TT + 纵向连接 RB）

- 数据键 `CROSS_TT=[250,260,340,350,355]`、`CROSS_TT_DEVS={250,340,355}`（TT 类型集）、`cross_tt_devs`（引擎读小写键）。
- 脊柱 0° 主链边走链、90° 叉臂走脊柱；**边全建（双向）**，位置信号只控显示、不改可达性（用户定夺）。
- `layout_flow.layout_line`：遍历脊柱，**非 TT 的连接 RB（260/350）置于相邻两 TT 的垂直几何中点**（允许半行）→ 260 在 250–340 正中、350 在 340–355 正中。
- **260 可停**（订阅车点）；**350 过渡**（不可停，`can_park=False`）。

## 十字 TT 朝向显示（新构造 #1 · `make_line_viz.py`）

- 识别：`_spine_tt={pf: {str(x) for x in cross_tt_devs}}`，节点 `type=='TT'` 且设备号 ∈ `_spine_tt[pf]` → 注入 `_tt90`（= `…TT_pos2` 节点 id）、`_num`、`_baseLabel`。
- 初始（Python）：`POSITIONS[_tt90]=='1'`（90°）→ **竖放**（宽 22-28 / 高 60-66，数字 `2\n5\n0` 竖排）；否则横放如普通滚床。
- 运行时（JS `updateLive` 的 `n._tt90` 分支）：依 `DrPos2` 随时在横/竖间切换，有车变绿，悬停出车身卡。
- **垂直 RB 竖放**：仅对拥有脊柱的线（Line_4）生效，其它线同编号 260/350 保持横放（`VERT_RB` × 本次属脊柱线判断）。

> ⚠️ 实现陷阱：嵌入 JS 里若要 JS 字符串输出 `\n`（如 `join('\n')`），源码必须写 `'\\n'`，否则 Python 生成为真实换行 → 整个 `<script>` 语法错误 → 页面全白。

## 纯 LT 存位（新构造 #2：205/215/300/305）

- tag **LT/LT**（X1=X2=LT），图中**停车节点 LT + 对位节点 WT** 双节点。
- **只画 WT**：`make_line_viz` 跳过 `type=='LT'` 节点及其边，图面上纯 LT 存位由 WT 表达（停车并入 WT 对位，图面简洁）。
- `workshop_source.node_device_x1x2`：节点名以 `LT` 结尾 → 返 `(posLT, posLT)`；`run_runtime` 将 `pure_lt` 各设备纳入订阅。

## 修订记录
| 日期 | 内容 |
|---|---|
| 2026-10-26 | 布局收尾：脊柱竖直列对称等距（2/4/6/8/10）；260/350 竖放且仅 Line_4；310→305 正下等距；纯 LT 只画 WT；十字 TT 本体按 DrPos2 横/竖动态显示、取消旋臂。 |

## 待确认 / 待办
- [ ] 十字 TT、260 的实际 DrPos 节拍（当前 `Positions` 缺 pos2 数据，初始均横放；订阅 EMOS 后自动随信号转向）。
- [ ] 9 条 DrPos 位置信号接入 `config/deviceConfig/Line_4.json` 的 `points` 段并由 run_runtime 订阅。

## 相关文件
`codes\config_Line_4.py` · `codes\layout_flow.py` · `codes\make_line_viz.py` · `lines\Line_4\Line_4输送描述.md` · 跨线接口 `./interfaces.json`