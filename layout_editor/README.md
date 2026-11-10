# 布局编辑器（可视化建线工具）— 自包含独立任务文件夹

> 目标：把「新增一条输送」从"改 config/XX.py 文本 → 建图 → 反复看图挑毛病"，变成
> **浏览器里拖拽放单元 + 连线 + 填属性 → 导出线路规格 spec JSON → 本文件夹内自建图自渲染 → 直接出 deviceConfig/tag 片段**。
>
> **本文件夹自包含、可分发**：内嵌自己的建图/渲染/校验/标签引擎，**运行不依赖 `./codes\`、`config\`、`lines\` 及 Line_1-28**。
> 打包发给同事 = 整个 `layout_editor\` zip，对方单独解压即可做相关新增建线工作。

## 自包含承诺（用户 2026-10-28 定）
- **工具不依赖现有文件**：建图、预览渲染、校验、标签推导的逻辑全部落在本文件夹内（独立引擎，不从 E: codes 引 import）。
- **新线在本文件夹内全流程完成**：spec JSON → 自建图 → 自预览(vis-network HTML) → 自出 deviceConfig points。
- **现有 Line_1/Line_2/Line_3/Line_4 概不迁移、概不依赖**；必要时把某线一小段**复制为自包含样例**进 `samples/` 作测试夹具（拷贝数据，非引用）。
- 失败/返工只发生在本文件夹，回滚=删目录，主线无碍。

## 架构（最终，2026-10-28 定）
```
editor.html (拖拽+连线+属性表单) ──导出──▶ spec JSON
                                             │
              ┌──────────────────────────────┤
              ▼                              ▼
        graph.py(自建图+校验)          tags.py(→deviceConfig points)
              ▼
        preview.html(自渲染 SVG, 自包含单文件, 吃 preview_data.js)
```
- **编辑器**负责录入拓扑+语义+标签；**graph.py** 把 spec 转图并校验；**tags.py** 由 spec 直接产设备订阅点（避 tag 拼接错）；**preview.html** 自渲染，无需外部仓库。
- 标签维度输入：单元属性里填「UB 标签、X1/X2、branch(.IL./.ILO.)」，tags.py 据此生成。

## 决策点 DEC元素（本轮新增，2026-11-03）
独立六边形元素 `kind:'DEC'`，承载「某位置分道后各道号(Dest)→后续滚床」映射。
属性 `attrs = {device, db, dest_map:[{dest,to}]}`（dest≤12 预留）。**现场决策表是活的**（HMI
可改、需实时订阅），编辑器只做**静态建模**；其完整结构（4页×12行·五字段VIN/CodeF/CodeB/
BodyType/Platform·Use/Prior·扫描规则）见 **`docs/决策点DEC.md`**。graph.py 已认 DEC(hexagon/
#455A64)、不计入未连通；tags.py 不含 DEC。

## 阶段（子任务挂件见 docs/待办任务.md）
- **A 最小闭环（自包含）**：editor.html 面板 + palette + 拖放 + 连线 + 属性表单 → 导出 spec → **本目录内 graph.py 自建图 + 校验 + preview.html 自渲染**。证明"编辑器出的能在本文件夹内单独跑通、不碰 E: codes"。
  - **LT 站（已实现）**：**一个元素 = 一个节点一个图标**，调色板「升降·工位(RB) / 升降·存位(WT)」，拖一次即建；按配位着色（工位橙 / 存位紫），属性面板可改 pair。**不拆配位/低位、不分高/中/低多级**（现场确认 tag=LT+LT、CP 在低位交接，positions 已移除）；`pair` 仅用于着色 + tag 语义，不建额外节点。
- **节点标签缺省 = 完整 id**：去线号前缀，如 `1A010RB`、`1A025LT`、`1A035XX`。
  - **CP 控制自动连（已实现）**：CP 属性面板**勾选受控 LT 站** → 画布紫虚线控制线，graph.py 展开自动生成 `CP→{LT}_low` load 转运边(18s)。
- **B 属性补全 + 校验**：方向(单向/双向) / TT 转向角(90/180°) / LT 位置数(高/中/低) / CP 链站点 / can_park 过渡不可停 / EXT/INBOUND 跨线接口 / cross·lift·tt·tc 耗时类；校验：id 唯一·线前缀一致·边端点在场·可停位成对·跨线成对。
- **C tag 订阅生成**：body/skid/target + `x1/x2`(LT+LT vs LT+RB) + branch → 产 deviceConfig points 片段。
  - **阶段C 已实现 + s1/s2/s3 菜单已落地（2026-11-02）**：编辑器顶部 S1导出spec/S2点位/S3校验；
    S1/S2 用浏览器文件系统 API（`window.showDirectoryPicker`，句柄记 IndexedDB，Firefox 降级下载）；
    点位推导编辑器内是 JS 预览版，**权威在 tags.py**（无可避免漂移，整合方用 tags.py 重跑）。
    tags.py 接口：`my -o <outdir> <spec.json>` 产 deviceConfig\<线>.json + _tags.txt；
    `TAG_KINDS=(RB,WT,LT,TT,SL,HE,TC)`（CP/EXT/ENT/DEC 不订阅点位）。tag 覆盖：元素 `tag.x1/x2`
    存**完整设备串**（非简写），`tag.branch`(.IL./.ILO.)、`tag.mid`(非标段)。
- **D 坐标三阶段（用户定）**：**手动先排 → 自动校对(对齐/防重叠/对称) → 手动微调**；坐标存 spec 的 layout 段，可导出导入。

## 坐标策略（用户 2026-10-28 定，非二选一）
`layout_editor` 坐标走**三段式**：
1. **手动先排**：编辑器网格里拖到大致位置；
2. **自动校对**：跑一轮对齐/防重叠/对称/等距修正（类似现有 layout_flow 的语义，但内嵌在本目录）；
3. **手动微调**：在校对后细微覆盖，结果存 spec.layout。

## 文件规划
- `README.md` —— 本文档（设计定论 + 口径）
- `spec_schema.md` —— 线路规格 JSON 草案（元素/边/标签 schema）
- `editor.html` —— 前端拖拽面板（阶段 A）
- `graph.py` —— spec → 图 + 自建图校验 + 导出可渲染数据（阶段 A，自包含引擎）
- `preview.html` —— 自渲染 vis-network 预览（由 spec/graph.py 灌数据，阶段 A）
- `tags.py` —— spec → deviceConfig points 片段（阶段 C）
- `samples/` —— 自包含样例（阶段 A 起：如把 Line_1 一小段拷成夹具）
- `docs/决策点DEC.md` —— 决策点元素说明 + 现场 PLC 决策表结构（4页×12行/五字段/Use/Prior）

## 给同事的三步工作流 + 点位权威约定（用户 2026-11-02 定）

**同事画完一条线,只用做三件事(菜单位于编辑器顶部)：**
1. **S1 导出spec** —— 把当前线 spec 直接写进专属文件夹（浏览器文件系统 API,首次弹一次目录选择器,之后免授权写同一目录;Chrome/Edge 支持等,Firefox 自动降级到「导出弹窗(多线)」下载）。
2. **S2 点位** —— 预览当前线生成的 deviceConfig 点（body/skid/target 地址）。
3. **S3 校验** —— 当前线 id/边/CP/partner 校验,红叉列表面板里点击跳转定位。

**同事侧零 Python：(2026-11-03 定)** S1/S2/S3 **全部是 editor.html 内纯 JS**，同事只装浏览器
（Chrome/Edge 最佳,Firefox 降级到下载）即可用,解压 `layout_editor` zip 直接开 `editor.html`,**不需要 Python 环境**。
打包发给同事的素材只需 `editor.html`（+自用样例可选）；`graph.py`/`tags.py`/`preview.html` 是**整合方侧**权威校验/点位/预览，
不进同事打包件。交付物单一：`Line_x.json`(spec)，最终点位权威仍在整合方 tags.py（见下）。

**交付物给整合方：`Line_x.json`(spec)一个文件即可。**
**点位权威约定（关键,A 方案）：**
- **`tags.py` 是唯一权威**。同事 S2 生成的 json 只作画图时的即时预览,`deviceConfig\` 最终点位一律由**整合方(我)用 `tags.py` 对收到的 spec 重跑生成**,不采信同事 S2 的落盘结果。
- 原因:editor 内嵌的是 JS 版推导,与 tags.py(Python)是两套实现,会自然漂移;收敛到 tags.py 一处保证最终点位无错配。
- 因此同事改推导无关 S2 的落盘,只需把 spec 交来即可；"该线有没有 ILO/非标段"这类确证由整合方在 tags.py 重跑时可见。

## 跨线整合（整合方做,非同事）
- 收到各线 spec 后,按「出口 EXT → 对端入口 ENT」在 spec 元素的 `attrs.partner` 上配严格互指;这是唯一需人工对的地方(同事各自不知他人布局,不该填)。
- `python graph.py --merge` 统一平移对齐 + 折叠接口标签 + 全厂可达性校验。