# -*- coding: utf-8 -*-
"""
FJSP-T 流程式布局 —— 按"车辆输送流向"给节点算确定坐标(左→右: 入口最左/出口最右)。
- 纯渲染层: 只产出 {node_id: (x_px, y_px)}, 不改图模型/config/CSV。
- **行驱动**: x = 设备在"行(泳道)"内的列序号, y = 行号。行由 config 的 layout_rows 指定
  (每行一条水平输送链, None 表示该格留空以对齐上下行)。这样能精确实现用户想要的
  跨行对齐(如 010/015、235/135、300/295、CP310 的 330/325/320 竖排)。
- config 未给 layout_rows 时, 退化为"每条链一行"。
- CP 位置椭圆贴到各自受控 LT 站(其行下方 0.5 行), 用既有的红色横移虚线把相邻站串起来。
- TT 分支出口落下一行(纸面方向随 TT 90° 改向)。
"""
import networkx as nx

COL_W, ROW_H = 82.0, 78.0     # 格距(px)（COL_W 收紧让滚床贴近; 行高保留便于塞 LT 三状态）
LT_DY = 34.0                  # 详情视图 LT 状态列沿站纵向展开偏移


def _rows_of(cfg):
    """行定义: 优先用 config.layout_rows(含 None 留空格), 否则每条链一行。"""
    rows = cfg.get('layout_rows')
    if rows is None:
        rows = cfg.get('chains', [])
    return rows


def _tc_devs(cfg):
    """tc_display 里的承载体设备号: 兼容 纯int 或 {'dev':..,'f':[..]} 两种写法。"""
    return [t['dev'] if isinstance(t, dict) else t for t in cfg.get('tc_display', [])]


def _core_ids(cfg, H):
    """该线自建的"真实"节点集合(辊床/LT/CP/TC, 含 TC402 显示节点), 不含外接占位EXT。
    用于跨线汇总时, 使真实节点位置以自建线为准、不被别线 EXT 引用覆盖(如 Line_2 的
    外接 `Line_1_1F445RB` 不能把 Line_1 真实 445 节点移走)。"""
    core = set()
    for rline in _rows_of(cfg):
        for dev in rline:
            if dev is None:
                continue
            core.add(H['rb'](dev))
            if dev in cfg.get('lt_combos', []):
                core.add(H['lth'](dev)); core.add(H['ltl'](dev))
                if dev in cfg.get('pos3', []):
                    core.add(H['ltm'](dev))
    for dev in cfg.get('pure_lt', []):                   # 纯LT存位: 停车+对位双节点
        core.add(H['ltp'](dev)); core.add(H['wt'](dev))
    for dev in cfg.get('cross_tt', []):                  # 十字脊柱(含过渡 350)
        core.add(H['rb'](dev))
    for _z, _cp, stations in cfg.get('cp_groups', []):
        for s in stations:
            if isinstance(s, int):
                core.add(H['cp'](_cp, s))
    if 'tc' in cfg:
        td = cfg['tc']['dev']
        for n, lane in cfg['tc']['docks'].items():
            core.add(H['tcp'](n, td))
        pf = cfg.get('prefix', '')
        core.add(pf + '_F402TC'); core.add(pf + '_F402RB_TC_F')
    pf = cfg.get('prefix', '')
    for dev in _tc_devs(cfg):
        core.add(f"{pf}_F{dev:03d}TC"); core.add(f"{pf}_F{dev:03d}RB_TC_F")
    return core


def layout_line(cfg, H, G):
    """返回 {node_id: (x_px, y_px)}, 覆盖全部节点(rb/lth/ltm/ltl/cp/tcp/ext)。"""
    rows = _rows_of(cfg)
    col, row = {}, {}                       # 设备号 -> (列序号, 行号)
    for ri, rline in enumerate(rows):
        for ci, dev in enumerate(rline):
            if dev is not None:
                col[dev] = ci; row[dev] = ri

    # 可选"按行整列平移": col_shift = {行号: 列偏移}, 如 Line_2 链2(行4)整体左移一格 -> {4:-1}
    csh = cfg.get('col_shift', {})
    def X(d):  return col[d] + csh.get(row[d], 0)      # 该设备平移后的列号

    def P(nid, cx, cy): pos[nid] = (COL_W * cx, ROW_H * cy)
    pos = {}
    # 每台设备: 辊床 + LT 状态列
    for dev in col:
        base = row[dev]; xc = X(dev)
        P(H['rb'](dev), xc, base)
        if dev in cfg.get('lt_combos', []):
            P(H['lth'](dev), xc, base)
            P(H['ltl'](dev), xc, base + LT_DY / ROW_H)
            if dev in cfg.get('pos3', []):
                P(H['ltm'](dev), xc, base + LT_DY / (2 * ROW_H))
        if dev in cfg.get('pure_lt', []):                # 纯LT存位: 图上仅 WT 对位点(LT停车并入, 不单画)
            P(H['wt'](dev), xc, base)
    # CP 位置椭圆: 贴到各自受控 LT 站下方(跨多行的 CP 自然形成竖排, 如 310CP)
    for _z, _cp, stations in cfg.get('cp_groups', []):
        for s in stations:
            if isinstance(s, int) and s in col:
                P(H['cp'](_cp, s), X(s), row[s] + 0.55)
    # TC 虚拟停靠位(虚线滚床框): 与各自 CP 位置同水平, 不压真实RB/CP标注
    #   Forward(408): 408 前方(右)、CP@408 标注下方, 避开与 CP@408 交叠
    #   后位(410):    与 CP@410 同一水平层(row[lane]+0.55)
    if 'tc' in cfg:
        td = cfg['tc']['dev']
        for _n, lane in cfg['tc']['docks'].items():
            if lane in col:
                P(H['tcp'](_n, td), col[lane] - 1.6, row[lane] + 0.55)
        # 穿梭小车(仅显示, 不在图模型): MC 紫色框在 402 上方, TC.F 虚拟滚床在 402 下方
        if td in col:
            pf = cfg.get('prefix', '')
            P(pf + '_F402TC', col[td], row[td] - 0.6)   # 小车贴近 402RB 上方
            P(pf + '_F402RB_TC_F', col[td], row[td] + 1.0)
    # 通用 TC 承载体显示(仿 F402TC): 每个承载体滚床上方画紫色小车标记(纯显示, 不进图模型)
    pf = cfg.get('prefix', '')
    for dev in _tc_devs(cfg):
        if dev in col:
            P(f"{pf}_F{dev:03d}TC", X(dev), row[dev] - 0.6)
            # TC.F 虚滚床(纯显示, 链5侧): 放承载体下方一行(链5行), 与 TC.R(承载体, 链6行) 呼应
            P(f"{pf}_F{dev:03d}RB_TC_F", X(dev), row[dev] + 1.0)
    # 外接接口/占位: exit 在源站所在行的末尾右侧; 整行平移时右缘同步平移
    for ext, dev in cfg.get('ext_exits', []):
        if dev in row:
            last = max((c for d, c in col.items() if row[d] == row[dev]), default=col[dev])
            P(ext, last + 1 + csh.get(row[dev], 0), row[dev])
    # 入向占位(邻线喂入): 放目标辊床该行的最外侧一格以外 —— 行左端入口往左放(如 1A020/1A030 的
    #   Line_6 喂入正好落 Line_1 出口列), 行右端(如链4头 338 的 Line_4 喂入)往右放。
    for ext, dev in cfg.get('ext_inbound', []):
        if dev in col and dev in row:
            r = row[dev]
            cols = [c for d, c in col.items() if row[d] == r]
            side = 1 if col[dev] == max(cols) else -1
            P(ext, X(dev) + side, r)
    for dev, ext in cfg.get('ext_turn', []):
        if dev in col:
            P(ext, X(dev) + 1, row[dev] + 1)
    # 十字脊柱: 纵向连接RB(260/350)置于相邻两TT 的垂直几何中点(允许半行),
    #   使竖向上 250-260-340-350-355 的可达节点位于各自两TT正中(350 跨 340-355 时非整行).
    _sp = cfg.get('cross_tt', [])
    _tts = set(cfg.get('cross_tt_devs', []))
    for _i in range(1, len(_sp) - 1):
        _m = _sp[_i]
        if _m in _tts:
            continue                                   # 十字TT本体(250/340/355)不受此摆放
        _a, _b = _sp[_i - 1], _sp[_i + 1]
        _nm = H['rb'](_m)
        _na, _nb = H['rb'](_a), H['rb'](_b)
        if _na in pos and _nb in pos:
            pos[_nm] = (pos[_na][0], (pos[_na][1] + pos[_nb][1]) / 2.0)
    return pos


def layout_all(lines_output):
    """多线汇总: 同 band(缺省0) 横排(左->右), 不同 band 垂直堆叠。
    新 band 对齐到上一带"最右一线"的起始 x 下方(如 Line_2 落到 Line_3 正下方)。
    顶部预留一行给 TC 穿梭小车示意。线纵向带由 config['band'] 控制。"""
    groups = {}
    for item in lines_output:
        groups.setdefault(item[1].get('band', 0), []).append(item)
    # 真实节点归属: 某节点若由某线自建为核心(辊床/LT/CP/TC), 位置以该线为准;
    # 别线只是外接EXT引用时, 不得移动它(真实邻线节点保留自己位置)。
    owner = {}
    for _pf, cfg, H, G in lines_output:
        for nid in _core_ids(cfg, H):
            owner.setdefault(nid, _pf)
    allpos = {}
    yoff = 0.0
    prev_last_start = 0.0                        # 上一带最右线的起始x(下带对齐其下方)
    for bi in sorted(groups):
        xoff = prev_last_start
        band_maxy = 0.0
        starts = []
        for _pf, cfg, H, G in groups[bi]:
            core = _core_ids(cfg, H)
            p = layout_line(cfg, H, G)
            maxx = max((v[0] for v in p.values()), default=0)
            band_maxy = max(band_maxy, max((v[1] for v in p.values()), default=0))
            starts.append(xoff)
            dy = cfg.get('dy', 0)                # 可选每线纵向偏移(如Line_2整带上移对齐Line_1)
            for n, (x, y) in p.items():
                if n in owner and owner[n] != _pf and n not in core:
                    continue                     # 别线EXT引用真实节点: 保留自建线位置
                allpos[n] = (x + xoff, y + ROW_H + yoff + dy)
            xoff += maxx
        prev_last_start = starts[-1]
        yoff += band_maxy + ROW_H
    return allpos, xoff


def ascii_preview(cfg, H):
    """开发时肉眼概览: 画一张 行x列 的网格简图。"""
    rows = _rows_of(cfg)
    grid = {}
    for ri, rline in enumerate(rows):
        for ci, dev in enumerate(rline):
            if dev is not None:
                grid.setdefault(ci, {})[ri] = f"·{dev}·"
    out = []
    for c in sorted(grid):
        out.append(f"C{c:02d}  " + "   ".join(
            f"R{r}:{grid[c].get(r,'····'):>6}" for r in range(len(rows))))
    return "\n".join(out)