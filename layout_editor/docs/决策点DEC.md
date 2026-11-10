# 决策点 DEC 元素（分道决策 · Dest 映射）

> 结论先行：**在布局编辑器里，一个「决策点」就是独立元素 `kind:'DEC'`** —— 一个六边形图标（`#455A64`），属性用来表达「这个位置在哪决策、决策后各道号对应后续哪个滚床」。导出 spec 后由 graph.py / tags.py 接管，供路径规划在分叉处决定下一步走向。
> 现场依据：`ud_Sf_DecPointRow.udt` + 导出 DB `L3L4_1A_LT025DecVisu.db`（决策表是 **西门子 S7 DB 块**，非 SQLite）。

## 一、先理解现场 PLC 的决策表（为什么它这么设计）

真实决策**不是**拿"目标地址直接翻表"那么简单，而是**用车辆信息多字段评分**，表由工人可在 HMI 上改，所以是**活的、要实时订阅**：

- **每个需要决策的位置一个 DB 块**，类型 `ud_Sf_DecPoint` = **4 页 × 12 行** 决策行。
- 每行（`ud_Sf_DecPointRow`）四个子结构，分工：
  | 子结构 | 含 | 用途 |
  |---|---|---|
  | **SB** | `S9999_Data1..5Match` / `FullMatch` / `Use` / `Prior` | 匹配状态 + 启用开关 + 优先级 |
  | **CB** | `C9999_Use/Clear/Lock/Edit/Up/Down/Prior` | HMI 操作命令 |
  | **AS** | `I9999_Filtr01..05` + `I9999_Dest` | **决策条件**(5 个过滤字段) + **结果**(道号) |
  | **AA** | `A9999_LastSearch/LastModeChange` | 管理时间戳 |
- **5 个过滤字段（AS.I9999_Filtr01..05）依次对应**：
  1. **VIN**（车身号）→ 2. **CodeF** → 3. **CodeB** → 4. **BodyType**（车型）→ 5. **Platform**（平台）。
- **扫描规则**（车辆到达该站，PLC 解析出行数据后）：**先扫 VIN → 没中再扫 CodeF → … → 最后扫 Platform；命中第一条就结束**；空过滤字段跳过；且该行 `S9999_Use=true` 才算启用。
  例：只设车型 `xv→2`、`HC→3`，则到站车辆按车型翻到对应道。
- **`I9999_Dest` = 结果道号（1,2,3…）**，不是滚床编号；道号 → 具体滚床的映射**在布局图中给出**（这正是 DEC 元素要承载的）。
- **`S9999_Use`（启用开关）工人可在 HMI 上随时开/关** —— 某行决策不要就关，需要就开 → 说明这数据**不是静态配置，要实时订阅**（走 EMOS 事件回放/订阅链路）。

> 所以 `Dest` 数量在编辑器里**定 12**（留余量，2 页逻辑≤12 但按 12 预留），决策点作为**独立元素**（HMI 上也是一个独立图标，告诉工人"这位置能分道决策"）。

## 二、编辑器里怎么落（本轮已实现）

### 元素属性
```jsonc
{
  "id": "Line_5_1A032DT",
  "kind": "DEC",
  "pos": { "r": 2, "c": 9 },
  "attrs": {
    "device": "Line_5_1A030TT",          // 挂靠设备（决策发生的设备）
    "db": "L3L5_1A_LT025DecVisu",    // 决策 DB 块名（留空则自动推导：L3<线>_<段>_LT025DecVisu）
    "dest_map": [                       // 有序，≤12 行；Dest 道号 → 后续滚床
      { "dest": 1, "to": "Line_5_1A020RB" },
      { "dest": 2, "to": "Line_5_1A030RB" }
    ]
  },
  "tag": {}
}
```
- `editor.html` 面板：`select()` 里 `else if(e.kind==='DEC')` 独占表单 —— **DB 名、挂靠设备、12 个 dest_map 行**；DEC 分支**隐藏 attrs JSON 与 tag 手动覆盖段**（`if(e.kind!=='DEC')` 守卫）。
- 校验 `validateNow()`：`device` 必填；`dest_map` 内 `dest` 道号不重复；`to` 必须指向**已存在**的元素 id。
- 内置样例 `sampleSpec()` 有一处 `<线>1A032DT` 决策点，挂在 `1A030TT` 上，dest 1→`1A020RB`、dest 2→`1A030RB`，DB `L3<线>_1A_LT025DecVisu`（对齐现场 Line_4 的 1A025 决策块命名）。

### 渲染 / 建图 / 标签侧
- `graph.py` `KIND_STYLE` 补 `'DEC': dict(shape='hexagon', color='#455A64', w=66, h=20)`；DEC 既非可停设备也非真实边目标，**不计入未连通/流计数**（`kinds.get(x) not in ('CP','EXT','ENT','DEC')`）。
- `preview.html` / `preview_merge.html` 图例各加了一行 `⬡ DEC 决策点`。
- tags.py 的 `TAG_KINDS` **不含 DEC**（决策点不订阅点位，只做路由语义）。

## 三、现场对齐口径（写给自己 / 整合方）
- 决策要真正生效，靠的是**现场 DB + EMOS 实时订阅**；布局编辑器只负责**静态建模**：哪个位置有决策、各道号对应哪个滚床。
- 五字段决策条件与 `Use/Prior` 是**运行时**数据，编辑器不建模在 spec 里（那不是静态拓扑）。
- 需要决策的现场位置：横移 LT / 转台 TT / 横移车 TC / 升降机 HE 处都有；DEC 是**独立于这些设备之外**贴出来的标记点。
- 简化演示样本（`samples/`）可先不去动真实 Line_1-28；DEC 语义先在自包含样例里跑通。

## 参考资料
- `../ud_Sf_DecPointRow.udt`（本仓库根目录，行业侧 UDT）
- `../L3L4_1A_LT025DecVisu.db`（现场决策 DB 导出，文本格式）