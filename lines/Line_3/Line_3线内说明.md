# Line_3 线内说明（本线专有）

> 通用机理见 `./硬件与功能_总则.md`；本线正式口径 = `Line_3输送描述.md` + `codes/config_Line_3.py`。

## Line_3 拓扑摘要（图模型 44 节点 / 42 边）
- **RB 主链**：2 条纯 RB 链，全单向（沿链"向前"），无功能件。
  - 链1：1A230 → … → 1C330（喂料自 Line_1 1F430，送 Line_4 1B075）
  - 链2：1A340 → … → 1C440（喂料自 Line_1 1F425，送 Line_4 1B080）
- **入口**：A230、A340（来自 Line_1）。
- **出口**：C330→Line_4 1B075、C440→Line_4 1B080（占位）。
- 无 LT/CP/SL/TT/TC，无过渡辊床。

## 修订记录
| 日期 | 内容 |
|---|---|
| 2026-10-18 | 首个版本：两条纯 RB 输送链；`Line_3输送描述.txt` 【1】的"双向"为模板残留，按【3】"单向"与实际喂料方向统一为单向。 |

## 待确认 / 待办
- [ ] Line_3 设备台账核对（设备号 vs 内部设备台账）。
- [ ] Line_4 线建图后接真实节点（当前 Line_4 占位）。

## 相关文件
`codes\config_Line_3.py` · `lines\Line_3\Line_3输送描述.md` · `lines\Line_3\Line_3_nodes.csv` · `Line_3_edges.csv` · 跨线接口 `./interfaces.json`（导出人类索引 `line_meta.csv`）