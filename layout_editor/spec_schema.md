# 线路规格 spec JSON（草案 schema）

> 编辑器导出的统一中间格式；阶段 A 实现 converter 时据此细化。字段命名尽量对齐 `codes/config_XX.py` 现用词，减少桥接成本。

## 顶层
```jsonc
{
  "line": "Line_5",                 // 输送编号，全局唯一
  "comment": "描述串（原输送描述文档的要点，编辑器里可编辑）",
  "elements": [ … ],             // 单元/实体（RB/LT/TT/SL/HE/CP/TC/纬度架…）
  "edges": [ … ],                // 边：由「连接两个 entity」产生
  "subscriptions": [ … ],        // 可选：tag 订阅派生段（阶段 C）
  "layout": { … }                // 可选：坐标微调覆盖（阶段 D）；缺省走 layout_flow 自动
}
```

## elements[]（每类是「实体 + 属性」）
```jsonc
{
  "id": "Line_5_1A010RB",           // 编辑器自动按「线前缀_段号设备号类型」生成，可改
  "kind": "RB",                  // RB|LT|TT|SL|HE|CP|TC|EXT|DEC
  "type": "roller",              // 设备映射后的 node/edge 类型（RB→roller…，Editor 下拉）
  "pos": { "r": 2, "c": 7 },     // 编辑器摆放格（阶段D交互；A 可能仅占位）
  "attrs": {
    "park": true,                // can_park：false = 过渡滚床不可停
    "dir": "fwd",                // RB：fwd(单向) | bi(双向)   —— 对应 config bi_rb
    "pair": "RB",                // LT：复合配位 — RB(主线辊床配) | WT(存位配)
    "turn": 90,                  // TT：转向角度 90/180
    "stops": ["Line_5_1A015LT"],    // CP：其管辖的 LT 站元素 id 列表(勾选自动连)
    "is_hold": false,            // LT：纯存位(hold) vs 工位
    "reach": "R",                // TC：R/F 位属性(TC.pos1/pos2 建模)…
    "device": "Line_5_1A030TT",     // DEC：挂靠设备（决策发生的设备）
    "db": "L3L5_1A_LT025DecVisu",// DEC：决策 DB 块名（留空自动推导）
    "dest_map": [{"dest":1,"to":"Line_5_1A020RB"}],  // DEC：Dest道号→后续滚床，≤12
  },
  "tag": { "x1": "LT", "x2": "LT", "branch": ".IL." }   // 订阅 tag（阶段 C 推导）；DEC 无 tag
}
```

## LT 站 & CP 控制（编辑器拖一次/勾选自动连 → graph.py 展开成真节点真边）
- **LT 站 = 一个元素、一个节点**（id 带 `LT` 尾）；editor 画成**单个图标**，按配位着色（工位橙 / 存位紫），拖动一次即建，**不拆节点**。
- **不分高/中/低多级、不拆配位/低位**（现场已确证 tag=LT+LT、CP 在低位交接，positions 已移除）：主线/CP 线直接连到 LT 站节点本身；`pair`(RB/WT) 仅用于着色与 tag 语义，不建额外节点。
- **label 缺省 = 完整 id**（去掉线号前缀，如 `1A010RB`、`1A025LT`）。
- **CP stops**：`stops` 列出受控 LT 站 id → 展开自动生成 `CP→受控站` load 边（CP=LT/LT 或 LT/WT 间传送，18s）；
  编辑器画面上以紫色虚线「控制线」标出 CP→各受控站。

## edges[]（由连接两个实体产生）
```jsonc
{
  "from": "Line_5_1A010RB",
  "to":   "Line_5_1A015RB",
  "etri": "roller",              // roller|load|cross|lift|tt|ext
  "cost": null,                  // 缺省 = 类别基准表(config/edge_model.json)自动；显式填则进 exceptions
  "xline": false                 // 跨线接口边(EXT) 等
},
{ "from": "Line_5_1A010RB", "to": "Line_5_1A010LT_high", "etri": "load" }
```

## subscriptions[]（阶段 C）
```jsonc
{ "node": "Line_5_1C210LT", "dtype": "LT", "branch": ".IL.",
  "x1": "LT", "x2": "LT", "points": ["body","skid","target"] }
```

## 校验（converter/阶段 A 起步）
- 元素 id 全局唯一、线前缀一致；边两端元素存在；可停位成对（CP 的站点都在场）。
- 跨线接口成对（上游声明 + 对线占位）；tag 与设备类型一致（LT→x1=x2=LT/RB 规则）。
- 连通性：喂给 build_line `--verify`，示例演示用例可跑通。