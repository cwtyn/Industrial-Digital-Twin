# -*- coding: utf-8 -*-
"""
FJSP-T 可视化生成器 — 汇总所有线的一张可交互全图(离线单文件)。
- 输出: ./conveyor_visual.html
- 功能: 线选择(Line_1/Line_3/全部); 布局切换(流程式·按输送流向 / 力导向); 主干/全部(折叠LT状态列);
        节点/边按类型着色; 双击两点 Dijkstra 求最短路径(含跨线); 悬停完整ID; 缩放拖拽; 搜索; 入口高亮。
- 流程式坐标由 codes/layout_flow.py 计算(拓扑最长路径列=输送步序, 主机=泳道行, CP独占带行, TT分支90°转向)。
- HTML 全由本脚本生成, 不手改; vis-network.min.js 内嵌, 无 CDN/服务端。
用法:  python codes/make_line_viz.py
"""
import os, sys, json, re
import networkx as nx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_line as BL
import layout_flow as LF
from common import split_node, line_of, dev_of, EXIT_CODES

# ---- 运行时快照(vehicle_state.json): 占用滚床叠加车辆信息 + 附加位信号(TC/SL/TT 位置) ----
# 新格式 {'vehicles': {node: veh}, 'positions': {node: '1'/'0'}}; 兼容旧版纯车辆映射(顶层 node->veh)。
VEHICLE = {}
POSITIONS = {}
DEC = {}
_RS_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vehicle_state.json'))
if os.path.exists(_RS_PATH):
    try:
        with open(_RS_PATH, 'r', encoding='utf-8') as _f:
            _data = json.load(_f) or {}
        if isinstance(_data, dict) and 'vehicles' in _data:
            VEHICLE = _data.get('vehicles') or {}
            POSITIONS = _data.get('positions') or {}
            DEC = _data.get('dec') or {}                  # 决策表DB: {dec_id: {'p_r_field': val}}; 仅路径推算用
        else:
            VEHICLE = _data or {}
            DEC = {}
    except Exception:
        VEHICLE = {}

VIZ_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vis-network.min.js")
if os.path.exists(VIZ_LIB):
    with open(VIZ_LIB, 'r', encoding='utf-8') as _f:
        LIB_TAG = "<script>\n" + _f.read() + "\n</script>"
else:
    LIB_TAG = '<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>'

# ---- 车型/平台字典(config/carModelPlatform.json) + 车间区域黑话(glossary.md) ----
# 两者都注入 Ollama System Prompt, 同时给前端做本地模糊匹配/统计聚合兜底(LLM 不可用时仍可用)。
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load_car_model():
    p = os.path.join(_PROJ_ROOT, 'config', 'carModelPlatform.json')
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"[warn] 读取车型字典失败 {p}: {e}")
        return {}

CAR_MODEL = _load_car_model()
MODEL_INFO = {}       # code -> {name, aliases, platform}
MODEL_ALIAS = {}      # 名称/别名/代码(大写) -> [code...]  (如 Sedan_02→VS,VH)
MODEL_ORDER = (CAR_MODEL.get('meta') or {}).get('modelOrder') or []
for _p in CAR_MODEL.get('platforms', []):
    _pc = _p.get('code', '')
    for _m in _p.get('models', []):
        _c = _m.get('code', '')
        if not _c:
            continue
        MODEL_INFO[_c] = dict(name=_m.get('name', ''), aliases=_m.get('aliases') or [], platform=_pc)
        for _t in [_c, _m.get('name', '')] + (_m.get('aliases') or []):
            if _t:
                MODEL_ALIAS.setdefault(_t.upper(), [])
                if _c not in MODEL_ALIAS[_t.upper()]:
                    MODEL_ALIAS[_t.upper()].append(_c)
# 车型代码展示顺序: 字典 modelOrder 优先, 其余按出现顺序
MODEL_SORT = [c for c in MODEL_ORDER if c in MODEL_INFO] + [c for c in MODEL_INFO if c not in MODEL_ORDER]

def _car_dict_text():
    lines = ['【平台与车型映射字典】']
    for p in CAR_MODEL.get('platforms', []):
        parts = []
        for m in p.get('models', []):
            al = m.get('aliases') or []
            parts.append('%s(%s%s)' % (m.get('code', ''), m.get('name', ''),
                                       ('，别名' + '、'.join(al)) if al else ''))
        lines.append('平台代码：%s，包含车型代码及名称：%s' % (p.get('code', ''), ', '.join(parts)))
    return '\n'.join(lines)

CAR_DICT_TEXT = _car_dict_text()

def _load_glossary():
    """glossary.md 的 `- Line_1：连廊, ...` 行 → {黑话: 区域代码}"""
    p = os.path.join(_PROJ_ROOT, 'glossary.md')
    m = {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if not s.startswith('- '):
                    continue
                mm = re.match(r'^([A-Za-z0-9_]+)\s*[：:]\s*(.+)$', s[2:])
                if not mm:
                    continue
                for t in re.split(r'[,，、]', mm.group(2)):
                    t = t.strip()
                    if t:
                        m[t] = mm.group(1)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[warn] 读取 glossary.md 失败: {e}")
    return m

AREA_ALIAS = _load_glossary()
_g = {}
for _t, _c in AREA_ALIAS.items():
    _g.setdefault(_c, []).append(_t)
AREA_GLOSSARY_TEXT = '【区域黑话映射】' + '；'.join(
    '%s→%s' % ('/'.join(ts), c) for c, ts in _g.items())

NODE_TYPE = {
    'RB':      dict(color='#9E9E9E', shape='box'),   # 辊床无车=灰; 有车在节点生成时改绿(见下)
    'LT_high': dict(color='#FFB74D', shape='box'),
    'LT_mid':  dict(color='#00BCD4', shape='box'),
    'LT_low':  dict(color='#FF8A65', shape='box'),
    'CP_pos':  dict(color='#E53935', shape='ellipse'),
    'SL':      dict(color='#26A69A', shape='box'),
    'TT':      dict(color='#5C6BC0', shape='box'),
    'TC_pos':  dict(color='#AB47BC', shape='box'),
    'WT':      dict(color='#BA68C8', shape='box'),   # 纯LT存位对位节点(CP停靠, 无订阅); 与RB同形(矩形)
    'EXT':     dict(color='#90A4AE', shape='ellipse'),
}
# 辊床可视化宽度(px): 普通可停滚床正常长宽, 过渡(不可停)滚床更短(约1/3, 个别1/4), 布局图一眼可辨。
#   尺寸取"标签文本宽度+余量", 过渡用较窄固定宽。LT 位(TT_*下)不套用, 保持各状态列紧凑。
RB_W, RB_W_TRANS, RB_W_TRANS_14 = 60.0, 44.0, 34.0
# 更短的过渡滚床(1/4): 按设备号匹配。这些在长链里是短衔接段, 缩短更贴合物理长度。
RB_TRANS_14 = {'060', '065', '070', '140', '145', '150', '155'}
# 十字脊柱 90°方向双向RB(260/350): 车间为竖直(转90°)摆布, 布局图竖放(瘦高+竖排数字), 与横向滚床成90°。
# 注意: 仅对"声明了十字脊柱"的线(当前仅 Line_4)生效; 其它线同编号设备是普通辊床, 不竖放。
VERT_RB = {'260', '350'}
EDGE_TYPE = {
    'roller': dict(color='#2E7D32', dashes=False),
    'cross':  dict(color='#E53935', dashes=True),
    'lift':   dict(color='#FB8C00', dashes=False),
    'load':   dict(color='#90A4AE', dashes=False),
    'tc':     dict(color='#8E24AA', dashes=False),
    'tt':     dict(color='#5C6BC0', dashes=True),
    'repos':  dict(color='#D500F9', dashes=True),
    'ext':    dict(color='#607D8B', dashes=True),
}

# TC402 显示重构(纯显示, 不改图模型): F402TC=穿梭小车、402RB→402RB/TC.R(Reverse位)、TC_F=虚拟Forward滚床
SHIP, FCELL = 'Line_1_F402TC', 'Line_1_F402RB_TC_F'          # 显示层独立节点ID(与图段号前缀解耦)
POS1, POS2 = 'Line_1_1F402TC_pos1', 'Line_1_1F402TC_pos2'    # 图模型真实 TCpos(段号带1前缀)
RCELL, TO408, TO410 = 'Line_1_1F402RB', 'Line_1_1F408RB', 'Line_1_1F410RB'   # 与 rb()/链一致(带1前缀)
LABEL_OVERRIDE = {RCELL: 'F402RB/TC.R', SHIP: 'F402TC', FCELL: 'F402RB/TC.F'}

def label(n, G):
    if n in LABEL_OVERRIDE:
        return LABEL_OVERRIDE[n]
    t = G.nodes[n].get('type', '')
    s = dev_of(n)
    for suf, ch in [('_high','高'),('_mid','中'),('_low','低')]:
        if s.endswith(suf): s = s[:-len(suf)] + ch; break
    if t == 'CP_pos':
        cp, at = s.split('_at_'); s = cp + '@' + (at.lstrip('0') or '0')
    if t == 'EXT': s += '·外'
    if t == 'TT': s += '·转'
    if t == 'SL': s += '·剪'
    if t == 'TC_pos':
        s = s.replace('TC_pos1','TC·前→408').replace('TC_pos2','TC·后→410')
    return s

lines = BL.available_lines()
# builds[线] = (小写config, 建图结果r, 入口列表); 并据此合成全网图 + 流程式坐标
builds = {}
for pf in lines:
    cfgm, r, entries = BL.build_line(pf)
    d = {k.lower(): v for k, v in vars(cfgm).items() if not k.startswith('_')}
    builds[pf] = (d, r, entries)
graphs = {pf: b[1]['G'] for pf, b in builds.items()}
entries_by_line = {pf: b[2] for pf, b in builds.items()}
# 各线 TC 承载体(仿 F402TC, 纯显示): RB 节点重标 "xxxRB/TC.R"(链6侧 = R), 收集小车标记
# 通则: TC 承载体滚床一律在链6行, 故其 RB 标 TC.R; 链5侧 TC.F 为下方虚滚床(见 layout_flow)。
_TC_CARRIER = []                       # [(pf, dev, rb_node_id, fc_node_id, [链5邻接node_id...])]
for _pf, (cfg, r, _e) in builds.items():
    for tc in cfg.get('tc_display', []):
        dev = tc['dev'] if isinstance(tc, dict) else tc
        fdevs = tc.get('f', []) if isinstance(tc, dict) else []
        rb_node = r['rb'](dev)
        LABEL_OVERRIDE.setdefault(rb_node, f'{dev}RB/TC.R')
        _TC_CARRIER.append((_pf, dev, rb_node, f'{_pf}_F{dev:03d}RB_TC_F', [r['rb'](f) for f in fdevs]))
# TC 位置显示格: R位 = 承载体 RB(链6, 如 Line_2_1F485RB), F位 = 虚拟载格(链5, 如 Line_2_F485RB_TC_F)。
# 这两格的颜色由位置信号(POSITIONS[格] == '1' 表示 TC 目前在该位)驱动: 在 -> 有车绿/空灰, 不在 -> 暗灰。
TC_CELLS = {RCELL, FCELL}                       # Line_1 402TC
for _pf, _dev, _rb, _fc, _fnbrs in _TC_CARRIER:
    TC_CELLS.update((_rb, _fc))
# 链5侧 carrier↔链5 真实边被 TC.F 取代(须移到 F 位才与链5通路联通): 收集待抑制边 + 待新增 TC.F↔链5 边
_TC_SKIP, _TC_FROLLER = set(), []
for _pf, dev, rb_node, fc_node, fnbrs in _TC_CARRIER:
    for fn in fnbrs:
        _TC_SKIP.update(((rb_node, fn), (fn, rb_node)))
        _TC_FROLLER.append((fc_node, fn))
# 流程式布局坐标 {node_id:(x_px,y_px)}: 按车辆输送流向(入口左/出口右)
FLOW_POS, _ = LF.layout_all([(pf, b[0], b[1], b[1]['G']) for pf, b in builds.items()])
# Line_1 402TC: 布局上 R/F 位需上下对调 (R=链6承载体, 其下游=408; F=虚拟Forward, 其下游=410)。
# 纯显示调整: 只互换两格 y 坐标, 不改图模型。
if RCELL in FLOW_POS and FCELL in FLOW_POS and FLOW_POS[RCELL][1] != FLOW_POS[FCELL][1]:
    _rx, _ry = FLOW_POS[RCELL]
    _fx, _fy = FLOW_POS[FCELL]
    FLOW_POS[RCELL] = (_rx, _fy)
    FLOW_POS[FCELL] = (_fx, _ry)

# 合成一张全网图(共享接口节点自动合并, 后建线属性优先生效)
G_all = graphs[lines[0]]
for pf in lines[1:]:
    G_all = nx.compose(G_all, graphs[pf])

def _veh_card(_v):
    """占用滚床悬停展示字段(车辆PIN/颜色/车型/平台/雪橇/目标)。六行恒显, 空/缺字段以 '-' 占位, 便于看出'该数据为空'。"""
    _pin = _v.get('pin_code') or (_v.get('body_no') or '-')[:14]
    _row = lambda k, unit='': f"{unit}{( _v.get(k) or '').strip() or '-'}"
    return "\n".join([
        "🚗 " + (_pin or '-'),
        f"颜色 {_row('color')}",
        f"车型 {_row('vehicle_type')}",
        f"平台 {_row('platform')}",
        f"雪橇 {_row('sled_no')}",
        f"目标 {_row('target')}",
    ])


# 车辆承载位置: 可停靠的 RB/LT/TT/SL/HE(与订阅清册一致), 这些节点按"空灰/有车绿"着色。
# 注意 1E355RB/1E390RB 拓扑类型是 SL, 1D135RB 等是 TT, 不能只认 type=='RB'。
VEH_TYPES = ('RB', 'LT', 'TT', 'SL', 'HE')

all_nodes, all_edges = [], []
_edgeid = 0
# 竖放仅对"拥有十字脊柱"的线(当前仅Line_4)生效; 其它线同编号的 260/350 是普通辊床, 保持横放。
_spine_lines = {pf for pf, (_cfg, _r, _e) in builds.items() if _cfg.get('cross_tt_devs')}
_spine_tt = {pf: {str(x) for x in _cfg.get('cross_tt_devs', [])} for pf, (_cfg, _r, _e) in builds.items()}
# 纯LT存位: 车挂在停车节点「…LT」, 图面只画对位节点「…WT」; 建立 WT→配对LT 映射, 供着色/取卡。
WT_PAIR = {n: n[:-2] + 'LT' for n in G_all if n.endswith('WT')}
for n, d in G_all.nodes(data=True):
    if n in (POS1, POS2):
        continue                                              # 真实TC_pos 隐藏, 改由显示层 F402TC/R/F 表达
    if d.get('type') == 'LT':
        continue                                              # 纯LT存位停车节点不单画; 图面由 WT 对位点表达
    t = d.get('type', 'other'); st = NODE_TYPE.get(t, dict(color='#777', shape='box'))
    occ = n in VEHICLE                                       # 运行时占用: 有车
    tc_cell = n in TC_CELLS                                  # TC 位置格(R/F): 颜色由位置信号驱动
    veh_pos = (not tc_cell) and t in VEH_TYPES and d.get('can_park', True)
    tc_at = tc_cell and POSITIONS.get(n) == '1'              # TC 目前在该位
    _wpk = None                                                # 纯LT对位WT: 车从配对LT节点取(见下)
    if t == 'WT':
        _wpk = WT_PAIR.get(n)
        if _wpk:                                              # WT 视为停车承载位, 占用/取卡一律用配对 LT
            occ = _wpk in VEHICLE
            veh_pos = True
    bg, border = st['color'], '#333'
    if tc_cell:                                              # TC 位置格: 在->有车绿/空灰, 不在->暗灰
        bg = '#4CAF50' if (tc_at and occ) else ('#9E9E9E' if tc_at else '#616161')
        border = '#2E7D32' if (tc_at and occ) else '#333'
    elif veh_pos:                                            # 普通承载位: 无车灰 / 有车绿
        bg = '#4CAF50' if occ else '#9E9E9E'
        if occ: border = '#2E7D32'
    node = {'id': n, 'label': label(n, G_all), 'title': f"{label(n, G_all)} · {n}", 'type': t,
            'park': bool(d.get('can_park', True)),        # 过渡辊床=False, 类别权值隔过渡 +6 用
            'color': dict(background=bg, border=border,
                          highlight=dict(background='#FFD600', border='#333')),
            'shape': st['shape'], 'font': {'size': 11}}
    # 普通可停 RB 增长固定宽; 过渡(不可停) RB 缩短(默认1/3, 指定设备 1/4), 布局图直观区分。
    # 过渡节点仅显示纯设备号(如 "140"), 窄框不裁切; 完整ID仍在悬停 title 里。
    if t == 'RB':
        import re as _re
        _num = _re.search(r'(\d{3,4})', n)
        _devno = _num.group(1) if _num else ''
        if _devno in VERT_RB and (line_of(n) in _spine_lines):
            node['heightConstraint'] = {'minimum': RB_W, 'maximum': RB_W * 1.1}
            node['widthConstraint'] = {'minimum': 22.0, 'maximum': 28.0}
            node['label'] = '\n'.join(_devno)
            node['font'] = {'size': 10}
        elif d.get('can_park', True):
            node['widthConstraint'] = {'minimum': RB_W, 'maximum': RB_W * 1.05}
        else:
            w = RB_W_TRANS_14 if _devno in RB_TRANS_14 else RB_W_TRANS
            node['widthConstraint'] = {'minimum': w, 'maximum': w}
            node['label'] = _devno or node['label']
            node['font'] = {'size': 9}
    if t == 'WT':                                           # 纯LT对位WT: 外形与"有车绿/空灰"的普通可停RB一致
        node['widthConstraint'] = {'minimum': RB_W, 'maximum': RB_W * 1.05}
    if t == 'TT' and _spine_tt.get(line_of(n)):      # 十字TT(250/340/355, type='TT'): DrPos2=90°竖放
        import re as _re_tt
        _mm = _re_tt.search(r'(\d{3,4})', n)
        _dd = _mm.group(1) if _mm else ''
        if _dd in _spine_tt[line_of(n)]:
            _mid = dev_of(n)
            _seg = _mid[:_mid.index(_dd)]
            node['_tt90'] = f"{line_of(n)}_{_seg}{_dd}TT_pos2"
            node['_num'] = _dd
            node['_baseLabel'] = node.get('label', _dd)
            if POSITIONS.get(node['_tt90']) == '1':
                node['heightConstraint'] = {'minimum': RB_W, 'maximum': RB_W * 1.1}
                node['widthConstraint'] = {'minimum': 22.0, 'maximum': 28.0}
                node['label'] = '\n'.join(_dd)
                node['font'] = {'size': 10}
            else:
                node['widthConstraint'] = {'minimum': RB_W, 'maximum': RB_W * 1.05}
    if veh_pos: node['vehpos'] = True
    if tc_cell: node['tccell'] = True
    if _wpk: node['_parkId'] = _wpk            # 纯LT对位WT: JS 实时取车用配对 LT 节点
    if occ and (veh_pos or (tc_cell and tc_at)):
        node['borderWidth'] = 2
    if n in FLOW_POS:                                   # 流程式坐标(视图中由JS决定是否采用)
        node['flow'] = [round(FLOW_POS[n][0], 1), round(FLOW_POS[n][1], 1)]
    if t == 'TC_pos':                                  # 虚拟停靠位 = 虚线滚床框
        node['borderDashes'] = [6, 3]
    if not d.get('can_park', True):
        node['borderDashes'] = [6, 3]; node['title'] += '（过渡辊床，不可停车）'
    if occ:                                            # 有车: 悬停追加车辆信息(纯LT对位WT取配对LT的车)
        _vkey = _wpk or n
        node['vehicle'] = VEHICLE[_vkey]
        # 注意: 本页 vis-network(3.33) 的 tooltip 用 innerText 渲染, HTML 标签不生效, 换行必须用 \n
        node['title'] += f"\n{_veh_card(VEHICLE[_vkey])}"
    all_nodes.append(node)
for a, b, d in G_all.edges(data=True):
    if a in (POS1, POS2) or b in (POS1, POS2):
        continue                                              # 隐藏真实TC_pos 关联边, 由显示层边替代
    if G_all.nodes[a].get('type') == 'LT' or G_all.nodes[b].get('type') == 'LT':
        continue                                              # 纯LT存位关联边: 停车LT不显示, 图上仅 WT↔CP
    if (a, b) in _TC_SKIP:
        continue                                              # 链5侧 carrier 边改由 TC.F 表达
    _edgeid += 1
    et = d.get('type', 'roller'); st = EDGE_TYPE.get(et, dict(color='#777', dashes=False))
    _a_t = G_all.nodes[a].get('type', ''); _b_t = G_all.nodes[b].get('type', '')
    if et == 'cross':
        _ti = f"色线 CP · 吊挂间传送 · {d.get('weight')}s"        # CP=LT/LT 或 LT/WT 间传送
    elif et == 'load' and ('CP_pos' in (_a_t, _b_t)):
        _ti = f"CP 交付/转运 · {d.get('weight')}s"
    else:
        _ti = f"{et} {d.get('weight')}s"
    all_edges.append({'id': _edgeid, 'from': a, 'to': b, 'weight': d.get('weight', 1),
                      'title': _ti,
                      'color': dict(color=st['color'], highlight='#FFD600'),
                      'dashes': st['dashes'], 'width': 3.0 if et == 'cross' else 1.5,
                      'arrows': {'to': {'enabled': True, 'scaleFactor': 0.5}}})
# TC 显示层补集(穿梭小车 + 虚拟Forward滚床 + 关联边), 不进图模型、只进画面
def _rnd(p):
    return [round(FLOW_POS[p][0], 1), round(FLOW_POS[p][1], 1)]
if SHIP in FLOW_POS:
    all_nodes.append({'id': SHIP, 'label': LABEL_OVERRIDE[SHIP], 'title': SHIP, 'type': 'TC_pos',
                      'color': dict(background='#AB47BC', border='#333',
                                    highlight=dict(background='#FFD600', border='#333')),
                      'shape': 'box', 'font': {'size': 11}, 'flow': _rnd(SHIP)})
    _fc_at = POSITIONS.get(FCELL) == '1'
    _fc_occ = FCELL in VEHICLE
    _fc_bg = '#4CAF50' if (_fc_at and _fc_occ) else ('#9E9E9E' if _fc_at else '#616161')
    _fc_bd = '#2E7D32' if (_fc_at and _fc_occ) else '#333'
    all_nodes.append({'id': FCELL, 'label': LABEL_OVERRIDE[FCELL],
                      'title': f"{LABEL_OVERRIDE[FCELL]} · {FCELL}", 'type': 'RB',
                      'color': dict(background=_fc_bg, border=_fc_bd,
                                    highlight=dict(background='#FFD600', border='#333')),
                      'shape': 'box', 'borderDashes': [6, 3], 'font': {'size': 11},
                      'flow': _rnd(FCELL), 'tccell': True})
    for _a, _b, _et, _dash, _wid, _ti in [
        (SHIP, RCELL, 'tc', True, 2.0, 'TC 横移到 Reverse 位'),
        (SHIP, FCELL, 'tc', True, 2.0, 'TC 横移到 Forward 位'),
        (RCELL, FCELL, 'tc', True, 1.5, 'TC 在 R/F 位间换向'),
        (RCELL, TO408, 'roller', False, 1.5, 'Reverse 位 → 408'),
        (FCELL, TO410, 'roller', False, 1.5, 'Forward 位 → 410'),
    ]:
        _edgeid += 1
        all_edges.append({'id': _edgeid, 'from': _a, 'to': _b, 'weight': 1, 'title': _ti,
                          'color': dict(color='#8E24AA' if _et == 'tc' else '#2E7D32', highlight='#FFD600'),
                          'dashes': _dash, 'width': _wid,
                          'arrows': {'to': {'enabled': True, 'scaleFactor': 0.5}}})
# 通用 TC 承载体显示(各线 tc_display, 仿 F402TC)：紫色穿梭小车 + 承载体RB标TC.R(链6) + 虚TC.F(链5) + 绑定边
# 与 F402TC 一致: ship↔R、ship→F、R↔F 换向; 且 链5侧滚床边经 TC.F 联(须移 F 位才连通), 链6侧留 TC.R。
for _pf, _dev, _rb, _fc, _fnbrs in _TC_CARRIER:
    _mid = f'{_pf}_F{_dev:03d}TC'                                  # 穿梭小车
    if _mid not in FLOW_POS or _fc not in FLOW_POS:
        continue
    _pg = dict(background='#AB47BC', border='#333', highlight=dict(background='#FFD600', border='#333'))
    _gr = dict(background='#4CAF50', border='#333', highlight=dict(background='#FFD600', border='#333'))
    all_nodes.append({'id': _mid, 'label': f'{_dev}TC', 'title': f'{_pf} {_dev} 横移车(TC承载体)',
                      'type': 'TC_pos', 'color': _pg, 'shape': 'box', 'font': {'size': 11},
                      'flow': [round(FLOW_POS[_mid][0], 1), round(FLOW_POS[_mid][1], 1)]})
    _fc_at = POSITIONS.get(_fc) == '1'
    _fc_occ = _fc in VEHICLE
    _fc_bg = '#4CAF50' if (_fc_at and _fc_occ) else ('#9E9E9E' if _fc_at else '#616161')
    _fc_bd = '#2E7D32' if (_fc_at and _fc_occ) else '#333'
    all_nodes.append({'id': _fc, 'label': f'{_dev}RB/TC.F',
                      'title': f"{f'{_dev}RB/TC.F'} · {_fc}",
                      'type': 'RB',
                      'color': dict(background=_fc_bg, border=_fc_bd,
                                    highlight=dict(background='#FFD600', border='#333')),
                      'shape': 'box', 'borderDashes': [6, 3], 'font': {'size': 11},
                      'flow': [round(FLOW_POS[_fc][0], 1), round(FLOW_POS[_fc][1], 1)],
                      'tccell': True})
    # 绑定虚线(紫): ship→R、ship→F、R↔F 换向(双向, 使两向路径都经 TC 换向可见)
    for _a, _b, _ti in [( _mid, _rb, f'{_dev}TC 移到 R 位(链6)'),
                        ( _mid, _fc, f'{_dev}TC 移到 F 位(链5)'),
                        ( _rb, _fc, f'{_dev}TC 在 R/F 位间换向(链6→链5)'),
                        ( _fc, _rb, f'{_dev}TC 在 F/R 位间换向(链5→链6)')]:
        _edgeid += 1
        all_edges.append({'id': _edgeid, 'from': _a, 'to': _b, 'weight': 1, 'title': _ti,
                          'color': dict(color='#8E24AA', highlight='#FFD600'),
                          'dashes': True, 'width': 2.0,
                          'arrows': {'to': {'enabled': True, 'scaleFactor': 0.5}}})
    # 连通实线(绿): TC.F ↔ 链5侧滚床 —— 取代被抑制的 carrier↔链5 真实边, 表达"须在 F 位才联链5"
    for _n in _fnbrs:
        for _a, _b in ((_fc, _n), (_n, _fc)):
            _edgeid += 1
            all_edges.append({'id': _edgeid, 'from': _a, 'to': _b, 'weight': 3,
                              'title': f'{_dev}TC.F ↔ {_n}', 'color': dict(color='#2E7D32', highlight='#FFD600'),
                              'dashes': False, 'width': 1.5,
                              'arrows': {'to': {'enabled': True, 'scaleFactor': 0.5}}})
# 线过滤集合 = 各线自身图的节点(含其外接占位节点, 从而边界可见)
line_ids = {pf: list(graphs[pf].nodes) for pf in lines}
line_entries = {pf: [x for x in entries_by_line[pf] if x in line_ids[pf]] for pf in lines}
# 线出口 = 该线图内无出边节点(含外接占位); 供问答 "线出口" 别名解析(如 Line_4 → Exit_1/2/3)。
line_exits = {pf: sorted(n for n in graphs[pf].nodes if graphs[pf].out_degree(n) == 0) for pf in lines}
# 十字TT 朝向现由 TT 本体节点按 DrPos2 信号动态横/竖显示(见 RB 节点的 _tt90 分支 + 前端), 不再画4臂。
if SHIP in FLOW_POS:                                        # 显示层节点纳入 Line_1 过滤集合
    for _x in (SHIP, FCELL):
        if _x not in line_ids['Line_1']:
            line_ids['Line_1'].append(_x)
for _pf, _dev, _rb, _fc, _fnbrs in _TC_CARRIER:             # 各线 TC 显示节点(小车+TC.F)纳入各自线过滤
    for _x in (f'{_pf}_F{_dev:03d}TC', _fc):
        if _x not in line_ids[_pf]:
            line_ids[_pf].append(_x)

# 决策点 DEC 标记(贴锚点旁, 不碰任何现有节点坐标; 纯展示/路径推算语义, 不入 G_all)。
def _dec_label(dp):
    return dp.get('label') or dp['id'].split('_')[-1]

def _dec_card(dp):
    L = [f"{dp.get('label') or dp['id']} · 决策点", dp.get('db', ''), f"转发自 {dp['anchor']}"]
    dm = dp.get('dest_map') or []
    if dm:
        L.append('→ ' + ' '.join(f"道{d}:{t.split('_')[-1]}" for d, t in dm))
    dec = DEC.get(dp['id'])
    if dec:
        use = sum(1 for k, v in dec.items() if k.endswith('_SB.S9999_Use') and str(v).strip() == '1')
        L.append(f"启用行 {use}(路径推算用)")
    return '\n'.join(L)

# DEC 标记避让: 在锚点邻近搜最近空位, 校验不压任何设备盒(FLOW_POS 是节点中心, L6/L8 相邻仅~82px)。
_DEC_HW, _DEC_HH = 17, 11          # DEC 盒半宽/半高
_DEV_HW, _DEV_HH = 44, 30          # 设备盒半宽/半高(略放余量, 保证不贴边)
_OCC = [(FLOW_POS[n][0], FLOW_POS[n][1]) for n in FLOW_POS]  # 所有设备节点中心(DEC 不进 FLOW_POS)

def _dec_free(cx, cy):
    return not any(abs(cx - ox) < _DEC_HW + _DEV_HW and abs(cy - oy) < _DEC_HH + _DEV_HH
                   for ox, oy in _OCC)

def _dec_place(anchor, pref, _id=''):
    # 配置给的 dx/dy 是明确意图(如"上移一个滚床高"), 首选位空就直接用, 不被 18px 搜索网格量化;
    # 被占时才在锚点邻域就近搜空位。
    ex, ey = anchor[0] + pref[0], anchor[1] + pref[1]
    if _dec_free(ex, ey):
        return (ex, ey)
    best, bkey = None, None
    for dx in range(-180, 181, 18):
        for dy in range(-90, 91, 18):
            if dx == 0 and dy == 0:
                continue
            cx, cy = anchor[0] + dx, anchor[1] + dy
            if not _dec_free(cx, cy):
                continue
            key = abs(dx - pref[0]) + abs(dy - pref[1])
            if bkey is None or key < bkey:
                bkey, best = key, (cx, cy)
    return best if best else (ex, ey)

for _pf, (cfg, _r, _e) in builds.items():
    for _dp in cfg.get('dec_points', []):
        _anc = _dp['anchor']
        if _anc not in FLOW_POS:
            print(f"[warn] DEC {_dp['id']} 锚点 {_anc} 不在布局, 跳过")
            continue
        _pref = (_dp.get('dx', 16), _dp.get('dy', -14))
        # force=True: 按 dx/dy 精确落位, 不做避让(用于人工指定的位置, 如"上/下移一个滚床高")。
        if _dp.get('force'):
            _cx, _cy = FLOW_POS[_anc][0] + _pref[0], FLOW_POS[_anc][1] + _pref[1]
        else:
            _cx, _cy = _dec_place(FLOW_POS[_anc], _pref)
        all_nodes.append({'id': _dp['id'], 'label': _dec_label(_dp),
                          'title': f"{_dec_label(_dp)} · 决策点(悬停/点按看 4页表)",
                          'type': 'DEC',
                          'flow': [round(_cx, 1), round(_cy, 1)],
                          'color': dict(background='#00E6FF', border='#00668A',
                                        highlight=dict(background='#33EEFF', border='#7CFC00')),
                          'shape': 'box', 'borderWidth': 2,
                          'font': {'size': 9, 'color': '#00121C', 'bold': True},
                          'widthConstraint': {'minimum': 34, 'maximum': 34},
                          'heightConstraint': {'minimum': 20, 'maximum': 20}})
        if _dp['id'] not in line_ids[_pf]:
            line_ids[_pf].append(_dp['id'])

# 决策点元数据 → 页面富表: DEC_MAP(道号→目标设备, 键成字符串以便 JS 查表), DEC_META(标签/db/锚点)
DEC_MAP, DEC_META = {}, {}
for _pf, (cfg, _r, _e) in builds.items():
    for _dp in cfg.get('dec_points', []):
        DEC_META[_dp['id']] = {'label': _dp.get('label') or _dp['id'].split('_')[-1],
                               'db': _dp.get('db', ''), 'anchor': _dp['anchor']}
        DEC_MAP[_dp['id']] = {str(_lane): _node for _lane, _node in (_dp.get('dest_map') or [])}

# 各线入口/出口 摘要(用于信息栏)
line_info = []
for pf in lines:
    G = graphs[pf]
    ents = entries_by_line[pf]
    jeans = [n for n in G.nodes if G.out_degree(n) == 0]
    line_info.append(
        f"<b>{pf}</b> · 节点{G.number_of_nodes()}/边{len(G.edges)} · "
        f"入口 {', '.join(dev_of(x) for x in ents)} · 出口 {', '.join(sorted(x for x in jeans))}")

# ---- 页面脚本(纯字符串, 用 __标记__ 占位, 避免 f-string 花括号冲突) ----
SCRIPT = r"""const ALL_NODES = __NODES__;
const ALL_EDGES = __EDGES__;
const LINES = __LINES__;
const EXIT_CODES = __EXIT_CODES__;
// 线/区域代号含下划线(Line_1), 节点ID必须按"已知前缀"整体剥离, 不能用 split('_')[0]。
const LINE_CODES_JS = Object.keys(LINES).sort((a, b) => b.length - a.length);
const NODE_PREFIXES = LINE_CODES_JS.concat(EXIT_CODES).sort((a, b) => b.length - a.length);
function splitNodeId(id) {
  const s = String(id || '');
  for (const lc of NODE_PREFIXES) if (s.startsWith(lc + '_')) return [lc, s.slice(lc.length + 1)];
  const m = /^((?:Line|Exit)_\d+)_/.exec(s);   // 未配置的外部线(Line_5/Line_6 等)
  if (m) return [m[1], s.slice(m[0].length)];
  const i = s.indexOf('_');
  return i < 0 ? ['', s] : [s.slice(0, i), s.slice(i + 1)];
}
const lineOfNode = id => splitNodeId(id)[0];
const devOfNode = id => splitNodeId(id)[1];
// 文字输入常被 toUpperCase() 归一, 查 LINE_ENTRY/LINE_EXIT 前需换回规范大小写。
const LINE_CANON = {};
LINE_CODES_JS.concat(EXIT_CODES).forEach(k => LINE_CANON[k.toUpperCase()] = k);
const LINE_ENTRY = __LINE_ENTRY__;
const LINE_EXIT = __LINE_EXIT__;         // 各线出口(无出边节点): Line_4 → Exit_1/Exit_2/Exit_3
let VEHICLE = __VEHICLE__;          // 实时模式下由轮询 vehicle_state.json 覆盖
let POSITIONS = __POSITIONS__;      // 附加位信号(TC位置等): node -> '1'/'0', 驱动 TC 位置格上色
let DECSTATE = __DEC_EMPTY__;       // 决策表实时值(轮询 vehicle_state.json 的 dec 段覆盖): dec_id -> {p_r_field: val}
const DEC_MAP = __DEC_MAP__;        // dec_id -> {道号: 目标设备节点}(路程推算/富表"道→设备"用, 来自 config)
const DEC_META = __DEC_META__;      // dec_id -> {label, db, anchor}
// 车间黑话(glossary.md) + 车型/平台字典(config/carModelPlatform.json): 本地模糊匹配兜底
const AREA_ALIAS = __AREA_ALIAS__;        // 黑话原词 -> 区域代码
const AREA_TERMS = __AREA_TERMS__;        // 黑话词, 长词优先(避免短词先替换)
const MODEL_INFO = __MODEL_INFO__;        // 2位车型代码 -> {name, aliases, platform}
const MODEL_ALIAS = __MODEL_ALIAS__;      // 中文名/别名/代码(大写) -> [2位代码...]
const MODEL_SORT = __MODEL_SORT__;        // 车型展示顺序(字典 modelOrder 优先)
const __Q = s => document.getElementById(s);
const LABEL = {}; ALL_NODES.forEach(n => LABEL[n.id] = n.label);
// 纯LT存位在图面显示为配对的 WT: 追踪时把车辆挂点(LT)映射回屏幕上真正可见的 WT
const VEH_DISPLAY = {}; ALL_NODES.forEach(n => { if (n._parkId) VEH_DISPLAY[n._parkId] = n.id; });
// 基础标题(有车的悬停卡片在生成时已拼入, 这里按首段还原, 便于实时刷新时重拼)
ALL_NODES.forEach(n => { n._baseTitle = String(n.title || n.id).split('\n')[0]; });

// 运行时车辆: 占用滚床编号(按线过滤归属, 只显示当前线)
function renderVehList(lineSel, fold) {
  const box = document.getElementById('vehlist');
  if (!box) return;                    // 右侧车辆面板已移除, 无面板时跳过
  const ids = (lineSel === 'ALL') ? null : LINE_SET[lineSel];
  const rows = [];
  for (const id in VEHICLE) {
    if (ids && !ids.has(id)) continue;
    const okv = VEHICLE[id];
    if (fold && LT_STATES.has((ALL_NODES.find(x => x.id === id) || {}).type)) continue;
    rows.push(`<div><b>${LABEL[id] || id}</b><br><span style="color:#FFD835">${okv.body_no || ''}</span><br>颜色 ${okv.color || '-'} · 车型 ${okv.vehicle_type || '-'}</div>`);
  }
  box.innerHTML = rows.length ? rows.join('') : '<div style="color:#777">当前无车辆停靠</div>';
}
const OUT = document.getElementById('stdout');
const out = s => OUT.innerText = s;

// 三期·类别基准表: 按边类型+端点节点类型查表(实测权值, 替代秒级自动采集). 口径2 堵塞逻辑不变。
// edge_model.json 类别值(用户实测): roller14 / tt10 / sl10 / rb_lt14 / lt_rb12 / lt_cp18 /
// cross18 / lift竖向忽略0 / 跨线按跳+过渡6; exceptions 显式特例(如 .→FS024).
let M = {};                                                    // config/edge_model.json (类别基准表, loadEdgeTime 载入)
const NTYPE = {}; ALL_NODES.forEach(n => NTYPE[n.id] = n.type || '');
const NPARK = {}; ALL_NODES.forEach(n => NPARK[n.id] = n.park !== false);   // 过渡辊床=False
const ELINE = {}; ALL_NODES.forEach(n => ELINE[n.id] = lineOfNode(n.id));
const ETYPE = {}; for (const e of ALL_EDGES) ETYPE[e.from + '→' + e.to] = e.type || 'roller';
const EKEY = (u, v) => u + '→' + v;
function edgeModel(u, v) {                                     // 边 u→v 的类别基准值(秒)
  if (!M || M.roller == null) {                                // 表未载入: 回退抽象权值
    for (const e of ALL_EDGES) if (e.from === u && e.to === v) return e.weight;
    return 1;
  }
  const k = EKEY(u, v);
  if (M.exceptions && k in M.exceptions) return M.exceptions[k];// 用户实测特例(长跳/过渡)
  const tu = NTYPE[u] || '', tv = NTYPE[v] || '';
  const xl = ELINE[u] !== ELINE[v];                            // 跨线
  const et = ETYPE[k] || 'roller';
  if (et === 'lift') return M.lift != null ? M.lift : 6;        // LT 竖向 high↔mid↔low 升降
  if (et === 'cross') return M.cross;                          // CP→CP 色线行走 18
  if (et === 'load') {
    if (tv === 'CP_pos' || tu === 'CP_pos') return M.lt_cp;    // LT⇄CP 交付/转运 18
    if (tu === 'RB' && tv === 'LT_high') return M.rb_lt;       // 上提升机 14
    if (tu === 'LT_high' && tv === 'RB') return M.lt_rb;       // 出提升机 12
    return M.rb_lt;                                            // 兜底(TC承载等)
  }
  if (tu === 'TT' || tv === 'TT') return M.tt;                 // 转台 10
  if (tu === 'SL' || tv === 'SL') return M.sl;                 // 转台 10
  if (tu === 'TC_pos' && tv === 'TC_pos') return M.tc;         // TC 横移车 R↔F 16
  if (et === 'ext') return xl ? (M.cross_line || M.roller) : M.roller;  // 跨线接口: 无过渡按跳
  if (xl) return (M.cross_line || M.roller) + ((!NPARK[u] || !NPARK[v]) ? (M.cross_penalty || 0) : 0);
  return M.roller;                                          // 同线 roller RB→RB 14
}
function baseOf(u, v) { return edgeModel(u, v); }              // 类目基准(替代原锁定中位)
function outVal(v) {                                           // 被占站 v 清空跳值 = 它下游方向边基准(推进到下一位)
  const es = ALL_EDGES.filter(e => e.from === v);
  if (!es.length) return 0;
  return baseOf(v, es[0].to);
}
function dijkstra(s, t) {
  const adj = {}; for (const e of ALL_EDGES) (adj[e.from] = adj[e.from] || []).push(e.to);
  const dist = {}, prev = {}, done = new Set(); dist[s] = 0;
  for (;;) {
    let u = null, best = Infinity;
    for (const k in dist) if (!done.has(k) && dist[k] < best) { best = dist[k]; u = k; }
    if (u == null || u === t) break;
    done.add(u);
    for (const v of (adj[u] || [])) {
      if (done.has(v)) continue;
      let w = baseOf(u, v);
      if (VEHICLE[v]) w += outVal(v);            // 口径2: 进入被占可停位 → 加它清空(推进下一跳)的等待
      if (dist[u] + w < ((v in dist) ? dist[v] : Infinity)) { dist[v] = dist[u] + w; prev[v] = u; }
    }
  }
  if (!(t in dist) || !isFinite(dist[t])) return null;
  const ns = [t]; let c = t; while (c !== s) { c = prev[c]; ns.push(c); }
  ns.reverse(); return { nodes: ns, cost: Math.round(dist[t]) };
}

const LINE_SET = Object.fromEntries(Object.entries(LINES).map(([k,v]) => [k, new Set(v)]));
const FLOW_POS = {}; ALL_NODES.forEach(n => { if (n.flow) FLOW_POS[n.id] = n.flow; });
const LT_STATES = new Set(['LT_high','LT_mid','LT_low']);
let net, NODE_DS = null, EDGE_DS = null, r0 = null, mode = 'flow', fold = true, VISIBLE_ID = new Set();
let TRACK_HL = { nodes: {}, edges: {} };   // 轨迹高亮前的原始样式, 查询新轨迹/路线前恢复
let EDGE_PAIR = null;                      // "from|to" → [edgeId], 轨迹连线查找缓存
// ---- 鹰眼追踪: 同时锁定 1~2 台车, 用悬浮光环/车号旗标随 updateLive 移动 ----
// 颜色刻意避开布局既有色: ① 霓虹绿(区别于占用绿 #4CAF50) ② 电光蓝(区别于 LT/DEC 青 #00BCD4/#00E6FF)
const TRACK_COLORS = [
  { name: '绿色', emoji: '🟢', bg: '#00FF66', fg: '#00220D', fill: 'rgba(0,255,102,.16)',
    border: '#C6FFDD', glow: 'rgba(0,255,102,.95)' },
  { name: '蓝色', emoji: '🔵', bg: '#2979FF', fg: '#FFFFFF', fill: 'rgba(41,121,255,.20)',
    border: '#D6E4FF', glow: 'rgba(41,121,255,.95)' }
];
const TRACK_LINE4_EXIT = new Set(LINE_EXIT['Line_4'] || []);   // 车到达/驶出 Line_4 出口后自动解锁(严格按实际节点 ID)
const TRACK_MISS_LIMIT = 15;     // 连续 15 次轮询(~15s)找不到才自动解锁: 给 EMOS 丢包/交接延迟留足宽限
const TRACK_LOST_AFTER = 4;      // 第 4 次起旗标半透明闪烁(提示数据延迟), 但地图上保留不删
let trackList = [];              // 标准化车号, 最多 2 个
let trackMeta = {};             // 车号 -> {colorIdx, seen, missing, exitFlag, hit}
let _trackLayer = null, _trackEls = {};
let _trackPickNode = null;      // 地图上点中的车辆承载节点(显示"追踪此车"按钮)
let _trackPickKey = '';         // 上述节点上车辆的车号(标准化)

function visible(line, fold) {
  const ids = (line === 'ALL') ? null : LINE_SET[line];
  const ok = new Set();
  for (const n of ALL_NODES) {
    if (ids && !ids.has(n.id)) continue;
    if (fold && LT_STATES.has(n.type)) continue;      // 主干视图: 折叠 LT 高/中/低状态列
    ok.add(n.id);
  }
  return ok;
}

function apply() {
  clearTrackHighlight();                                  // 重建网络/切过滤线时先撤掉轨迹高亮
  hideTrackPick();                                        // 网络重建后浮出的追踪按钮要撤掉
  if (_trackLayer) { try { _trackLayer.remove(); } catch (e) {} _trackLayer = null; }
  _trackEls = {};                                         // 网络重建后 DOM 旗标需重新挂载
  const line = document.getElementById('line').value;
  fold = document.getElementById('fold').checked;
  mode = document.getElementById('ldm').value;
  const ok = visible(line, fold);
  const vs = ALL_NODES.filter(n => ok.has(n.id));
  VISIBLE_ID = new Set(vs.map(n => n.id));                  // 记录当前可见节点, 供路径高亮只点可见的
  const es = ALL_EDGES.filter(e => ok.has(e.from) && ok.has(e.to));
  const opt = {
    background: { background: '#121212' },
    nodes: { shape: 'box', borderWidth: 1, font: { size: 10, color: '#eee' }, color: { border: '#333' } },
    edges: { smooth: { type: 'continuous' } },
    interaction: { hover: true, tooltipDelay: 120, navigationButtons: true, keyboard: true }
  };
  if (mode === 'flow') {                              // 流程式: 关闭物理, 用确定坐标(左入口/右出口)
    opt.physics = false;
    vs.forEach(n => { if (FLOW_POS[n.id]) { n.x = FLOW_POS[n.id][0]; n.y = FLOW_POS[n.id][1]; n.fixed = {x:true,y:true}; } });
  } else {                                            // 力导向: 物理折叠
    opt.physics = { solver: 'forceAtlas2Based', forceAtlas2Based: { gravitationalConstant: -46, springConstant: 0.08 },
                    stabilization: { iterations: 300 } };
  }
  NODE_DS = new vis.DataSet(vs);
  EDGE_DS = new vis.DataSet(es);
  net = new vis.Network(document.getElementById('net'), { nodes: NODE_DS, edges: EDGE_DS }, opt);
  wireNet();
  const ents = (line === 'ALL') ? Object.values(LINE_ENTRY).flat() : (LINE_ENTRY[line] || []);
  if (mode === 'flow') ents.forEach(id => net.selectNodes([id]));   // flow 无物理, 直接高亮入口
  else net.once('stabilizationIterationsDone', () => ents.forEach(id => net.selectNodes([id])));
  updateLive(VEHICLE, POSITIONS);         // 重建后套用最新车辆状态 + TC 位置信号
}

function setStats() {
  document.getElementById('stats').innerHTML =
    (mode === 'flow' ? '流程式(按输送流向)' : '力导向') + ' · ' + (fold ? '主干' : '全部') +
    ' · 节点 ' + ALL_NODES.length + ' · 边 ' + ALL_EDGES.length +
    ' · 车 ' + Object.keys(VEHICLE).length +
    (trackList.length ? ' · 鹰眼 ' + trackList.length : '') +
    ' · ' + new Date().toLocaleTimeString();
}

// 悬停卡片: vis 3.33 的 tooltip 走 innerText, 只能用换行符拼, 不能用 HTML 标签
function vehCard(v) {
  const r = k => String(v[k] || '').trim() || '-';             // 空/缺字段以 '-' 占位, 六行恒显
  const pin = v.pin_code || (v.body_no || '').slice(0, 14) || '-';
  return ['🚗 ' + pin, '颜色 ' + r('color'), '车型 ' + r('vehicle_type'),
          '平台 ' + r('platform'), '雪橇 ' + r('sled_no'),
          '目标 ' + r('target')].join('\n');                  // 车身目标地址(I1031_RBDataTarget)
}

// ---- 一期·对话式问答(纯前端本地反查, 无需大模型; 二期"路径回溯"的解析层前置) ----
const QABOX = document.getElementById('qachat');
function qaClearHint() {
  if (!QABOX) return;
  const ph = QABOX.querySelector('.qa-empty');
  if (ph) ph.remove();
}
function qa(t) {
  if (!QABOX) return null;
  qaClearHint();
  const q = (typeof escHtml === 'function') ? escHtml(t.q) : String(t.q || '');
  QABOX.insertAdjacentHTML('beforeend', '<div class="qa-q">问：' + q + '</div><div class="qa-a">' + t.a + '</div>');
  QABOX.scrollTop = QABOX.scrollHeight;
  return QABOX.lastElementChild;                     // 图表的挂载点(.qa-a)
}
// 历史统计图表: 文字回答渲染完后再初始化 ECharts, 卡片淡入(CDN 不可用则提示且不报错)
let _qaChartSeq = 0;
function renderQaChart(container, cfg) {
  if (!container || !cfg || !cfg.option) return;
  const holder = document.createElement('div');
  holder.className = 'qa-chart';
  holder.id = 'qachart_' + (++_qaChartSeq);
  container.appendChild(holder);
  setTimeout(() => {
    const el = document.getElementById(holder.id);
    if (!el) return;
    if (typeof echarts === 'undefined') {
      el.innerHTML = '<div class="qa-chart-miss">图表库未加载（需能访问 ECharts CDN）</div>';
      el.classList.add('on');
      return;
    }
    try {
      const chart = echarts.init(el, null, { renderer: 'canvas' });
      chart.setOption(cfg.option);
      el.classList.add('on');                        // 淡入
      setTimeout(() => {
        try { chart.resize(); } catch (e) {}
        if (QABOX) QABOX.scrollTop = QABOX.scrollHeight;
      }, 80);
      window.addEventListener('resize', () => { try { chart.resize(); } catch (e) {} });
      if (typeof ResizeObserver !== 'undefined')
        new ResizeObserver(() => { try { chart.resize(); } catch (e) {} }).observe(el);
    } catch (e) {
      console.warn('[图表] 渲染失败:', e);
      el.innerHTML = '<div class="qa-chart-miss">图表渲染失败</div>';
      el.classList.add('on');
    }
  }, 30);
}
const norm = s => String(s || '').replace(/[\s-]+/g, '').toUpperCase();
// 规则增强①: 口语衬词/语气词剥离 → 得到用于意图分类与 token 匹配的干净副本(不回显, 只解析)
function cleanQ(q) {
  return String(q || '')
    .replace(/[呀吗呢啊哦哟吧咯么嘛]/g, '')                                  // 语气尾词
    .replace(/帮我看看|帮我查查|帮我看下|帮我查|帮我|请问下|请问|麻烦|麻烦一下|帮忙|看看|看下|查一下|查一查|查查|现在|目前|当前|到底|究竟|一下|一会儿|请/g, '')
    .replace(/\s+/g, ' ').trim();
}
function vehIndex() {                              // 反查表: 现建(≤几百台, 开销可忽略), 保证随车辆位移保持新鲜
  const out = [];
  for (const id in VEHICLE) {
    const v = VEHICLE[id] || {};
    out.push({ node: id, label: LABEL[id] || id, v,
               body: norm(v.body_no), sled: norm(v.sled_no), pin: norm(v.pin_code) });
  }
  return out;
}
function locate(id) {                              // 高亮并聚焦某节点; 节点被线过滤(不可见)时返回 false
  if (!net || !VISIBLE_ID.has(id)) return false;
  try { net.selectNodes([id]); net.focus(id, { scale: 1.35 }); } catch (e) {}
  return true;
}
// ---- 鹰眼追踪: 车辆反查 / 指令解析 / 光环旗标 / 自动解锁 ----
function trackLookup(id) {
  const k = norm(id);
  if (!k) return null;
  let best = null, bestRank = 9;
  for (const raw in VEHICLE) {
    const v = VEHICLE[raw] || {};
    let rank = 9;
    if (norm(v.sled_no) === k) rank = 0;                 // 雪橇号优先(工人最常用)
    else if (norm(v.pin_code) === k) rank = 1;
    else if (norm(v.body_no) === k) rank = 2;
    if (rank < bestRank) {
      const node = VEH_DISPLAY[raw] || raw;
      best = { rawNode: raw, node: node, v: v, label: LABEL[node] || node };
      bestRank = rank;
      if (rank === 0) break;
    }
  }
  return best;
}
function trackNo(hit, id) {
  return String((hit && hit.v && (hit.v.sled_no || hit.v.pin_code)) || id || '');
}
function parseTrackIds(s) {
  const out = [];
  for (const m of (String(s || '').match(/[0-9][0-9A-Z]{2,16}/gi) || [])) {
    const k = norm(m);
    if (k && out.indexOf(k) < 0) out.push(k);
  }
  return out;
}
function rememberVehicle(v, fallback) {           // 记下刚查过的车, 供"跟踪这台车/它"承接
  let no = String((v && (v.sled_no || v.pin_code || v.body_no)) || fallback || '');
  const ids = parseTrackIds(no);
  if (ids.length) no = ids[0];
  if (!no) return;
  lastContext.vehicle = no;
  lastContext.vehicleKeyword = /^\d{3,17}$/.test(norm(no)) ? norm(no) : '';
}
function trackContextId() {                         // 只取上一轮真实车号, 防止把节点号当车号
  for (const x of [lastContext.vehicleKeyword, lastContext.vehicle]) {
    if (!x) continue;
    const ids = parseTrackIds(x);
    if (ids.length && /^\d{3,17}$/.test(ids[0])) return ids[0];
  }
  return '';
}
function parseTrackCommand(raw) {
  const s = String(raw || '');
  if (!s) return null;
  const trackWord = /跟踪|追踪|锁定|盯住|盯紧|鹰眼|目标锁定/.test(s);
  if (!trackWord) return null;
  const cancelWord = /取消|停止|关闭|解除|清除|别/.test(s);
  return { action: cancelWord ? 'remove' : 'add', ids: parseTrackIds(s) };
}
function nextTrackColor() {
  const used = {};
  for (const id in trackMeta) used[trackMeta[id].colorIdx] = 1;
  if (!used[0]) return 0;
  if (!used[1]) return 1;
  return -1;
}
function dropTrack(id) {
  const k = norm(id);
  trackList = trackList.filter(x => x !== k);
  delete trackMeta[k];
  const el = _trackEls[k];
  if (el && el.root && el.root.parentNode) el.root.parentNode.removeChild(el.root);
  delete _trackEls[k];
}
function trackDesc() {
  return trackList.map(id => {
    const st = trackMeta[id] || {}, c = TRACK_COLORS[st.colorIdx || 0];
    return c.emoji + ' ' + escHtml(trackNo(st.hit, id)) + '（' + c.name + '）';
  }).join('、');
}
function trackAdd(ids) {
  const added = [], already = [], missing = [], full = [];
  for (const rawId of ids) {
    const id = norm(rawId);
    if (!id) continue;
    if (trackMeta[id]) { already.push(id); continue; }
    if (trackList.length >= 2) { full.push(id); continue; }
    const hit = trackLookup(id);
    if (!hit) { missing.push(id); continue; }
    const ci = nextTrackColor();
    if (ci < 0) { full.push(id); continue; }
    trackMeta[id] = { colorIdx: ci, seen: true, missing: 0, exitFlag: false, hit: hit };
    trackList.push(id);
    added.push({ id: id, hit: hit, colorIdx: ci });
  }
  if (added.length) {
    const last = added[added.length - 1];
    lastContext.vehicle = trackNo(last.hit, last.id);
    lastContext.vehicleKeyword = lastContext.vehicle;
    lastContext.intent = 'vehicle';
    ensureTrackVisible();
    renderTrackBadges();
  }
  return { added: added, already: already, missing: missing, full: full };
}
function trackRemove(ids) {
  const removed = [];
  if (!ids || !ids.length) {
    for (const id of trackList.slice()) {
      removed.push({ id: id, hit: trackMeta[id] && trackMeta[id].hit });
      dropTrack(id);
    }
  } else {
    for (const rawId of ids) {
      const id = norm(rawId), st = trackMeta[id];
      if (!st) continue;
      removed.push({ id: id, hit: st.hit });
      dropTrack(id);
    }
  }
  renderTrackBadges();
  return removed;
}
function trackCmdReply(cmd) {
  if (cmd.action === 'remove') {
    const removed = trackRemove(cmd.ids);
    if (!removed.length) {
      return trackList.length ? '未找到要取消的追踪目标。当前追踪：' + trackDesc() + '。'
                              : '当前没有正在追踪的车辆。';
    }
    return '已取消追踪 ' + removed.map(x => '雪橇 <b>' + escHtml(trackNo(x.hit, x.id)) + '</b>').join('、') +
           '。' + (trackList.length ? '仍在追踪：' + trackDesc() + '。' : '');
  }
  if (!cmd.ids.length) return '请给出要跟踪的雪橇号，例如“跟踪雪橇 1234”。';
  const r = trackAdd(cmd.ids);
  const parts = [];
  for (const x of r.added) {
    const c = TRACK_COLORS[x.colorIdx];
    parts.push('已启动对雪橇 <b>' + escHtml(trackNo(x.hit, x.id)) + '</b> 的实时追踪（' +
               c.emoji + ' ' + c.name + '标识）。当前在 <b>' + escHtml(x.hit.rawNode) + '</b>。');
  }
  for (const id of r.already) {
    const st = trackMeta[id] || {}, c = TRACK_COLORS[st.colorIdx || 0], hit = st.hit;
    parts.push('雪橇 <b>' + escHtml(trackNo(hit, id)) + '</b> 已在追踪中（' + c.emoji + ' ' + c.name + '标识）' +
               (hit ? '，当前在 <b>' + escHtml(hit.rawNode) + '</b>。' : '。'));
  }
  for (const id of r.missing)
    parts.push('未在实时状态中找到雪橇 <b>' + escHtml(id) + '</b>，未启动追踪。');
  if (r.full.length) {
    const names = trackList.slice(0, 2).map(x => '“取消跟踪 ' + escHtml(x) + '”').join(' 或 ');
    parts.push('最多同时追踪 2 台车。当前已追踪：' + trackDesc() +
               '。请先' + names + '。');
  }
  return parts.length ? parts.join('<br>') : '未找到可追踪的目标。';
}
function handleTrackCommand(raw) {
  const cmd = parseTrackCommand(raw);
  if (!cmd) return false;
  if (!cmd.ids.length) {
    // "跟踪这台车/这辆车/它" 或只说了"跟踪": 继承上一轮查过的车辆
    const fromCtx = cmd.action === 'add' || /这台车|这辆车|这个车|该车|此车|这车|这台|那辆|那台|它|刚才那|上一辆/.test(String(raw));
    if (fromCtx) {
      const id = trackContextId();
      if (id) { cmd.ids = [id]; cmd.fromContext = true; }
    }
  }
  qa({ q: raw, a: trackCmdReply(cmd) });
  lastContext.lastQ = raw;
  if (_trackPickNode) refreshTrackPick();
  return true;
}
function ensureTrackVisible() {
  if (!net || !trackList.length) return false;
  const lineSel = document.getElementById('line');
  if (!lineSel || lineSel.value === 'ALL') return false;
  let need = false;
  for (const id of trackList) {
    const st = trackMeta[id];
    if (st && st.hit && !VISIBLE_ID.has(st.hit.node)) { need = true; break; }
  }
  if (!need) return false;
  lineSel.value = 'ALL';
  apply();                                          // 追踪目标跨出当前线过滤 → 切到全部视图
  return true;
}
function sysMsg(html) {
  if (!QABOX) return;
  qaClearHint();
  QABOX.insertAdjacentHTML('beforeend', '<div class="qa-sys">' + html + '</div>');
  QABOX.scrollTop = QABOX.scrollHeight;
}
function isLine4ExitNode(nodeId) {                 // 只认 Line_4 图上真正的出口节点; Line_1/Line_2 节点一律不算
  return !!nodeId && TRACK_LINE4_EXIT.has(nodeId) && !/^Line_[12]_/.test(nodeId);
}
function trackPoll() {
  const gone = [];
  for (const id of trackList.slice()) {
    const st = trackMeta[id];
    if (!st) continue;
    const hit = trackLookup(id);
    if (hit) {
      if (st.missing > 0) {                        // 暂时丢失后又刷出新位置 → 旗标立刻回实心
        st.recovered = true;
        try { console.log('[鹰眼追踪] 追踪信号已恢复: 雪橇 ' + trackNo(hit, id) + ' → ' + hit.rawNode); } catch (e) {}
      }
      st.hit = hit;
      st.seen = true;
      st.missing = 0;
      if (!st.exitFlag && isLine4ExitNode(hit.rawNode)) {
        st.exitFlag = true;
        sysMsg('🏁 雪橇 <b>' + escHtml(trackNo(hit, id)) +
               '</b> 已到达 Line_4 出口，离开监控区域后将自动取消追踪。');
      }
    } else if (st.seen) {
      st.missing += 1;
      if (st.missing >= TRACK_MISS_LIMIT) {        // 连续 15 次轮询(~15s)不在实时状态 → 才自动解锁
        const last = st.hit, raw = last ? last.rawNode : '';
        gone.push({ id: id, no: trackNo(last, id), rawNode: raw, exited: isLine4ExitNode(raw) });
        dropTrack(id);
      }
    }
  }
  for (const x of gone) {
    if (x.exited) {                                // 真正经过 Line_4 出口(严格按实际节点)才说"离开监控区域"
      sysMsg('📍 雪橇 <b>' + escHtml(x.no) + '</b> 已离开监控区域。');
    } else {
      sysMsg('⚠️ 雪橇 <b>' + escHtml(x.no) + '</b> 连续 15 秒无位置更新，已暂停追踪（最后位置 <b>' +
             escHtml(x.rawNode || '-') + '</b>，车辆仍在厂内可重新跟踪）。');
    }
  }
  return gone;
}
function ensureTrackLayer() {
  const host = document.getElementById('net');
  if (!host) return null;
  if (_trackLayer && _trackLayer.parentNode === host) return _trackLayer;
  _trackLayer = document.createElement('div');
  _trackLayer.id = 'trackLayer';
  host.appendChild(_trackLayer);
  return _trackLayer;
}
function makeTrackEl() {
  const root = document.createElement('div');
  root.className = 'track-item';
  root.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;display:none';
  const halo = document.createElement('div'); halo.className = 'track-halo';
  const badge = document.createElement('div'); badge.className = 'track-badge';
  root.appendChild(halo); root.appendChild(badge);
  return { root: root, halo: halo, badge: badge };
}
function renderTrackBadges() {
  const layer = ensureTrackLayer();
  if (!layer) return;
  const keep = {};
  for (const id of trackList) {
    const st = trackMeta[id];
    if (!st) continue;
    if (!st.hit) st.hit = trackLookup(id);
    if (!net || !st.hit || !VISIBLE_ID.has(st.hit.node)) {
      if (_trackEls[id]) _trackEls[id].root.style.display = 'none';
      continue;
    }
    keep[id] = 1;
    let el = _trackEls[id];
    if (!el || !el.root.parentNode) {
      el = makeTrackEl();
      layer.appendChild(el.root);
      _trackEls[id] = el;
    }
    const c = TRACK_COLORS[st.colorIdx || 0];
    el.halo.style.setProperty('--tc', c.bg);
    el.halo.style.background = c.fill;
    el.badge.style.setProperty('--tc', c.bg);
    el.badge.style.background = c.bg;
    el.badge.style.color = c.fg;
    el.badge.style.borderColor = c.border;
    const lost = st.missing >= TRACK_LOST_AFTER;
    el.badge.classList.toggle('lost', lost);      // 丢失宽限期内: 半透明闪烁, 但不删除旗标
    el.badge.textContent = c.emoji + ' ' + trackNo(st.hit, id);
    el.badge.title = lost
      ? '追踪 ' + trackNo(st.hit, id) + ' · 位置数据延迟(' + st.missing + 's)，等待信号恢复 · 最后位置 ' + st.hit.rawNode
      : '追踪 ' + trackNo(st.hit, id) + ' · ' + c.name + '标识 · ' + st.hit.rawNode;
    el.root.style.display = 'block';
  }
  for (const id in _trackEls) {
    if (!keep[id]) {
      const el = _trackEls[id];
      if (el.root.parentNode) el.root.parentNode.removeChild(el.root);
      delete _trackEls[id];
    }
  }
  positionTrackBadges();
}
function _canvasToDOM(p) {
  try {
    if (net && net.canvas) {
      if (typeof net.canvas.canvasToDOM === 'function') return net.canvas.canvasToDOM(p);
      if (typeof net.canvas.coordinateToDOM === 'function') return net.canvas.coordinateToDOM(p);
    }
  } catch (e) {}
  return p;
}
function positionTrackBadges() {
  if (!net || !trackList.length) return;
  const host = document.getElementById('net');
  if (!host) return;
  const conv = p => _canvasToDOM(p);
  for (const id of trackList) {
    const st = trackMeta[id], el = _trackEls[id];
    if (!st || !el || !st.hit || !VISIBLE_ID.has(st.hit.node)) continue;
    let b = null;
    try { b = net.getBoundingBox(st.hit.node); } catch (e) {}
    if (!b) { el.root.style.display = 'none'; continue; }
    const tl = conv({ x: b.left, y: b.top }), br = conv({ x: b.right, y: b.bottom });
    if (![tl.x, tl.y, br.x, br.y].every(Number.isFinite)) { el.root.style.display = 'none'; continue; }
    const x1 = Math.min(tl.x, br.x), y1 = Math.min(tl.y, br.y);
    const x2 = Math.max(tl.x, br.x), y2 = Math.max(tl.y, br.y);
    const pad = 2;
    el.halo.style.left = (x1 - pad) + 'px';
    el.halo.style.top = (y1 - pad) + 'px';
    el.halo.style.width = Math.max(8, x2 - x1 + pad * 2) + 'px';
    el.halo.style.height = Math.max(8, y2 - y1 + pad * 2) + 'px';
    const same = trackList.filter(t => (trackMeta[t] || {}).hit && trackMeta[t].hit.node === st.hit.node);
    const off = same.length > 1 ? same.indexOf(id) * 24 : 0;
    el.badge.style.left = ((x1 + x2) / 2) + 'px';
    if (y1 < 28) {                                  // 顶部贴边 → 旗标翻到节点下方, 避免被工具条裁掉
      el.badge.classList.add('below');
      el.badge.style.top = (y2 + 8 + off) + 'px';
    } else {
      el.badge.classList.remove('below');
      el.badge.style.top = (y1 - 6 - off) + 'px';
    }
    const on = x2 > -80 && y2 > -80 && x1 < host.clientWidth + 80 && y1 < host.clientHeight + 80;
    el.root.style.display = on ? 'block' : 'none';
  }
}
// ---- 地图点选直连追踪: 点一下有车的设备 → 悬浮按钮直接锁定/解锁, 工人零输入 ----
const NODE_MAP = {}; ALL_NODES.forEach(n => NODE_MAP[n.id] = n);
function vehicleAtNode(id) {                       // 设备节点 → 当前停靠车辆(纯LT存位走配对WT)
  if (!id) return null;
  const n = NODE_MAP[id];
  if (!n || n.type === 'DEC') return null;
  const v = VEHICLE[n._parkId || n.id];
  const no = String((v && (v.sled_no || v.pin_code)) || '');
  if (!no || !/^[0-9A-Z]{3,17}$/i.test(norm(no))) return null;
  return { node: n, key: norm(no), no: no, v: v };
}
function trackPickTracked(key) {
  return trackList.indexOf(norm(key)) >= 0;
}
function ensureTrackPick() {
  const host = document.getElementById('net');
  if (!host) return null;
  let el = document.getElementById('trackpick');
  if (el && el.parentNode === host) return el;
  el = document.createElement('button');
  el.id = 'trackpick';
  el.type = 'button';
  el.style.display = 'none';
  el.addEventListener('click', e => {
    e.preventDefault(); e.stopPropagation();
    if (_trackPickNode) trackVehicleAtNode(_trackPickNode);
  });
  host.appendChild(el);
  return el;
}
function showTrackPick(nodeId) {
  const info = vehicleAtNode(nodeId);
  const el = ensureTrackPick();
  if (!el) return;
  if (!info) { hideTrackPick(); return; }
  _trackPickNode = nodeId;
  _trackPickKey = info.key;
  const on = trackPickTracked(info.key);
  el.classList.toggle('on', on);
  el.textContent = (on ? '🔓 取消追踪 ' : '🎯 追踪 ') + info.no;
  el.title = on ? '点击取消对雪橇 ' + info.no + ' 的追踪' : '点击直接追踪雪橇 ' + info.no + '(免打字)';
  el.style.display = 'block';
  positionTrackPick();
}
function hideTrackPick() {
  _trackPickNode = null;
  _trackPickKey = '';
  const el = document.getElementById('trackpick');
  if (el) el.style.display = 'none';
}
function refreshTrackPick() {                      // 追踪列表/实时状态变化后刷新按钮文字
  if (!_trackPickNode) return;
  const info = vehicleAtNode(_trackPickNode);
  if (!info) { hideTrackPick(); return; }
  showTrackPick(_trackPickNode);
}
function positionTrackPick() {
  const el = document.getElementById('trackpick');
  if (!el || !_trackPickNode || !net) return;
  if (!VISIBLE_ID.has(_trackPickNode)) { hideTrackPick(); return; }
  let b = null;
  try { b = net.getBoundingBox(_trackPickNode); } catch (e) {}
  if (!b) { el.style.display = 'none'; return; }
  const tl = _canvasToDOM({ x: b.left, y: b.top }), br = _canvasToDOM({ x: b.right, y: b.bottom });
  if (![tl.x, tl.y, br.x, br.y].every(Number.isFinite)) { el.style.display = 'none'; return; }
  const x1 = Math.min(tl.x, br.x), y1 = Math.min(tl.y, br.y);
  const x2 = Math.max(tl.x, br.x), y2 = Math.max(tl.y, br.y);
  const host = document.getElementById('net');
  el.style.left = ((x1 + x2) / 2) + 'px';
  if (y1 < 46) {                                   // 贴顶 → 翻到节点下方
    el.classList.add('below');
    el.style.top = (y2 + 10) + 'px';
  } else {
    el.classList.remove('below');
    el.style.top = (y1 - 10) + 'px';
  }
  const on = x2 > -80 && y2 > -80 && x1 < host.clientWidth + 80 && y1 < host.clientHeight + 80;
  el.style.display = on ? 'block' : 'none';
}
function trackVehicleAtNode(id) {                  // 地图按钮 → 直接调用追踪子进程, 不走输入框
  const info = vehicleAtNode(id);
  if (!info) { hideTrackPick(); return false; }
  const on = trackPickTracked(info.key);
  const cmd = { action: on ? 'remove' : 'add', ids: [info.key] };
  sysMsg(trackCmdReply(cmd));                      // 结果进对话区(不伪装成用户提问)
  lastContext.lastQ = (on ? '取消追踪 ' : '追踪 ') + info.no;
  refreshTrackPick();
  positionTrackBadges();
  return true;
}
// ---- 车辆历史轨迹 → 地图橙色高亮 ----
const TRACK_NODE_COLOR = { background: '#FF8C1A', border: '#E65100',
  highlight: { background: '#FF8C1A', border: '#E65100' },
  hover: { background: '#FFA040', border: '#E65100' } };
const TRACK_EDGE_COLOR = { color: '#FF8C1A', highlight: '#FF8C1A', hover: '#FFA040' };
function _cloneStyle(x) {
  if (x == null) return x;
  try { return JSON.parse(JSON.stringify(x)); } catch (e) { return x; }
}
function clearTrackHighlight() {
  if (NODE_DS) {
    const upd = [];
    for (const id in TRACK_HL.nodes) {
      const old = TRACK_HL.nodes[id];
      if (!NODE_DS.get(id)) continue;
      const o = { id: id };
      if (old.color != null) o.color = old.color;
      o.borderWidth = (old.borderWidth == null ? 1 : old.borderWidth);
      upd.push(o);
    }
    if (upd.length) NODE_DS.update(upd);
  }
  if (EDGE_DS) {
    const upd = [];
    for (const id in TRACK_HL.edges) {
      const old = TRACK_HL.edges[id];
      if (!EDGE_DS.get(+id)) continue;
      const o = { id: +id };
      if (old.color != null) o.color = old.color;
      if (old.width != null) o.width = old.width;
      upd.push(o);
    }
    if (upd.length) EDGE_DS.update(upd);
  }
  TRACK_HL = { nodes: {}, edges: {} };
  if (net) { try { net.unselectAll(); } catch (e) {} }
}
function _edgePairMap() {
  if (!EDGE_PAIR) {
    EDGE_PAIR = {};
    ALL_EDGES.forEach(e => {
      const k = e.from + '|' + e.to;
      (EDGE_PAIR[k] || (EDGE_PAIR[k] = [])).push(e.id);
    });
  }
  return EDGE_PAIR;
}
function highlightTrack(nodes) {
  if (!net || !NODE_DS || !EDGE_DS) return false;
  const ids = [];
  const seen = {};
  (nodes || []).forEach(x => {
    const id = String(x || '');
    if (id && !seen[id] && ALL_NODES.some(n => n.id === id)) { seen[id] = 1; ids.push(id); }
  });
  if (!ids.length) return false;

  // 轨迹跨线时切到“全部”，否则当前线过滤会把轨迹节点隐藏掉
  const lineSel = document.getElementById('line');
  const trackLines = Array.from(new Set(ids.map(lineOfNode)));
  if (lineSel && lineSel.value !== 'ALL' && trackLines.some(x => x !== lineSel.value)) {
    lineSel.value = 'ALL';
    apply();                                        // apply 内部会清掉旧高亮并重建可见集
  }
  clearTrackHighlight();

  const visNodes = ids.filter(id => VISIBLE_ID.has(id));
  if (!visNodes.length) return false;

  // 节点对之间优先取直连边；无直连时用最短路补出中间连线（中间节点只用于取景，不伪装成过车记录）
  const pairMap = _edgePairMap();
  const edgeIds = new Set();
  const fitIds = new Set(visNodes);
  const addPairEdges = (a, b) => {
    (pairMap[a + '|' + b] || []).forEach(eid => { if (EDGE_DS.get(eid)) edgeIds.add(eid); });
    return (pairMap[a + '|' + b] || []).length > 0;
  };
  for (let i = 1; i < ids.length; i++) {
    const a = ids[i - 1], b = ids[i];
    if (a === b) continue;
    if (!addPairEdges(a, b)) {
      const p = dijkstra(a, b);
      if (p && p.nodes && p.nodes.length > 1) {
        p.nodes.forEach(id => { if (VISIBLE_ID.has(id)) fitIds.add(id); });
        for (let k = 1; k < p.nodes.length; k++) addPairEdges(p.nodes[k - 1], p.nodes[k]);
      }
    }
  }

  const nUpd = [];
  visNodes.forEach(id => {
    const cur = NODE_DS.get(id);
    if (!cur) return;
    TRACK_HL.nodes[id] = { color: _cloneStyle(cur.color), borderWidth: cur.borderWidth };
    nUpd.push({ id: id, color: TRACK_NODE_COLOR, borderWidth: 4 });
  });
  if (nUpd.length) NODE_DS.update(nUpd);

  const eUpd = [];
  Array.from(edgeIds).forEach(eid => {
    const cur = EDGE_DS.get(eid);
    if (!cur) return;
    TRACK_HL.edges[eid] = { color: _cloneStyle(cur.color), width: cur.width };
    eUpd.push({ id: eid, color: TRACK_EDGE_COLOR, width: 4 });
  });
  if (eUpd.length) EDGE_DS.update(eUpd);

  try {
    // 必须一次同时选中节点+边: 分开调用 selectNodes/selectEdges 会互相清空对方的选择
    if (net.setSelection) net.setSelection({ nodes: visNodes, edges: Array.from(edgeIds) });
    else {
      net.selectNodes(visNodes);
      if (edgeIds.size) net.selectEdges(Array.from(edgeIds));
    }
    const fit = Array.from(fitIds).filter(id => VISIBLE_ID.has(id));
    if (fit.length) net.fit({ nodes: fit, animation: { duration: 350, easingFunction: 'easeInOutQuad' } });
    else net.fit({ animation: { duration: 350, easingFunction: 'easeInOutQuad' } });
  } catch (e) {
    try { net.fit(); } catch (e2) {}
  }
  return true;
}
function vehTrackBtn(v) {                                  // 车辆卡片上的"追踪此车"按钮(免打字)
  const no = String((v && (v.sled_no || v.pin_code)) || '');
  if (!no) return '';
  const on = trackList.indexOf(norm(no)) >= 0;
  return '<br><button type="button" class="vctrack' + (on ? ' on' : '') + '" data-no="' +
         escHtml(no) + '">' + (on ? '🔓 取消追踪 ' : '🎯 追踪 ') + escHtml(no) + '</button>';
}
const vehCardHtml = v => vehCard(v).replace(/\n/g, '<br>') + vehTrackBtn(v);
function lineNameOf(id) { return lineOfNode(id) || '-'; }
// 三期②: "A 到 B" 路线查询 —— 解析起终点(入口/节点/占位), 跑含堵塞的最优路径并高亮
function resolveNode(tokRaw) {
  const s = String(tokRaw || '').toUpperCase().replace(/[从到→\-—、]/g, ' ').trim();
  if (!s) return null;
  const canon = c => LINE_CANON[c] || c;
  let m = /^(LINE_\d+)(线|的)?\s*入口/.exec(s);              // "Line_1入口/ Line_1线入口" → 该线首入口
  if (m) return (LINE_ENTRY[canon(m[1])] || [])[0] || null;
  m = /^(LINE_\d+)(线)?$/.exec(s);                          // 裸线前缀 "Line_3 / Line_3线" → 该线首入口(作路线起点)
  if (m) return (LINE_ENTRY[canon(m[1])] || [])[0] || null;
  m = /^(LINE_\d+)[^\d]*(\d{3,})\s*$/.exec(s);             // "Line_1的015"/"Line_1 065" → 该线主站 RB
  if (m) {
    const L = canon(m[1]), pos = m[2];
    const rbs = ALL_NODES.filter(n => n.id.startsWith(L + '_') && n.id.toUpperCase().endsWith(pos + 'RB'));
    return rbs.length ? rbs[0].id : null;                 // 主链站位 RB (LT_high/low 不落, 天然唯一主链)
  }
  m = /^(LINE_\d+)\s*出口\s*(\S+)/.exec(s);                // "Line_4出口Exit_3" → Exit_3_xx 占位
  if (m) { const hits = ALL_NODES.filter(n => n.id.toUpperCase().includes(m[2])); return hits.length === 1 ? hits[0].id : null; }
  const hits = ALL_NODES.filter(n => n.id.toUpperCase() === s || devOfNode(n.id).toUpperCase() === s
                                     || norm(n.label) === s || n.id.toUpperCase().includes(s));
  return hits.length === 1 ? hits[0].id : null;           // 模糊仅唯一命中才认(歧义交普通查询)
}
// 高亮链格式化: 连续的色线 CP 段折成 "CP·吊挂间传送[…]"(CP=LT/LT 或 LT/WT 间传送), 其余照常
function fmtChain(nodes) {
  const parts = [];
  let i = 0;
  while (i < nodes.length) {
    const tp = NTYPE[nodes[i]] || '';
    if (tp === 'CP_pos') {
      let j = i;
      while (j < nodes.length && (NTYPE[nodes[j]] || '') === 'CP_pos') j++;
      const run = nodes.slice(i, j).map(x => LABEL[x] || x);
      parts.push('🟥CP·吊挂间传送[' + run.join('→') + ']');
      i = j;
    } else {
      parts.push(LABEL[nodes[i]] || nodes[i]);
      i++;
    }
  }
  return parts;
}
// ---- 路线规划增强: 终点别名 / 属性过滤 / 决策表剪枝 / Yen K-最短路 / 堵塞耗时 ----
// 终点别名: "Line_4出口" = 到达该线任一出口(无出边节点; Line_4 → Exit_1/Exit_2/Exit_3)
function exitAlias(tok) {
  const s = String(tok || '').toUpperCase().replace(/\s+/g, '');
  // 现场把 Line_4 的出口叫 "注蜡出口/灌蜡出口"(Exit_1/2/3 的 1A005RB)
  if (/^(?:注蜡|灌蜡)(?:线)?(?:的)?出口$/.test(s)) {
    const ex = LINE_EXIT['Line_4'] || [];
    return ex.length ? ex.slice() : null;
  }
  const m = /^(LINE_\d+)(?:线|的)?出口/.exec(s);
  if (!m) return null;
  const ex = LINE_EXIT[LINE_CANON[m[1]] || m[1]] || [];
  return ex.length ? ex.slice() : null;
}
// ---- 黑话/车型字典本地归一(LLM 不可用时也能用; LLM 结果同样过这里校验) ----
const AREA_CODES = (() => {
  const s = new Set(Object.keys(LINES));
  for (const n of ALL_NODES) {
    const p = lineOfNode(n.id);
    if (/^Exit_\d+$/.test(p)) s.add(p);         // Exit_1/2/3(注蜡线)虽不在线过滤里, 也能按区域统计
  }
  s.forEach(k => LINE_CANON[k.toUpperCase()] = k);
  return Array.from(s).sort();
})();
function reEsc(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
// 黑话 → 区域代码: 整句替换(大小写不敏感), 长词优先; 返回归一后的句子
function areaNorm(text) {
  let s = String(text || '');
  for (const term of AREA_TERMS) {
    if (!term) continue;
    s = s.replace(new RegExp(reEsc(term), 'gi'), AREA_ALIAS[term]);
  }
  return s;
}
// 车型名称/别名/代码 → 2位代码数组(边界匹配, 避免 DM 命中 DM42)
const MODEL_TERMS = Object.keys(MODEL_ALIAS).sort((a, b) => b.length - a.length);
const MODEL_REGEX = MODEL_TERMS.map(t => [t, new RegExp('(^|[^A-Z0-9])' + reEsc(t) + '($|[^A-Z0-9])')]);
function modelAsked(text) {
  const U = String(text || '').toUpperCase(), out = [];
  for (const pair of MODEL_REGEX) {
    if (!pair[1].test(U)) continue;
    for (const c of (MODEL_ALIAS[pair[0]] || [])) if (out.indexOf(c) < 0) out.push(c);
  }
  return out;
}
function modelLabel(code) {
  const m = MODEL_INFO[code];
  return (m && m.name) ? (code + '(' + m.name + ')') : code;
}
// 车辆属性(从问句动态取): 平台→Filtr05, VIN→Filtr01, 车型→Filtr04
const PLATFORMS = __PLATFORMS__;
const BODYTYPES = MODEL_SORT.slice();
function queryAttrs(qc) {
  const U = String(qc || '').toUpperCase();
  const has = w => new RegExp('(^|[^A-Z0-9])' + w + '($|[^A-Z0-9])').test(U);
  const platform = PLATFORMS.find(has) || null;
  let bodytype = BODYTYPES.find(has) || null;
  if (!bodytype) {                                     // 名称/别名: 唯一命中才作为过滤值(一个名称对应多代码时不猜)
    const ms = modelAsked(U);
    bodytype = ms.length === 1 ? ms[0] : null;
  }
  let vin = null;
  const v17 = U.match(/\b[A-HJ-NPR-Z0-9]{17}\b/);        // 标准 VIN(不含 I/O/Q)
  if (v17) vin = v17[0];
  else {
    const cand = (U.match(/[A-Z0-9]{9,}/g) || []).filter(x => !/^(?:LINE|EXIT)_\d+$/.test(x));
    vin = cand.length ? cand[0] : null;                    // 车身号/长码
  }
  return { platform: platform, bodytype: bodytype, vin: vin };
}
// ---- 多轮对话上下文: 代词/省略承接上一轮(区域/平台/路线起终点) ----
let lastContext = { intent: '', area: '', platform: '', routeStart: '', routeEnd: '',
                    routeFrom: '', routeTo: '', routeCost: null,
                    vehicle: '', vehicleKeyword: '', track: null, lastQ: '' };
const PRONOUN_RE = /这|那|它|它们|他们|上面说的|刚才|前面|上述|该区域|该线|这条|那条|这里|那里/;
function hasFollowRef(q) { return PRONOUN_RE.test(String(q || '')); }
// "这113台的车型分布": 113 是上一轮答案的台数, 只作话头/实时变化的对照, 绝不当过滤条件
function followCount(q) {
  const m = /(?:这|那|上述|刚才(?:的)?|前面(?:说的)?)\s*([0-9]{1,4})\s*[台辆]/.exec(String(q || ''));
  return m ? +m[1] : 0;
}
// 路线词优先于统计: "出到注蜡2线走哪条路线最优" 即使带区域/数量也是 route
const ROUTE_RE = /路线|路径|怎么走|如何走|走哪|哪条道|哪条线|怎么出去|如何出去|通行/;
function wantsRoute(q) {
  const s = String(q || '');
  if (ROUTE_RE.test(s)) return true;
  return /最优|最佳/.test(s) && /走|道|线|通|路径/.test(s);
}
// "这辆车经过了哪些设备/路径" = 查历史轨迹, 不是路线规划。必须先于 route 词判断。
const TRACK_RE = /(?:经过|走过|路过|到过|去了?)(?:了)?(?:哪些|什么|哪几个)?(?:设备|站点|站|节点|地方|路径)|(?:历史)?轨迹|历史路径|(?:刚才|上面|前面|之前)(?:说|问|提到)?(?:的|那条)?(?:路径|轨迹)/;
function wantsTrack(q) {
  const s = String(q || '');
  return TRACK_RE.test(s) && /车|雪橇|PIN|VIN|车身|它|这|那|刚才|上面|前面|之前/.test(s);
}
function forceTrackIntent(h, q) {
  if (!h || !wantsTrack(q)) return h;
  if (['route', 'vehicle', 'count', '', 'unknown'].indexOf(h.intent) >= 0) h.intent = 'history_track';
  return h;
}
function forceRouteIntent(h, q) {
  if (!h || wantsTrack(q)) return h;                 // "经过了哪些路径"不是路线规划
  if (!wantsRoute(q)) return h;
  const s = String(q || '');
  // "哪条道最堵/停留最久" 仍是堵点查询, 不抢成路线
  if (/堵|停留|过车量|台次|统计/.test(s) && !/路线|路径|怎么走|如何走|走哪/.test(s)) return h;
  if (h.intent !== 'history_bottleneck') h.intent = 'route';
  return h;
}
// "雪橇2192到注蜡出口需要多少时间" = 从该车当前位置规划到目的地的路线(不是查车/查轨迹)
const VEH_ROUTE_TIME_RE = /(?:雪橇|牵引|托架|车身|车辆|PIN|VIN)(?:号|码)?\s*([0-9A-Z]{3,17})\s*(?:到|→|至|去)\s*([^，。？?]+)/i;
function vehicleRouteIntent(q) {
  const s = String(q || '');
  if (!/(?:多少时间|多长时间|要多久|多久|耗时|花(?:了)?多久|总共.*(?:时间|耗时))/.test(s)) return null;
  const m = VEH_ROUTE_TIME_RE.exec(s);
  if (!m) return null;
  const kw = String(m[1] || '').toUpperCase();
  const dest = String(m[2] || '')
    .replace(/(?:需要|要|得|大约|大概|约为|约|用了|花费|花|多少时间|多长时间|多久|耗时).*$/, '').trim();
  if (!kw || !dest) return null;
  const hit = vehIndex().find(x => x.sled === kw || x.pin === kw || x.body === kw);
  const v = (hit && hit.v) || {};
  return {
    kw: kw, dest: dest, node: hit ? hit.node : '', label: hit ? hit.label : '',
    platform: String(v.platform || '').toUpperCase(),
    vin: String(v.pin_code || v.body_no || '').toUpperCase(),
    bodytype: String(v.vehicle_type || '').slice(0, 2).toUpperCase()
  };
}
function vehicleNodeOf(tok) {
  const K = norm(tok).replace(/^(?:雪橇|牵引|托架|车身|车辆|PIN|VIN)+(?:号|码)?/i, '');
  if (!K) return '';
  const hit = vehIndex().find(x => x.sled === K || x.pin === K || x.body === K);
  return hit ? hit.node : '';
}
function forceVehicleRouteIntent(h, q) {
  if (!h) return h;
  const vr = vehicleRouteIntent(q);
  if (!vr) return h;
  h.intent = 'route';                        // 本地硬拦截: 小模型判成 vehicle/history_track 也拉回 route
  h.start = vr.node || '';                   // 起点 = 该车当前所在节点; 查不到则由 askSmart 明确提示
  h.end = vr.dest;
  h.targetKeyword = '';                      // 清掉车辆关键词, 防止后续误走轨迹
  h.targetArea = ''; h.node_id = '';
  h.platform = vr.platform || '';            // 用该车真实属性做决策表过滤, 不继承上一轮平台
  h.vin = vr.vin || '';
  h.bodytype = vr.bodytype || '';
  h._vehRoute = vr;
  if (!vr.node) h._vehicleMissing = vr.kw;
  return h;
}
// 省略式追问: 上一轮查轨迹/路线, 当前只问"用了多少时间/要多久/耗时"
function wantsElapsedFollowUp(q, ctx) {
  if (!ctx || (ctx.intent !== 'history_track' && ctx.intent !== 'route')) return false;
  const s = String(q || '');
  if (!/(?:多少时间|多长时间|要多久|多久|耗时|花(?:了)?多久|总共.*(?:时间|耗时))/.test(s)) return false;
  if (vehicleRouteIntent(s)) return false;             // 新车+目的地 = 新一轮 route
  if (/(?:从|由).{1,24}(?:到|→|至|去)/.test(s)) return false;   // 明写起终点 = 新一轮 route
  return true;
}
function elapsedFollowAnswer(ctx) {
  if (!ctx) return '';
  if (ctx.intent === 'history_track') {
    const t = ctx.track;
    if (!t) return '上一轮轨迹耗时数据已失效，请重新问该车“经过了哪些设备”。';
    const who = t.vehicle ? ('该车（' + escHtml(t.vehicle) + '）') : '该车';
    let s = who + '共经过 <b>' + t.nodes + '</b> 个节点，节点停留时间累加约 <b>' + fmtDur(t.sec) + '</b>';
    if (t.ongoing) s += '；末节点仍在停留，已计入 <b>' + fmtDur(t.ongoingSec) + '</b>';
    if (t.truncated) s += '（轨迹较长，仅统计已返回的前 ' + t.segs + ' 段）';
    return s + '。';
  }
  if (ctx.intent === 'route') {
    if (ctx.routeCost == null) return '上一轮路线耗时数据已失效，请重新问“从 A 到 B 要多久”。';
    const rng = (ctx.routeFrom || ctx.routeStart || '?') + ' → ' + (ctx.routeTo || ctx.routeEnd || '?');
    return '上一轮路线 <b>' + escHtml(rng) + '</b> 最优路径约 <b>' + fmtDur(ctx.routeCost) +
           '</b>（' + Math.round(Number(ctx.routeCost) || 0) + ' 秒，含堵塞等待）。';
  }
  return '';
}
// "过车量/通行量/流量趋势" = 历史累计, 小模型别判成"现在在厂多少台"
function forceHistoryCountIntent(h, q) {
  if (!h) return h;
  const s = String(q || '');
  if (!/过车量|过车台次|过车情况|通行量|车流量|流量趋势|过车趋势/.test(s)) return h;
  if (['count', 'vehicle', '', 'unknown'].indexOf(h.intent) >= 0) h.intent = 'history_count';
  else if (h.intent === 'history_node_detail' && !/停留|堵/.test(s)) h.intent = 'history_count';
  return h;
}
function globalScope(q) { return /全厂|全车间|整个车间|所有区域|各区域|全部区域|所有线|全线|全部车辆/.test(String(q || '')); }
function areaIn(text) {
  const S = areaNorm(text).toUpperCase();
  const a = AREA_CODES.find(c => new RegExp('(^|[^A-Z0-9])' + c.toUpperCase() + '($|[^A-Z0-9])').test(S));
  return a ? (LINE_CANON[a.toUpperCase()] || a) : '';
}
// 多个区域并列: "Line_2和Line_4"、"连廊和车顶储存线" → ["Line_2","Line_4"] / ["Line_1","Line_2"]
// 按出现顺序去重(黑话先经 areaNorm 归一), 供 countAns / LLM 结果校验共用
function areasIn(text) {
  let S = areaNorm(text).toUpperCase();
  // 现场常省略第二个编号: "Line_2和4" / "Line_2、4" → "Line_2和Line_4"
  S = S.replace(/([和与及、,，])\s*(\d+)(?!\d)/g, (m, sep, n) => sep + 'LINE_' + n);
  const re = /(EXIT_\d+|LINE_\d+)(?!\d)/g, out = [];
  let m;
  while ((m = re.exec(S))) {
    const a = LINE_CANON[m[1]];
    if (a && AREA_CODES.indexOf(a) >= 0 && out.indexOf(a) < 0) out.push(a);
  }
  return out;
}
// 具体设备号: 全名 Line_1_1A045RB / 短名 1A045RB / 现场省略写法 045RB
function nodeTok(q) {
  const U = String(q || '').toUpperCase().replace(/[，。、？?！!；;：:]/g, ' ');
  let m = U.match(/(?:^|[^A-Z0-9])((?:LINE|EXIT)_\d+_[A-Z0-9_]+)/);
  if (m) return m[1].replace(/_+$/, '');
  m = U.match(/(?:^|[^A-Z0-9])(\d[A-Z]\d{3}[A-Z]{0,3})(?=[^A-Z0-9]|$)/);
  if (m) return m[1];
  m = U.match(/(?:^|[^A-Z0-9])(\d{2,4}[A-Z]{2,3})(?=[^A-Z0-9]|$)/);
  return m ? m[1] : '';
}
// 区域+具体节点+过车/停留问法: 统计对象必须是节点, 不能泛化成整个区域。
function forceNodeStatIntent(h, q) {
  if (!h) return h;
  const s = String(q || ''), tok = nodeTok(s);
  if (!tok) return h;
  const hist = /过车|通行|经过|通过|流量|台次|过了\s*多少|过了\s*几|多少辆|多少台|几辆|几台/.test(s);
  const detail = /停留|堵|等多久|耗时/.test(s);
  if (!hist && !detail) return h;
  const qa = areasIn(s);
  let area = qa.length === 1 ? qa[0] : '';
  if (!area && h.targetArea && h.targetArea.indexOf(',') < 0) area = h.targetArea;
  h.node_id = (area && tok.indexOf('_') < 0) ? area + '_' + tok : tok;
  h.intent = (detail && !hist) ? 'history_node_detail' : 'history_count';
  h.targetArea = '';       // 统计范围就是 node_id; 区域只用于拼出完整节点名, 绝不当整区查询
  return h;
}
function platformIn(text) {
  const U = String(text || '').toUpperCase();
  return PLATFORMS.find(p => new RegExp('(^|[^A-Z0-9])' + p + '($|[^A-Z0-9])').test(U)) || '';
}
// 上一轮背景(给 Ollama): "用户上一句问的是：…（区域=Line_1）。"
function lastCtxBrief() {
  if (!lastContext.lastQ) return '';
  const bits = [];
  if (lastContext.area) bits.push('区域=' + lastContext.area);
  if (lastContext.platform) bits.push('平台=' + lastContext.platform);
  if (lastContext.routeStart || lastContext.routeEnd)
    bits.push('路线=' + (lastContext.routeStart || '?') + '→' + (lastContext.routeEnd || '?'));
  if (lastContext.vehicle || lastContext.vehicleKeyword)
    bits.push('车辆=' + (lastContext.vehicleKeyword || lastContext.vehicle));
  return '用户上一句问的是：' + lastContext.lastQ +
         (bits.length ? '（当时上下文: ' + bits.join('、') + '）' : '') + '。';
}
// 句子里明写的起终点(只抽明写, 不含上一轮): "出到注蜡2线，走哪条路线最优" → 注蜡2线
function _routeTok(s) {
  return String(s || '').trim()
    .replace(/(?:怎么走|如何走|走哪.*|怎么出去|如何出去|通行|最优.*|最佳.*|呢|吗|啊)$/, '')
    .trim();
}
function explicitRoute(q) {
  const s = String(q || ''), h = { start: '', end: '' };
  let m = /(?:从|由)\s*([^到→至，。？]+?)\s*(?:到|→|至|去)/.exec(s);
  if (m) h.start = _routeTok(m[1]);
  m = /(?:到|→|至|去)\s*([^，。？]+)/.exec(s);           // 逗号前的目的物整段取, 再剥路线尾巴
  if (m) h.end = _routeTok(m[1]);
  return h;
}
// 追问句里显式起终点优先, 缺的用上一轮补: "那从Line_3入口到Line_4出口呢"
function ctxRouteHint(q) {
  const h = { start: lastContext.routeStart || '', end: lastContext.routeEnd || '' };
  const ex = explicitRoute(q);
  if (ex.start) h.start = ex.start;
  if (ex.end) h.end = ex.end;
  return h;
}
// LLM 结果补上下文: 句中显式值 > 上一轮; 全厂/各区域/各平台不复用
function fillHintContext(h, q) {
  if (!h) return h;
  h = forceTrackIntent(h, q);                            // "这辆车经过了哪些路径" 归轨迹
  h = forceVehicleRouteIntent(h, q);                     // "雪橇xxx到某出口要多久" = 从车当前位置规划路线
  h = forceRouteIntent(h, q);                            // 路线词硬优先, 防小模型判成 count
  h = forceNodeStatIntent(h, q);                         // 区域+具体节点 → 只统计该节点
  h = forceHistoryCountIntent(h, q);                     // "过车量/趋势" 归历史累计
  const qAreas = areasIn(q), qa = qAreas.join(','),
        qp = platformIn(q), gs = globalScope(q);
  const areaCtx = (!gs && lastContext.area) ? lastContext.area : '';
  const noPlatCtx = /各平台|所有平台|全部平台|任意平台|不限平台|不考虑平台|平台分布/.test(q);
  const platCtx = (!gs && !noPlatCtx && lastContext.platform) ? lastContext.platform : '';
  // 这些是“省略区域”的分布问法: 本句没点名区域时, 一律用上一轮区域, 模型若擅自填了区域也丢掉
  const omitAreaDist = /分布情况|分布如何|车型分布|平台分布|颜色分布|怎么分|如何分|各(?:车型|平台|颜色)(?:数量|多少)|按(?:车型|平台|颜色)/.test(q);
  // "这16台车型分布": 模型若把数量误判成 vehicle 关键词 → 纠正为统计
  if (h.intent === 'vehicle' && hasFollowRef(q) && /台|辆|车型|平台|颜色|分布|多少/.test(q) &&
      /^\d{1,4}$/.test(String(h.targetKeyword || ''))) { h.intent = 'count'; h.targetKeyword = ''; }
  if (h.intent === 'count') {
    if (qa) h.targetArea = qa;                                  // 本句点名区域 → 以本句为准
    else if (omitAreaDist) h.targetArea = areaCtx;              // 分布/怎么分等省略句: 无条件继承上下文区域
    else if (areaCtx && (!h.targetArea || hasFollowRef(q))) h.targetArea = areaCtx;
    if (qp) h.platform = qp;
    else if (platCtx && !h.platform) h.platform = platCtx;
  } else if (h.intent === 'route') {
    if (h._vehRoute) {                                  // 车→目的地: 起点/终点/属性已由本地硬解析, 不继承上一轮平台
      if (!h.platform && qp) h.platform = qp;
    } else {
      const ex = explicitRoute(q), ch = ctxRouteHint(q);
      if (ex.start) h.start = ex.start;                  // 句中明写 > 模型抽取 > 上一轮
      if (ex.end) h.end = ex.end;
      if (!h.start) h.start = ch.start;
      if (!h.end) h.end = ch.end;
      // "这些车出到注蜡2线": 起点的上下文 = 上一轮统计的区域入口
      if (!ex.start && areaCtx && areaCtx.indexOf(',') < 0 &&
          /这些车|这批车|那批车|它们/.test(q)) h.start = areaCtx;   // 代词的起点=上一轮区域, 压过模型臆测
      if (qp) h.platform = qp;
      else if (platCtx && !h.platform) h.platform = platCtx;
    }
  } else if (h.intent === 'history_count' || h.intent === 'history_node_detail') {
    if (h.node_id) h.targetArea = '';                    // 精确节点查询: 统计范围就是节点
    else if (qa) h.targetArea = qa;
    else if (areaCtx && (!h.targetArea || hasFollowRef(q))) h.targetArea = areaCtx;
  } else if (h.intent === 'history_track' || h.intent === 'vehicle') {
    const vctx = lastContext.vehicleKeyword || lastContext.vehicle;
    const kwValid = /^[0-9A-Z_]{3,20}$/.test(String(h.targetKeyword || ''));
    if (vctx && hasFollowRef(q) && !kwValid) h.targetKeyword = vctx;   // "这辆车/它"承接上一轮车辆号
  }
  return h;
}
// 决策表实时行: DECSTATE[id] 的 {p_r_field: val} → [{p,r,字段:val}]
function decRowsOf(id) {
  const cells = DECSTATE[id] || {}, rows = {};
  for (const k in cells) {
    const i = k.indexOf('_'); if (i < 0) continue;
    const j = k.indexOf('_', i + 1); if (j < 0) continue;
    const key = k.slice(0, j), f = k.slice(j + 1);
    (rows[key] = rows[key] || {})[f] = String(cells[k] == null ? '' : cells[k]).trim();
  }
  return Object.values(rows);
}
const DEC_BY_ANCHOR = (() => { const m = {}; for (const id in DEC_META) { const a = DEC_META[id].anchor; (m[a] = m[a] || []).push(id); } return m; })();
// 道号目标 → 图节点集合(exact / LT状态变体 / 同站兜底)
function decTargetNodes(t) {
  if (!t) return [];
  const ids = ALL_NODES.map(n => n.id);
  if (ids.indexOf(t) >= 0) return [t];
  const v1 = ids.filter(x => x.indexOf(t + '_') === 0);
  if (v1.length) return v1;
  const m = /^(.*?)(LT|RB|WT|TT|SL|HE)$/.exec(t);
  if (m) { const v2 = ids.filter(x => x.indexOf(m[1]) === 0 && x !== m[1]); if (v2.length) return v2; }
  return [];
}
// 某 DEC 对给定属性的放行规则: {prune, lanes, targets} ; null = 不约束
// 行匹配语义: 过滤列(Filtr01/04/05)为空 = 通配; 非空则须与已知属性一致。Use=1 为生效行,
// 生效行里有的道才可走; 命中行全为 Use!=1 → 该点禁行(剪枝); 过滤后无任何命中行 → 降级不剪(防全断)。
let _decRuleCache = {};
function decRule(decId, attrs) {
  const ck = decId + '|' + (attrs.platform || '') + '|' + (attrs.vin || '') + '|' + (attrs.bodytype || '');
  if (ck in _decRuleCache) return _decRuleCache[ck];
  const rows = decRowsOf(decId);
  let res = null;
  if (rows.length && (attrs.platform || attrs.vin || attrs.bodytype)) {
    const hit = (f, col, val) => { const fv = String(f[col] || '').trim(); return !fv || (!!val && fv === val); };
    let cand = rows.filter(f => hit(f, 'AS.I9999_Filtr05', attrs.platform)
                            && hit(f, 'AS.I9999_Filtr04', attrs.bodytype)
                            && hit(f, 'AS.I9999_Filtr01', attrs.vin));
    if (!cand.length) {                            // VIN/车型无命中 → 降级为只按平台(平台也无 → 不约束)
      cand = rows.filter(f => hit(f, 'AS.I9999_Filtr05', attrs.platform));
      if (!cand.length) cand = null;
    }
    if (cand) {
      const en = cand.filter(f => f['SB.S9999_Use'] === '1');
      if (!en.length) res = { prune: true, lanes: [], targets: [] };      // 命中行全禁用 → 剪
      else {
        const lanes = [];
        for (const f of en) { const d = (f['AS.I9999_Dest'] || '').trim(); if (d && d !== '0' && lanes.indexOf(d) < 0) lanes.push(d); }
        const targets = [];
        for (const d of lanes) {
          const t = (DEC_MAP[decId] || {})[d];
          for (const n of decTargetNodes(t)) if (targets.indexOf(n) < 0) targets.push(n);
        }
        res = (lanes.length && targets.length) ? { prune: false, lanes: lanes, targets: targets } : null;
      }
    }
  }
  _decRuleCache[ck] = res;
  return res;
}
// 图邻接(按查询现建一次)
let _ROUTE_ADJ = null, _OUT_IDS = null;
function routeAdj() {
  if (_ROUTE_ADJ) return _ROUTE_ADJ;
  _ROUTE_ADJ = {}; _OUT_IDS = {};
  for (const e of ALL_EDGES) {
    (_ROUTE_ADJ[e.from] = _ROUTE_ADJ[e.from] || []).push(e.to);
    (_OUT_IDS[e.from] = _OUT_IDS[e.from] || new Set()).add(e.to);
  }
  return _ROUTE_ADJ;
}
// s 出发 ≤cap 步内能否到 targets(不回 anchor)
function reachTarget(s, targets, anchor, cap) {
  if (targets.has(s)) return true;
  if (cap <= 0) return false;
  routeAdj();
  const seen = {}; seen[s] = 1; seen[anchor] = 1;
  const q = [[s, 0]];
  while (q.length) {
    const it = q.shift(), u = it[0], d = it[1];
    if (d >= cap) continue;
    for (const v of (_ROUTE_ADJ[u] || [])) {
      if (seen[v]) continue;
      if (targets.has(v)) return true;
      seen[v] = 1; q.push([v, d + 1]);
    }
  }
  return false;
}
let _succCache = {};
// DEC 锚点放行的直接后继集合; null=不约束; {prune:true}=该点禁止通行
function allowedFor(anchor, attrs) {
  const ck = anchor + '|' + (attrs.platform || '') + '|' + (attrs.vin || '') + '|' + (attrs.bodytype || '');
  if (ck in _succCache) return _succCache[ck];
  const ids = DEC_BY_ANCHOR[anchor] || [];
  if (!ids.length) { _succCache[ck] = null; return null; }
  routeAdj();
  let allowed = null;
  for (const id of ids) {
    const r = decRule(id, attrs);
    if (!r) continue;
    if (r.prune) { _succCache[ck] = { prune: true, succ: new Set() }; return _succCache[ck]; }
    const ts = new Set(r.targets), ok = new Set();
    for (const s of (_OUT_IDS[anchor] || [])) if (reachTarget(s, ts, anchor, 14)) ok.add(s);
    if (allowed === null) allowed = ok;
    else { const inter = new Set(); for (const x of ok) if (allowed.has(x)) inter.add(x); allowed = inter; }
  }
  const res = (allowed === null) ? null : { prune: false, succ: allowed };
  _succCache[ck] = res;
  return res;
}
function edgeOK(u, v, attrs) {
  if (!DEC_BY_ANCHOR[u]) return true;
  const r = allowedFor(u, attrs);
  if (!r) return true;
  return !r.prune && r.succ.has(v);
}
// 堵塞等待: 目标位有车 → 该位清空时间 + 下游连续占用排队(≤5 位), 按当下 vehicle_state 计
let _waitCache = {};
function firstOutOf(u) { routeAdj(); const es = _ROUTE_ADJ[u] || []; return es.length ? es[0] : null; }
function queueWait(v) {
  let w = 0, u = v, hops = 0;
  const seen = {};
  while (VEHICLE[u] && hops < 5 && !seen[u]) {
    seen[u] = 1; w += outVal(u);
    const nx = firstOutOf(u);
    if (!nx || seen[nx]) break;
    u = nx; hops++;
  }
  return w;
}
function waitOf(v) { return (v in _waitCache) ? _waitCache[v] : (_waitCache[v] = VEHICLE[v] ? queueWait(v) : 0); }
function ecost(u, v) { return baseOf(u, v) + waitOf(v); }
function pathCost(ns) { let c = 0; for (let i = 1; i < ns.length; i++) c += ecost(ns[i - 1], ns[i]); return Math.round(c); }
// 带 DEC 剪枝 + 堵塞权重的 Dijkstra; targets 可多个(任一到达即止)
function rdij(s, targets, attrs, bannedN, bannedE) {
  const adj = routeAdj();
  const dist = {}, prev = {}, done = new Set();
  const heap = [];                                       // 二叉堆 [d, node], Yen 会跑几千次, 必须 O(E log V)
  const push = (d, u) => {
    heap.push([d, u]); let i = heap.length - 1;
    while (i > 0) { const p = (i - 1) >> 1; if (heap[p][0] <= heap[i][0]) break; const t = heap[p]; heap[p] = heap[i]; heap[i] = t; i = p; }
  };
  const pop = () => {
    const top = heap[0], last = heap.pop();
    if (heap.length) {
      heap[0] = last; let i = 0;
      for (;;) { let l = i * 2 + 1, r = l + 1, m = i;
        if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
        if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
        if (m === i) break; const t = heap[m]; heap[m] = heap[i]; heap[i] = t; i = m; }
    }
    return top;
  };
  dist[s] = 0; push(0, s);
  while (heap.length) {
    const it = pop(), d = it[0], u = it[1];
    if (done.has(u) || d > (u in dist ? dist[u] : Infinity)) continue;
    if (targets.has(u)) {
      const ns = [u]; let c = u;
      while (c !== s) { c = prev[c]; ns.push(c); }
      ns.reverse();
      return { nodes: ns, cost: d };
    }
    done.add(u);
    for (const v of (adj[u] || [])) {
      if (done.has(v) || bannedN.has(v) || bannedE.has(u + '>' + v)) continue;
      if (!edgeOK(u, v, attrs)) continue;
      const w = d + ecost(u, v);
      if (w < (v in dist ? dist[v] : Infinity)) { dist[v] = w; prev[v] = u; push(w, v); }
    }
  }
  return null;
}
// Yen K-最短路(无环), 权重含当下堵塞等待; DEC 放行在边过滤里生效
function yenK(s, targets, attrs, K) {
  const first = rdij(s, targets, attrs, new Set(), new Set());
  if (!first) return [];
  const A = [first], B = [];
  const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  const seenPath = ns => A.some(p => same(p.nodes, ns)) || B.some(p => same(p.nodes, ns));
  for (let k = 1; k < K; k++) {
    const prev = A[k - 1].nodes;
    for (let i = 0; i < prev.length - 1; i++) {
      const root = prev.slice(0, i + 1), spur = prev[i];
      const bannedN = new Set(root.slice(0, -1)), bannedE = new Set();
      for (const p of A) {
        if (p.nodes.length > i && root.every((x, j) => p.nodes[j] === x)) bannedE.add(p.nodes[i] + '>' + p.nodes[i + 1]);
      }
      if (bannedN.has(spur)) continue;
      const sp = rdij(spur, targets, attrs, bannedN, bannedE);
      if (!sp) continue;
      const ns = root.slice(0, -1).concat(sp.nodes);
      if (seenPath(ns)) continue;
      B.push({ nodes: ns, cost: pathCost(ns) });
    }
    if (!B.length) break;
    B.sort((a, b) => a.cost - b.cost);
    A.push(B.shift());
  }
  return A;
}
// 路径经过的决策点/道号
function pathDecInfo(nodes, attrs) {
  const info = [];
  for (let i = 0; i + 1 < nodes.length; i++) {
    const u = nodes[i], ids = DEC_BY_ANCHOR[u];
    if (!ids || !ids.length) continue;
    for (const id of ids) {
      const r = decRule(id, attrs);
      if (!r) continue;
      const nm = lineOfNode(id) + ' ' + ((DEC_META[id] || {}).label || id);   // 各线有同名 045DEC, 带线名前缀区分
      if (r.prune) { info.push(nm + '=禁用'); continue; }
      let lane = '—';
      for (const d of r.lanes) {
        const ts = new Set(decTargetNodes((DEC_MAP[id] || {})[d]));
        if (ts.size && nodes.slice(i + 1).some(x => ts.has(x))) { lane = d; break; }
      }
      if (lane === '—' && r.lanes.length) lane = r.lanes[0];
      const tg = lane !== '—' ? ((DEC_MAP[id] || {})[lane] || '') : '';
      info.push(nm + '→道' + lane + (tg ? '(' + String(tg).split('_').pop() + ')' : ''));
    }
  }
  return info;
}
// hint(可选): 本地 Ollama 解析出的 {start,end,platform,vin,bodytype}; 有值优先于问句正则。
// out(可选): 传入对象则回填结构化摘要 {from,to,total,paths:[{cost,stations,dec,nodes}]}, 供 Ollama 汇总用。
function routeAns(q, hint, out) {
  clearTrackHighlight();                              // 新路线查询先撤掉上一次轨迹橙色高亮
  _decRuleCache = {}; _succCache = {}; _waitCache = {};  // 每次查询按当下 DECSTATE / vehicle_state 重算, 防陈旧
  q = areaNorm(q);                                      // 黑话起点/终点: "连廊→注蜡入口" → "Line_1→Line_4"
  const parts = q.split(/\s*(?:到|→|至|去|怎么走|->|-|—|。|,|，)\s*/)
            .map(p => String(p || '').replace(/要|大约|大概|需要|花费|花|约为|多久|多长时间|多少时间/g, ''))
            .filter(Boolean);
  const hasHint = !!(hint && (hint.start || hint.end));
  if (parts.length < 2 && !hasHint) return null;
  const attrs = queryAttrs(q);
  if (hint) {                                            // LLM 给出的属性覆盖正则结果, 但只认合法枚举值
    if (hint.platform) attrs.platform = hint.platform;
    if (hint.vin) attrs.vin = hint.vin;
    if (hint.bodytype) attrs.bodytype = hint.bodytype;
  }
  const lineOf = id => lineOfNode(id);
  let startTok = areaNorm((hint && hint.start) || parts[0] || '');
  if (!(hint && hint.start)) {                         // 本地兜底: "雪橇2192到..."的起点 = 该车当前节点
    const vn = vehicleNodeOf(startTok);
    if (vn) startTok = vn;
  }
  const endTok = areaNorm((hint && hint.end) || parts[1] || '');
  if (out) {                                            // 存干净的起终点供多轮复用(去掉 "怎走/呢" 尾巴)
    const ct = s => String(s || '').replace(/怎[么样]*走|如何走|呢|吗|吧/g, '').trim();
    out.startTok = ct(startTok); out.endTok = ct(endTok); out.platform = attrs.platform || '';
  }
  let from = startTok ? resolveNode(startTok) : null;
  let toNodes = endTok ? exitAlias(endTok) : null;       // "Line_4出口" → Exit_1/Exit_2/Exit_3
  if (!toNodes && endTok) { const t = resolveNode(endTok); if (t) toNodes = [t]; }
  if (!toNodes && from && endTok) { const t = resolveNode(lineOf(from) + '的' + endTok); if (t) toNodes = [t]; }
  if (!from && toNodes && startTok) from = resolveNode(lineOf(toNodes[0]) + '的' + startTok);
  if (!from || !toNodes) return null;
  const targets = new Set(toNodes), K = 20;
  const paths = yenK(from, targets, attrs, K);
  if (!paths.length) {
    if (out) out.paths = [];
    return '无可行路径（已按决策表与堵塞条件剪枝）：' + (LABEL[from] || from) + ' 到 ' + (toNodes.map(lineOfNode).join('/'));
  }
  paths.sort((a, b) => a.cost - b.cost);
  const filt = [];
  if (attrs.platform) filt.push('平台 ' + attrs.platform + '(Filtr05)');
  if (attrs.vin) filt.push('VIN ' + attrs.vin + '(Filtr01)');
  if (attrs.bodytype) filt.push('车型 ' + attrs.bodytype + '(Filtr04)');
  const isAlias = !!exitAlias(endTok);
  const toText = isAlias ? toNodes.map(lineOfNode).join('/') : (LABEL[toNodes[0]] || toNodes[0]);
  const decReady = Object.keys(DECSTATE).length > 0;
  if (out) {                                             // 供 Ollama 汇总: 只取前 3 条
    out.from = LABEL[from] || from; out.to = toText; out.total = paths.length; out.capped = paths.length >= K;
    out.paths = paths.slice(0, 3).map(p => ({ cost: p.cost, stations: p.nodes.length,
                                             dec: pathDecInfo(p.nodes, attrs), nodes: p.nodes.slice() }));
  }
  let h = '起 ' + (LABEL[from] || from) + ' → 终 <b>' + toText + '</b>' +
          (filt.length ? ' · 过滤 ' + filt.join('、') : ' · 未指定平台/属性(不做决策表剪枝)') +
          (filt.length && !decReady ? '（决策表数据未就绪，暂按拓扑枚举）' : '') +
          '<br>可行路径 <b>' + paths.length + '</b> 条' + (paths.length >= K ? '（已达上限 ' + K + ' 条，可能还有更多）' : '') + '：';
  paths.forEach((p, i) => {
    const dec = pathDecInfo(p.nodes, attrs);
    const full = fmtChain(p.nodes).join(' → ');
    const short = p.nodes.length > 16 ? fmtChain(p.nodes.slice(0, 16)).join(' → ') + ' …' : full;
    h += '<div style="margin:6px 0;padding-top:5px;border-top:1px dashed #445">' +
         (i === 0 ? '<b style="color:#b7791f">★ 最优</b> ' : '• ') +
         '第 ' + (i + 1) + ' 条 · <b>约 ' + p.cost + 's</b> · ' + p.nodes.length + ' 站' +
         (dec.length ? '<br>决策: ' + dec.join(' | ') : '') +
         '<br>' + short +
         (full === short ? '' : '<details style="margin-top:3px"><summary style="cursor:pointer;color:#2a6fb0">展开全部 ' + p.nodes.length + ' 站</summary><div style="color:#555;max-height:180px;overflow:auto">' + full + '</div></details>') +
         '</div>';
  });
  try { net.selectNodes(paths[0].nodes.filter(x => VISIBLE_ID.has(x))); } catch (e) {}
  return h;
}
// 聚合统计: "缓存/区域有多少台" · "xx颜色 多少台" · "xx车型 多少台" (纯前端对 VEHICLE 现算)
// areaHint: LLM 提取的区域名(如 "Line_2"), 优先生效; 缺省时退回从问句里正则匹配
function countAns(q, areaHint, out) {
  out = out || {};
  const s = countAnsBody(q, areaHint, out);
  // "那这113台的车型分布": 不按 113 硬过滤, 按上下文区域重新统计; 台数变了就注明当前实时值
  if (out.ref && out.total && out.ref !== out.total)
    return s + '（较上一轮的 ' + out.ref + ' 台，当前实时为 ' + out.total + ' 台）';
  return s;
}
function countAnsBody(q, areaHint, out) {
  const idx = vehIndex();
  const qq = areaNorm(q).toUpperCase();               // 黑话先归一: "连廊有几台" → "Line_1有几台"
  const qAreas = areasIn(qq);
  const hintAreas = areaHint ? areasIn(areaHint) : [];
  const areas = qAreas.length ? qAreas : hintAreas;   // 本句显式区域优先; 支持 "Line_2和Line_4"/"连廊和车顶储存线"
  const asksDist = /分布|怎么分|如何分|分组|构成|占比/.test(qq);
  const dim = new RegExp('平台|底座|樋|' + PLATFORMS.join('|')).test(qq) ? ['platform', '平台']
            : /车型|型号|款|车款/.test(qq) ? ['vehicle_type', '车型']
            : /颜色|色泽|色/.test(qq) ? ['color', '颜色']
            : asksDist ? ['vehicle_type', '车型'] : null;   // 只说“分布情况”时默认按车型，不能只回总数
  if (out) { out.area = areas.join(','); out.dim = dim ? dim[0] : ''; }   // 回填本次实际统计范围(供多轮上下文)
  const pool = areas.length ? idx.filter(x => areas.indexOf(lineNameOf(x.node)) >= 0) : idx;   // 指定区域→只算这些区
  const areaN = {};
  for (const a of areas) areaN[a] = 0;
  if (areas.length) for (const x of pool) { const a = lineNameOf(x.node); if (areaN[a] != null) areaN[a]++; }
  if (out) { out.ref = followCount(qq); out.total = pool.length; }
  const fval = (x, f) => (String((x.v && x.v[f]) || '')).trim();
  const distOf = f => { const m = {}; for (const x of pool) { const k = fval(x, f); if (k) m[k] = (m[k] || 0) + 1; } return m; };
  // 实时车型是 5 位(如 DM42+): 按前 2 位聚合, 展示 "DM(车型名): 9台"
  const modelOf = x => fval(x, 'vehicle_type').slice(0, 2).toUpperCase();
  const modelDist = () => { const m = {}; for (const x of pool) { const k = modelOf(x); if (k) m[k] = (m[k] || 0) + 1; } return m; };
  const joinModel = m => {
    const keys = MODEL_SORT.filter(c => m[c]).concat(Object.keys(m).filter(c => MODEL_SORT.indexOf(c) < 0).sort());
    return keys.map(c => modelLabel(c) + ': ' + m[c] + '台').join('、') || '无';
  };
  const found = map => Object.keys(map).find(kk => kk && new RegExp('(^|[^A-Z0-9])' + kk + '($|[^A-Z0-9])').test(qq));
  const join = m => Object.keys(m).map(k => k + ' ' + m[k]).join('、') || '无';
  const scope = areas.length === 1 ? '（' + areas[0] + '）'
              : (areas.length > 1 ? '（' + areas.join(' / ') + '）' : '');
  // 多区域: "Line_2 在厂 88 台，Line_4 在厂 12 台。两区域合计共 100 台。"
  const multiHead = areas.length > 1
    ? areas.map(a => a + ' 在厂 ' + areaN[a] + ' 台').join('，') + '。' : '';
  const multiSum = areas.length > 2 ? '以上 ' + areas.length + ' 个区域' : '两区域';
  const distHead = areas.length > 1 ? multiHead + multiSum + '合计' : '';
  if (dim) {                                           // ① 维度: "颜色/车型/平台 XX" 或 "XX 各多少/分布"
    const [f, name] = dim;
    if (f === 'vehicle_type') {
      const dmap = modelDist(), asked = modelAsked(qq);
      if (asked.length) {                              // 问具体车型(可名称/别名): 名称→对应的 2 位代码
        const hit = asked.map(c => c + ':' + (dmap[c] || 0));
        const total = asked.reduce((n, c) => n + (dmap[c] || 0), 0);
        const label = asked.map(modelLabel).join('/');
        return '<b>' + label + '</b>（车型' + scope + '）在厂 ' + total + ' 台' +
               (asked.length > 1 ? '（' + hit.join('、') + '）' : '');
      }
      if (areas.length > 1)
        return distHead + '按车型分布：' + joinModel(dmap) + '（合计 ' + pool.length + ' 台）';
      return '按车型分布' + scope + '：' + joinModel(dmap) + '（合计 ' + pool.length + ' 台）';
    }
    const dmap = distOf(f), k = found(dmap);
    if (k) { const c = dmap[k] || 0; return '<b>' + k + '</b>（' + name + scope + '）在厂 ' + c + ' 台'; }
    if (areas.length > 1)
      return distHead + '按' + name + '分布：' + join(dmap) + '（合计 ' + pool.length + ' 台）';
    return '按' + name + '分布' + scope + '：' + join(dmap) + '（合计 ' + pool.length + ' 台）';
  }
  if (areas.length > 1) return multiHead + multiSum + '合计共 ' + pool.length + ' 台。';   // ② 多区域合计
  if (areas.length === 1) { return '输送区域 <b>' + areas[0] + '</b> 在厂车辆 ' + pool.length + ' 台'; }
  // ③ 全厂总览
  const perArea = {}; for (const x of idx) perArea[lineNameOf(x.node)] = (perArea[lineNameOf(x.node)] || 0) + 1;
  const areaList = Object.keys(perArea).sort().map(a => a + ' ' + perArea[a] + '台').join('　');
  return '目前在厂车辆共 <b>' + idx.length + '</b> 台<br>按输送区域：' + areaList +
         '<br>按颜色（色码）：' + join(distOf('color')) +
         '<br>按车型：' + joinModel(modelDist()) +
         '<br>按平台：' + join(distOf('platform'));
}
function ask(_q) {
  const q = String(_q || '').trim();
  if (!q) return;
  if (handleTrackCommand(q)) return;                      // 鹰眼追踪指令本地拦截, 不送 LLM/正则
  const qc = cleanQ(q) || q;                             // 剥离口语衬词后的分类副本(仅解析用, 不明示)
  const follow = hasFollowRef(qc), gs = globalScope(qc);
  // ⓪ 省略式耗时追问: 上一轮轨迹/路线 + "用了多少时间/要多久"
  if (wantsElapsedFollowUp(qc, lastContext)) {
    const a = elapsedFollowAnswer(lastContext);
    if (a) { qa({ q: q, a: a }); return; }
  }
  // ⓪′ 车→目的地: "雪橇2192到注蜡出口需要多少时间" = 从车当前位置规划路线
  const vroute = vehicleRouteIntent(qc);
  if (vroute) {
    if (!vroute.node) {
      qa({ q: q, a: '未在实时状态中找到雪橇 <b>' + escHtml(vroute.kw) +
                   '</b> 的当前位置，无法计算到 ' + escHtml(vroute.dest) + ' 的路线耗时。' });
      return;
    }
    const vm = {};
    const vrAns = routeAns(qc, {
      start: vroute.node, end: vroute.dest,
      platform: vroute.platform, vin: vroute.vin, bodytype: vroute.bodytype
    }, vm);
    if (vrAns) {
      lastContext.intent = 'route';
      lastContext.vehicle = ''; lastContext.vehicleKeyword = '';
      if (vm.startTok) lastContext.routeStart = vm.startTok;
      if (vm.endTok) lastContext.routeEnd = vm.endTok;
      lastContext.routeFrom = vm.from || vroute.label || vroute.node;
      lastContext.routeTo = vm.to || vroute.dest;
      if (vm.paths && vm.paths.length) lastContext.routeCost = vm.paths[0].cost;
      else lastContext.routeCost = null;
      if (vm.platform) lastContext.platform = vm.platform;
      qa({ q: q, a: vrAns }); return;
    }
  }
  // ⓪″ 路线查询(先于统计, 免得 "到...多少时间" 被 "多少" 计数短路): "A 到 B"
  if (/到|→|至|去|怎么走|通行|路线|路径|几条|哪些|最优|最佳|多久|多长时间|多少时间/.test(qc)) {
    const meta = {};
    let r = routeAns(qc, null, meta);
    if (!r) {                                            // 追问省略起终点: 用上一轮补
      const ch = ctxRouteHint(qc);
      if (ch.start || ch.end) { const m2 = {}; r = routeAns(qc, ch, m2); if (r) Object.assign(meta, m2); }
    }
    if (r) {
      lastContext.intent = 'route';
      if (meta.startTok) lastContext.routeStart = meta.startTok;
      if (meta.endTok) lastContext.routeEnd = meta.endTok;
      lastContext.routeFrom = meta.from || lastContext.routeStart;
      lastContext.routeTo = meta.to || lastContext.routeEnd;
      if (meta.paths && meta.paths.length) lastContext.routeCost = meta.paths[0].cost;
      else lastContext.routeCost = null;
      if (meta.platform) lastContext.platform = meta.platform;
      else if (gs) lastContext.platform = '';
      qa({ q: q, a: r }); return;
    }
  }
  // ⓪ 聚合统计(前缀短路): "区域有多少台" / "xx颜色|车型多少台"
  if (/多少|几台|几个|几辆|若干|数量|分布|怎么分|如何分|构成|占比|汇总|统计|台数|缓存|缓冲|各区|清单|有车|有没有|有车辆/i.test(qc)) {
    const co = {}, ah = areasIn(qc).join(',') || (gs ? '' : lastContext.area);
    const a = countAns(qc, ah, co);
    lastContext.intent = 'count';
    lastContext.area = co.area || '';
    const qp = platformIn(qc);
    if (qp) lastContext.platform = qp; else if (gs) lastContext.platform = '';
    qa({ q: q, a: a });
    return;
  }
  const nums = (qc.match(/\d{2,}/g) || []).filter(s => s.length <= 6 || s.length >= 8); // 丢弃歧义中段
  const intent = /雪橇|牵引|托架|吊具|sled/i.test(qc) ? 'sled'
               : /车身|整车|车辆|车|车号|body/i.test(qc) ? 'body' : 'auto';
  const idx = vehIndex();
  // ① 车辆反查: 雪橇号(≤6位) / PIN·车身号(≥8位); 逐数字串尝试, 首中即止
  let hit = null, kind = null;
  for (const n of nums) {
    let m = null;
    if (intent !== 'body') { const c = idx.filter(x => x.sled === n); if (c.length) { m = c[0]; kind = 'sled'; } }
    if (!m && intent !== 'sled') { const c = idx.filter(x => x.pin === n || x.body === n); if (c.length) { m = c[0]; kind = 'body'; } }
    if (m) { hit = m; break; }
  }
  // ② 未命中 → 整句/整串匹配节点 label/id(设备定位)
  let node_hits = [];
  if (!hit) node_hits = ALL_NODES.filter(n => n.id.toUpperCase().includes(norm(qc)) || n.label.toUpperCase().includes(norm(qc)));
  // ③ 封答
  let a = '', hidden = false;
  if (hit) {
    hidden = !locate(hit.node);
    rememberVehicle(hit.v, hit.v.sled_no || hit.v.pin_code);  // 供下一句"跟踪这台车"直接承接
    a = (kind === 'sled' ? '雪橇 ' + hit.v.sled_no + ' 在 ' : '车身 ' + hit.v.pin_code + ' 在 ') +
        hit.label + '（' + hit.node + '）' + (hidden ? '（该节点不在当前视图，请切到 ' + lineOfNode(hit.node) + ' 线查看）' : '') +
        '<br>' + vehCardHtml(hit.v);
  } else if (node_hits.length) {
    const vis = node_hits.filter(x => VISIBLE_ID.has(x.id));
    if (vis.length === 1) {
      const id = vis[0].id; locate(id); const v = VEHICLE[id];
      if (v) rememberVehicle(v);                          // 点查设备带出车号时同样记上下文
      a = (v ? LABEL[id] + ' 有车：<br>' + vehCardHtml(v) : LABEL[id] + '（' + id + '）当前无车停靠');
    } else if (vis.length > 1) {
      a = '匹配设备 ' + vis.length + ' 个：' + vis.map(x => LABEL[x.id]).slice(0, 8).join('、') + '（输入完整设备号可精确高亮）';
    } else {
      a = '匹配到节点但当前视图已过滤（' + node_hits.map(x => x.id).slice(0, 8).join('、') + '），请切到对应线查看';
    }
  } else {
    a = '未找到 ' + q + '。可输入 雪橇号(如 1473) / 车身PIN(如 78202684403929) / 设备号(如 1B100 或 Line_4_1B100RB)。';
  }
  lastContext.intent = 'vehicle';
  qa({ q: q, a: a });
}

// ---- 本地大模型(Ollama)解析: 问句 → 意图 + 参数 JSON ----
// intent 三类: route(路线) / count(区域车辆统计) / vehicle(具体车辆查找)。
// 失败/超时自动回退原正则逻辑(ask), 页面不会卡死。
const LLM_URL = 'http://localhost:11434/api/generate';
const LLM_MODEL = 'qwen2.5:3b';
const LLM_TIMEOUT = 20000;                                  // ms; 3B 模型本地一般 1~5s
const LLM_SYSTEM = [
  '你是输送线问答的意图解析器。只输出一个 JSON 对象, 不要任何解释或代码块标记。',
  '字段固定为: intent, start, end, platform, vin, bodytype, targetArea, targetKeyword, node_id, time_range, 全部是字符串。',
  'intent 只能是 history_bottleneck / history_count / history_node_detail / history_track / route / count / vehicle / unknown 之一:',
  'route: 问两点之间怎么走、通行、路线/路径、几条可行、最优/最佳(如 "Line_1入口到Line_4出口怎么走")。',
  '【最高优先级】出现 "路线/路径/怎么走/如何走/走哪/哪条道/哪条线/通行/怎么出去" 或 "最优/最佳" 与走/道/线/通连用时，即使句子里带区域名、车型、数量(如 "这些车出到注蜡2线走哪条路线最优")，也必须判 route，不能判 count/history_count。',
  '【车→目的地耗时=route】"雪橇/牵引/托架/车身/PIN/号码 + 到/→/至/去 + 区域/出口 + 多少时间/多久/耗时" 必须判 route，不是 vehicle 也不是 history_track; start 填该车号, end 填目的地, targetKeyword 留空。',
  '例: "雪橇2192到注蜡出口需要多少时间" → intent=route, start=2192, end=注蜡出口, targetKeyword=(空) (起点由前端换成该车当前位置, 不是查 1706 的轨迹)。',
  'count: 问整条线或某个区域现在一共几台/多少台车(如 "Line_2有几台车"、"Line_1有多少台"、"Line_2各车型分布") — 存量。',
  'vehicle: 查某一台具体车辆(雪橇号/车身号/VIN)或某一个设备(如 "Line_4_1A025LT"、"1B100"); 问单个设备有无车属于 vehicle — 现在在哪。',
  'history_bottleneck: 找历史堵点/平均停留最久的节点(如 "最近24小时哪里最堵"、"过去3小时哪些站停留久"); 参数 time_range 填时间说法, 如 "最近24小时"。',
  'history_count: 查某节点/某区域在一段时间内的历史过车量(如 "Line_4的1D235RB今天过了多少台车"、"连廊过去24小时过车量"); node_id 填设备号, targetArea 填区域代码, time_range 填时间范围。',
  '【节点精确统计】句子里同时有区域和具体节点(如 "Line_1的045RB最近过了多少辆车"、"Line_4 1D235RB今天过车量")时, 统计对象是那个节点, 不是整条区域。必须判 history_count: node_id 填节点号(如 045RB, 能确定全名时填 Line_1_1A045RB); targetArea 留空, 绝不要把 Line_1 当统计范围。只有问整条线/整区域汇总(count 或 history_count)时才填 targetArea。',
  'history_node_detail: 问某一个具体节点的堵点/停留详情(如 "Line_3_1C330RB 为什么平均停留 218 分钟"、"1C330RB 停留多久/堵不堵"); node_id 必填设备号, targetArea 可填线名, time_range 填时间范围。只问单个节点时用它, 不要用 history_bottleneck 排行榜。',
  'history_track: 查某台车的历史轨迹/走过哪些设备/每站停了多久(如 "雪橇1239的轨迹"、"PIN 78202664513471走过哪些站"、"这辆车经过了哪些路径"); 参数 targetKeyword 填号码; 有代词时参考上一轮车辆号。',
  '区分: count=现在在线台数, history_count=一段时间累计过车; vehicle=现在在哪, history_track=历史路径; route=怎么走, history_bottleneck=哪儿最堵(排行榜), history_node_detail=某一个节点自己的停留/过车。',
  '判定要点: 出现 "雪橇/牵引/托架/车身号/VIN" 加号码时, 若问 "在哪/到哪了" 归 vehicle, 若问 "轨迹/走过/停留" 归 history_track;',
  '例: "雪橇6210在哪" → intent=vehicle, targetKeyword=6210 (不是 route)。',
  '例: "最近24小时哪里最堵" → intent=history_bottleneck, time_range=最近24小时。',
  '例: "Line_4 1D235RB 今天过了多少台车" → intent=history_count, node_id=1D235RB, targetArea=(空), time_range=今天 (节点查询不要把 Line_4 当统计范围)。',
  '例: "Line_1的045RB最近过了多少辆车" → intent=history_count, node_id=045RB, targetArea=(空), time_range=最近24小时 (后端按 Line_1 前缀补全为 Line_1_1A045RB)。',
  '例: "Line_3_1C330RB 为什么平均停留 218 分钟" → intent=history_node_detail, node_id=Line_3_1C330RB, targetArea=Line_3 (不是 history_bottleneck 排行榜)。',
  '例: "这些车如果要出到注蜡2线，走哪条路线最优" → intent=route, end=注蜡2线 (路线词优先于 count)。',
  '例: "Line_4 现在的过车量趋势怎么样" → intent=history_count, targetArea=Line_4 (问过车量/通行量/流量趋势=历史累计, 不是 count 在厂台数)。',
  '例: "Line_2和Line_4一共有多少车辆" → intent=count, targetArea=Line_2,Line_4 (多个区域必须全填, 逗号分隔)。',
  '例: "连廊和车顶储存线一共有多少车" → intent=count, targetArea=Line_1,Line_2 (黑话先按下方映射归一)。',
  'time_range 优先归一为相对窗口: 30m(半小时/30分钟/最近半小时)、1h(1小时)、3h、24h(今天/最近24小时)、7d(一周); 绝对日期/时段保留原文(如 "10月28日12点到16点")。未提到填 "最近24小时"。后端会严格按该窗口截取 ts_epoch, 禁止把 "半小时" 当 24 小时。',
  '多轮对话: 若当前问题出现 "这/那/它们/上面说的/刚才" 等代词, 或省略了区域/平台, 参考给出的上一轮问题与上下文;',
  '例: 上一轮 "连廊有几台车"(区域Line_1), 当前 "这16台车型分布怎么样" → intent=count, targetArea=(空) (由前端继承 Line_1, 不是 vehicle)。',
  '例: 上一轮 "雪橇6077在哪"(车辆=6077), 当前 "这辆车经过了哪些路径" → intent=history_track, targetKeyword=6077 (不是 route; "经过哪些路径"是历史轨迹)。',
  '本句明说的区域/平台优先; 说 "全厂/各区域/所有平台" 时不要套用上一轮。',
  'route 时: start/end 填起点/终点, 可用 "线名+入口/出口"(如 "Line_1入口"、"Line_4出口") 或设备号(如 "Line_4_1E355RB"); 未明确填空字符串。',
  'count / history_count 时: targetArea 填区域代码; 询问多个区域时用英文逗号分隔(如 "Line_2,Line_4"), 不要只填第一个; 未提到填空字符串。',
  'vehicle 时: targetKeyword 只填雪橇号或车身号/VIN 号码本身(不要带 "雪橇/车身" 等字样), 未提到填空字符串。',
  'platform: 仅取 ' + PLATFORMS.join('/') + ' 之一; vin: 车身号/VIN; bodytype: 2位车型代码。',
  '【车型识别要求】如果用户提到车型的中文名或别名，请务必将其映射为对应的2位车型代码（因为底层数据用的是代码）。',
  __CAR_DICT__,
  '【区域识别要求】现场对区域的习惯叫法(黑话)请按下面映射归一为区域代码, 并用于 start/end/targetArea。',
  __AREA_GLOSSARY__,
  '【多轮省略句硬规则 · Few-Shot】',
  '即使本句没有明说区域, "分布情况如何" / "按车型怎么分" / "各车型多少" / "各平台数量" / "颜色怎么分" 也必须判 intent=count, targetArea 留空字符串(不要填上一轮区域, 由前端继承 lastContext.area)。',
  '例: 上一轮 "Line_2有几台车", 当前 "分布情况如何" → intent=count, targetArea=(空); 不要判 vehicle 或 unknown。',
  '例: "按车型怎么分" → intent=count, targetArea=(空); 前端会按上一轮区域输出车型分布。',
  '例: "各平台数量" → intent=count, targetArea=(空); 不要判 vehicle。',
  '"这辆车经过了哪些设备" / "刚才的路径" / "它走过哪些站" / "刚才的轨迹" 必须判 intent=history_track; targetKeyword 留空或填上一轮车辆号, 不要判 route。',
  '例: 上一轮 "雪橇6077在哪", 当前 "刚才的路径" → intent=history_track, targetKeyword=(空或6077)。',
  '如果无法确定意图, 或上下文不足以确定参数, 必须返回 {"intent":"unknown"}; 其余字段全部填空字符串, 不要猜测。前端会走本地正则兜底。',
  '与当前 intent 无关的字段一律空字符串, 禁止臆造。'
].join('\n');
async function askLLM(userInput, ctxLine) {
  const s = v => String(v == null ? '' : v).trim();
  const u = v => s(v).toUpperCase();
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), LLM_TIMEOUT);
  try {
    const resp = await fetch(LLM_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: ctl.signal,
      body: JSON.stringify({
        model: LLM_MODEL,
        prompt: (ctxLine ? (s(ctxLine) + '当前问题是：' + s(userInput)) : ('用户问题: ' + s(userInput))),
        system: LLM_SYSTEM,
        format: 'json',                                    // 强制 JSON 输出
        stream: false,
        options: { temperature: 0, num_predict: 200 }
      })
    });
    if (!resp.ok) throw new Error('Ollama HTTP ' + resp.status);
    const data = await resp.json();
    const txt = s(data && data.response);
    if (!txt) throw new Error('Ollama 返回空内容');
    const obj = JSON.parse(txt);
    return { intent: s(obj.intent).toLowerCase(),
             start: s(obj.start), end: s(obj.end), platform: u(obj.platform),
             vin: u(obj.vin), bodytype: u(obj.bodytype),
             targetArea: u(obj.targetArea), targetKeyword: s(obj.targetKeyword),
             node_id: u(obj.node_id), time_range: s(obj.time_range) };
  } finally { clearTimeout(timer); }
}
// 只接受合法枚举/格式, 防止模型幻觉污染路由
function normLLMHint(h) {
  if (!h) return null;
  const clean = v => String(v == null ? '' : v).replace(/^["'\s]+|["'\s]+$/g, '');
  h.start = areaNorm(clean(h.start)); h.end = areaNorm(clean(h.end));   // 黑话起终点归一
  h.intent = String(h.intent || '').toLowerCase();
  h.platform = String(h.platform || '').toUpperCase();
  h.bodytype = String(h.bodytype || '').toUpperCase();
  h.vin = String(h.vin || '').toUpperCase();
  h.targetArea = String(h.targetArea || '').toUpperCase();
  h.targetKeyword = String(h.targetKeyword || '').toUpperCase().replace(/[\s"'“”]/g, '');
  h.node_id = String(h.node_id || '').toUpperCase().replace(/[\s"'“”]/g, '');
  h.time_range = clean(h.time_range).slice(0, 60);
  // 模型没把握/给了非法枚举 → unknown, 由 askSmart 走本地正则兜底, 不硬猜。
  if (['route', 'count', 'vehicle', 'history_bottleneck', 'history_count',
       'history_node_detail', 'history_track', 'unknown']
      .indexOf(h.intent) < 0) h.intent = 'unknown';
  if (h.node_id && !/^[A-Z0-9_]{3,64}$/.test(h.node_id)) h.node_id = '';
  // 小模型常把 "Line_4的1D235RB" 拆到 start/end/targetKeyword; 历史统计/节点详情时回收成 node_id
  if ((h.intent === 'history_count' || h.intent === 'history_node_detail') && !h.node_id) {
    const dev = [h.targetKeyword, h.start, h.end].find(v =>
      /^[0-9][A-Z][0-9A-Z]{2,}$/.test(String(v || '')) ||
      /^(?:LINE|EXIT)_\d+_[A-Z0-9]+$/.test(String(v || '')));
    if (dev) h.node_id = dev;
  }
  // 指名了具体节点的堵点问法: 即使模型判成排行榜, 也收敛为单节点详情
  if (h.intent === 'history_bottleneck' && h.node_id) h.intent = 'history_node_detail';
  if (h.platform && PLATFORMS.indexOf(h.platform) < 0) h.platform = '';
  if (h.bodytype && BODYTYPES.indexOf(h.bodytype) < 0) {   // 模型给了中文名/别名 → 映射 2 位代码(唯一才认)
    const mc = MODEL_ALIAS[h.bodytype] || [];
    h.bodytype = mc.length === 1 ? mc[0] : '';
  }
  if (h.vin && !/^[A-Z0-9]{6,}$/.test(h.vin)) h.vin = '';
  const az = areaNorm(h.targetArea).toUpperCase();          // "连廊/注蜡1线/Line_2线" → 区域代码
  const taTok = nodeTok(az);                              // 模型若把 "Line_1_045RB / 045RB" 当区域填
  h.targetArea = areasIn(az).join(',');                   // 多区域保留为 "Line_2,Line_4"
  if (taTok) { if (!h.node_id) h.node_id = taTok; h.targetArea = ''; }   // 回收成 node_id, 区域字段只放线名
  // 关键词剥掉 "雪橇/车身/PIN" 等字样, 只留号码; 允许保留设备号(含字母)
  h.targetKeyword = h.targetKeyword.replace(/^(雪橇|牵引|托架|车身|整车|车辆|车号|PIN|VIN|BODY)+/i, '');
  return h;
}
// ---- 路线数据 → 口语化汇报(第二次 Ollama 调用, 纯文本, 50 字内) ----
const LLM_SUMMARY_SYSTEM = '你是汽车产线调度专家。请根据以下路径数据，用 50 个字以内向车间主任口语化汇报最优路线及原因。';
function buildPathBrief(meta) {
  const key = s => String(s || '').replace(/\([^)]*\)/g, '')          // 去掉目标设备与冗余字样, 压成 "Line_1 010道1"
                              .replace(/DEC/g, '').replace(/→道/g, '道');
  const ps = meta.paths || [];
  const rank = ps.map((p, i) => (i + 1) + ') ' + p.cost + '秒/' + p.stations + '站').join('; ');
  const seqs = ps.map(p => (p.dec || []).map(key));
  const common = seqs.length ? seqs[0].filter(x => seqs.every(s => s.indexOf(x) >= 0)) : [];
  const diff = seqs.map((s, i) => {
    const d = s.filter(x => common.indexOf(x) < 0);
    return '第' + (i + 1) + '条' + (d.length ? ' ' + d.join('、') : ' 无');
  });
  return '起点 ' + meta.from + ', 终点 ' + meta.to + ', 共 ' + meta.total + ' 条可行。前' + ps.length + '(第1条即最优): ' + rank + '。' +
         (ps.length > 1 ? '第1条比第2条快' + Math.max(0, Math.round(ps[1].cost - ps[0].cost)) + '秒。' : '') +
         (common.length ? '共同决策: ' + common.join('、') + '。' : '') +
         '差异: ' + diff.join('; ') + '。' +
         '只输出一句50字内口语汇报, 不要罗列决策号。';
}
// 兜底压短: 60 字以内, 优先在句末标点收尾
function tidySummary(txt, maxLen) {
  const max = maxLen || 60;
  let t = String(txt == null ? '' : txt).replace(/\s+/g, ' ').trim()
            .replace(/^[\s"'“”]+|[\s"'“”]+$/g, '');
  if (t.length > max) {
    const head = t.slice(0, max);
    const m = head.match(/^.*[。！？；]/);
    t = m ? m[0] : (t.slice(0, max - 10) + '…');
  }
  return t;
}
async function askOllamaProse(brief, system, numPredict, maxLen) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), LLM_TIMEOUT);
  try {
    const resp = await fetch(LLM_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: ctl.signal,
      body: JSON.stringify({
        model: LLM_MODEL,
        prompt: brief,
        system: system,
        stream: false,
        options: { temperature: 0.3, num_predict: numPredict || 120 }
      })
    });
    if (!resp.ok) throw new Error('Ollama HTTP ' + resp.status);
    const data = await resp.json();
    const txt = String((data && data.response) || '').trim();
    if (!txt) throw new Error('Ollama 汇总为空');
    return tidySummary(txt, maxLen);
  } finally { clearTimeout(timer); }
}
async function askLLMSummary(brief) { return askOllamaProse(brief, LLM_SUMMARY_SYSTEM, 120, 60); }

// ---- 历史统计(MySQL) → 口语化汇报 ----
const HIST_API_BASE = 'http://' +
  ((typeof location !== 'undefined' && location.hostname) ? location.hostname : '127.0.0.1') + ':8766';
const HIST_SUMMARY_SYSTEM = '你是汽车产线数据播报员。请根据数据库统计结果，用 90 个字以内在车间主任面前口语化汇报。堵点只点出最严重的 1-2 个节点，绝对不要罗列全部；只讲最重要结论和关键数字，数字必须原样引用，不要自行计算、不要改写日期，不要罗列字段名或 JSON。若输入注明“无新数据/库中最近数据”，必须说明。轨迹汇报只允许陈述输入中的事实：该车经过几个节点、在哪个节点停留多久；严禁推断原因，严禁出现“堵车/拥堵/堵塞/严重/需重点关注/建议/故障”等输入没有的结论。若输入说明只有 1 站，必须补充“该车可能刚刚进站，历史轨迹数据较少”。';
async function askHistorySummary(brief) { return askOllamaProse(brief, HIST_SUMMARY_SYSTEM, 220, 100); }

async function histApi(path, params) {
  const qs = Object.keys(params || {}).filter(k => params[k] !== '' && params[k] != null)
    .map(k => encodeURIComponent(k) + '=' + encodeURIComponent(params[k])).join('&');
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 15000);
  try {
    const r = await fetch(HIST_API_BASE + path + (qs ? '?' + qs : ''),
                          { signal: ctl.signal, cache: 'no-store' });
    if (!r.ok) throw new Error('统计接口 HTTP ' + r.status);
    const d = await r.json();
    if (!d || d.ok === false) throw new Error((d && d.error) || '统计接口返回失败');
    return d;
  } finally { clearTimeout(timer); }
}

function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return sec + '秒';
  if (sec < 3600) return Math.floor(sec / 60) + '分' + (sec % 60 ? (sec % 60) + '秒' : '');
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h + '小时' + (m ? m + '分' : '');
}
function fmtClock(epoch) {
  if (!epoch) return '-';
  const d = new Date(epoch * 1000), p = n => ('0' + n).slice(-2);
  return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
// "最近24小时"/"过去3小时"/"今天"/"昨天"/"10月28日12点到16点" → {start,end,label}
function histRange(tr) {
  const now = Math.floor(Date.now() / 1000);
  const s = String(tr || '').trim();
  const md = /(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})(?:[:：](\d{2}))?点?)?/.exec(s);
  if (md) {
    const y = new Date().getFullYear(), m0 = +md[1] - 1, d0 = +md[2];
    if (md[3] != null) {
      const h0 = +md[3], mi = +(md[4] || 0);
      const st = new Date(y, m0, d0, h0, mi, 0);
      const tail = s.slice(s.indexOf(md[0]) + md[0].length);
      const em = /(?:到|至|~|-)\s*(\d{1,2})(?:[:：](\d{2}))?点?/.exec(tail);
      let et = new Date(st.getTime() + 3600000);
      if (em) {
        et = new Date(y, m0, d0, +em[1], +(em[2] || 0), 0);
        if (et <= st) et = new Date(et.getTime() + 86400000);
      }
      return { start: Math.floor(st.getTime() / 1000), end: Math.floor(et.getTime() / 1000),
               label: md[1] + '月' + md[2] + '日 ' + md[3] + '点' + (em ? '-' + em[1] + '点' : '') };
    }
    const st = new Date(y, m0, d0), et = new Date(y, m0, d0 + 1);
    return { start: Math.floor(st.getTime() / 1000), end: Math.floor(et.getTime() / 1000),
             label: md[1] + '月' + md[2] + '日' };
  }
  if (/今天|今日|当天/.test(s)) {
    const d = new Date(); d.setHours(0, 0, 0, 0);
    return { start: Math.floor(d.getTime() / 1000), end: now, label: '今天' };
  }
  if (/昨天|昨日/.test(s)) {
    const d = new Date(); d.setHours(0, 0, 0, 0);
    const e = Math.floor(d.getTime() / 1000);
    return { start: e - 86400, end: e, label: '昨天' };
  }
  let hours = 24, m, lab = '';
  // 支持 LLM 归一后的 30m / 1h / 24h / 7d, 以及现场口语 30分钟 / 半小时 / 两小时
  if ((m = /(\d+(?:\.\d+)?)\s*(?:分钟|分|m(?:in)?)(?![a-z])/i.exec(s))) {
    hours = parseFloat(m[1]) / 60; lab = '最近' + m[1] + '分钟';
  }
  else if (/半\s*(?:个)?\s*(?:小时|钟头)/.test(s)) { hours = 0.5; lab = '最近半小时'; }
  else if ((m = /([一二两三四五六七八九十]+)\s*(?:个)?(?:小时|钟头)/.exec(s))) {
    const cn = { '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
                 '六': 6, '七': 7, '八': 8, '九': 9, '十': 10 };
    hours = cn[m[1]] || 1; lab = '最近' + m[1] + '小时';
  }
  else if ((m = /(\d+(?:\.\d+)?)\s*(?:个)?(?:小时|钟头|h)(?![a-z])/i.exec(s))) {
    hours = parseFloat(m[1]); lab = '最近' + m[1] + '小时';
  }
  else if ((m = /(\d+(?:\.\d+)?)\s*(?:天|日|d)(?![a-z])/i.exec(s))) {
    hours = parseFloat(m[1]) * 24; lab = '最近' + m[1] + '天';
  }
  else if (/一周|7天|七天/.test(s)) { hours = 168; lab = '最近一周'; }
  else if (/半天/.test(s)) { hours = 12; lab = '最近半天'; }
  hours = Math.max(1 / 60, Math.min(hours, 24 * 31));
  return { start: now - Math.round(hours * 3600), end: now,
           label: lab || (hours < 1 ? '最近' + Math.round(hours * 60) + '分钟' : '最近' + hours + '小时') };
}
function histSpanLabel(sec) {
  sec = Math.max(60, Math.round(sec));
  return sec >= 3600 ? '库中最近' + Math.round(sec / 3600) + '小时' : '库中最近' + Math.round(sec / 60) + '分钟';
}
function histStaleNote(d, range, fb) {
  if (fb) return '（最近区间无新数据，以下为库中最近数据）';
  if (d.latest_ts && range.start && d.latest_ts < range.start)
    return '（数据库最近记录停在 ' + fmtClock(d.latest_ts) + '，该时段无新数据）';
  return '';
}
// 小模型可能算错/漏关键数字; 至少核对一个核心数字, 否则用本地成型文本
function histSummaryOk(intent, d, txt) {
  const t = String(txt || '');
  if (!t) return false;
  if (intent === 'history_bottleneck') {
    const rows = d.top || [];
    if (!rows.length) return true;
    const ids = rows.map(x => x.node_id.toUpperCase());
    // 点名的节点必须是榜单里的, 不能编
    const nodeToks = t.match(/(?:(?:LINE|EXIT)_\d+_)?[0-9][A-Z][0-9A-Z]{2,}RB/g) || [];
    for (const x of nodeToks) {
      const u = x.toUpperCase();
      if (ids.indexOf(u) < 0 && !ids.some(id => id.split('_').pop() === u))
        return false;
    }
    // 报出的平均停留时长落在榜单量级内(允许口语取整/说"以上")
    const avgs = rows.map(x => x.avg_dwell_sec);
    const lo = Math.min.apply(null, avgs), hi = Math.max.apply(null, avgs);
    for (const s of t.match(/\d+分\d+秒|\d+秒/g) || []) {
      const m = /^(\d+)分(\d+)秒$/.exec(s);
      const v = m ? (+m[1]) * 60 + (+m[2]) : (+s.replace('秒', ''));
      if (v < lo - 2 || v > hi + 2) return false;
    }
    return true;
  }
  if (intent === 'history_node_detail') {
    if (/最严重|最堵|最久|第一|榜首|排名/.test(t)) return false;  // 单节点详情不念榜单/不排名
    const row = (d.top || [])[0];
    const short = String(d.node_id || '').split('_').pop().toUpperCase();
    if (!short || t.toUpperCase().indexOf(short) < 0) return false;
    const pc = Number(d.pass_count);
    if (isFinite(pc) && t.indexOf(String(pc)) < 0) return false;
    const uv = Number(d.unique_vehicles);
    if (isFinite(uv) && uv > 0 && t.indexOf(String(uv)) < 0) return false;
    if (!row) return /没有|无|0/.test(t);
    const durs = t.match(/\d+分\d+秒|\d+秒/g) || [];
    if (!durs.length) return false;
    const secOf = s => {
      const m = /^(\d+)分(\d+)秒$/.exec(s);
      return m ? (+m[1]) * 60 + (+m[2]) : (+s.replace('秒', ''));
    };
    return durs.some(s => Math.abs(secOf(s) - Math.round(row.avg_dwell_sec)) <= 3);
  }
  if (intent === 'history_count') {
    if (/去重窗口|窗口期|300\s*秒|统计口径|是堵点/.test(t)) return false;  // 术语/结论瞎造 → 用本地文本
    if (d.pass_count === 0) return /0|没有|无/.test(t);
    return t.indexOf(String(d.pass_count)) >= 0 &&
           (!d.unique_vehicles || t.indexOf(String(d.unique_vehicles)) >= 0);
  }
  const c = d.car || {}, segs = d.segments || [];
  // 轨迹只许陈述事实, 不许加戏(堵车严重/需重点关注/建议/故障等都是模型臆造)
  if (/堵车|拥堵|堵塞|严重|需重点关注|重点关注|建议|故障|异常|瓶颈|因为|导致|推测|猜测/.test(t)) return false;
  if (!segs.length) return /0|没有|暂无|无/.test(t);
  const total = Number(d.total_segments);
  if (isFinite(total) && total <= 1 && !/刚刚进站|数据较少/.test(t)) return false;
  if (isFinite(total) && total > 1 && t.indexOf(String(total)) < 0) return false;
  if (!((c.pin_code && t.indexOf(c.pin_code) >= 0) ||
        (c.sled_no && t.indexOf(c.sled_no) >= 0) ||
        (segs[0] && t.indexOf(segs[0].label) >= 0))) return false;
  if (!/\d+分\d+秒|\d+秒/.test(t)) return false;           // 必须报出至少一个停留时长
  // 轨迹汇报里出现过的站名/停留时长必须来自 API 数据(实测小模型会把 6分9秒 说成 16分9秒,
  // 也会把别的站的 18秒 安到停留最久的站上), 因此按"站名 + 就近时长"成对核对.
  const perNode = {};
  segs.forEach(x => {
    const k = String(x.label || x.node_id).split('_').pop().toUpperCase();
    (perNode[k] = perNode[k] || []).push(x.dwell_sec);
  });
  const maxDwell = Math.max.apply(null, segs.map(x => x.dwell_sec));
  const durRe = /\d+分\d+秒|\d+秒/g;
  const secOf = s => {
    const m = /^(\d+)分(\d+)秒$/.exec(s);
    return m ? (+m[1]) * 60 + (+m[2]) : (+s.replace('秒', ''));
  };
  const nodeRe = /[0-9][A-Z][0-9A-Z]{2,}RB/gi;   // 不用单词边界: 本页 JS 经 Python 字符串转义, 边界符会被吞掉
  let mm;
  while ((mm = nodeRe.exec(t))) {
    const key = mm[0].toUpperCase();
    if (!perNode[key]) return false;
    const tail = t.slice(nodeRe.lastIndex, nodeRe.lastIndex + 24);
    const dm = tail.match(durRe);
    if (dm && perNode[key].indexOf(secOf(dm[0])) < 0) return false;
  }
  for (const s of t.match(durRe) || []) {
    if (!segs.some(x => x.dwell_sec === secOf(s))) return false;
  }
  if (/最(久|长|多|高)/.test(t) &&
      !(t.match(durRe) || []).some(s => secOf(s) === maxDwell)) return false;
  return true;
}
function histFallbackText(intent, d, range, fb) {
  const stale = d.latest_ts && d.latest_ts < range.start;
  const staleNote = fb ? '；最近区间无新数据，已改用库中最近数据'
                       : (stale ? '；数据库最近记录停在 ' + fmtClock(d.latest_ts) + '，该时段无新数据' : '');
  if (intent === 'history_bottleneck') {
    if (!d.top || !d.top.length)
      return range.label + '没有足够的停留数据（每个节点至少 3 次）' + staleNote + '。';
    const t = d.top[0];
    return range.label + '共 ' + d.cars + ' 台车，最堵的是 ' + t.node_id +
           '，平均每台停留 ' + fmtDur(t.avg_dwell_sec) + '。';
  }
  if (intent === 'history_count') {
    const area = /^F(HK)?\d+$/.test(String(d.scope || '')) ? d.scope : '';
    const head = (d.scope && d.scope !== '全厂') ? ' ' + d.scope : '';
    return '统计期内' + head + '过车 ' + d.pass_count +
           ' 台次（去重 ' + d.unique_vehicles + ' 台）' +
           (d._risk && area ? '，目前' + area + '区域有拥堵风险' : '') + staleNote + '。';
  }
  if (intent === 'history_node_detail') {
    const t = (d.top || [])[0];
    const head = range.label + '，' + (d.node_id || '该节点');
    const flow = '，同期过车 ' + (d.pass_count == null ? '-' : d.pass_count) +
                 ' 台次（去重 ' + (d.unique_vehicles == null ? '-' : d.unique_vehicles) + ' 台）';
    if (!t) return head + ' 没有有效停留记录（停留 <2 秒或 >15 分钟被剔除）' + flow + staleNote + '。';
    return head + ' 平均停留 ' + fmtDur(t.avg_dwell_sec) + '（' + t.visits + ' 次，范围 ' +
           fmtDur(t.min_dwell_sec) + '~' + fmtDur(t.max_dwell_sec) + '）' + flow + staleNote + '。';
  }
  const c = d.car || {}, segs = d.segments || [];
  const nodes = (d.nodes && d.nodes.length) ? d.nodes : segs.map(x => x.node_id);
  if (!nodes.length) return '未找到该车轨迹。';
  const total = nodes.length;
  const cs = c.sled_no ? ('雪橇 ' + c.sled_no + (c.pin_code ? ' / PIN ' + c.pin_code : ''))
                       : ('PIN ' + (c.pin_code || d.query));
  let tr = '该车（' + cs + '）共经过了 ' + total + ' 个节点。';
  const cur = d.current || segs[segs.length - 1] || null;
  if (cur) {
    const nm = cur.label || cur.node_id;
    if (cur.ongoing) tr += '目前在 ' + nm + ' 已停留 ' + fmtDur(cur.dwell_sec) + '。';
    else if (cur.dwell_sec > 0) tr += '最后记录在 ' + nm + '，停留 ' + fmtDur(cur.dwell_sec) + '。';
    else tr += '最后记录在 ' + nm + '。';
  }
  const prev = segs.filter(x => !cur || x.node_id !== cur.node_id || x.enter_ts !== cur.enter_ts)
                   .slice(0, 5);
  if (prev.length) {
    tr += '之前经过了 ' + prev.map(x => x.label || x.node_id).join('、') +
          (total - 1 > prev.length ? ' 等' : '') + '。';
  }
  if (total <= 1) {
    tr += (cur && cur.ongoing && cur.dwell_sec >= 600)
      ? '该车当前仅记录了 1 个节点，历史轨迹数据较少。'
      : '该车可能刚刚进站，历史轨迹数据较少。';
  }
  if (total < 5) {
    tr += '（注：部分短轨迹是因为车辆在服务重启前已经进厂，或经过了不停车的过渡辊床）';
  }
  return tr;
}
function histDetails(intent, d, range, fb) {
  if (intent === 'history_bottleneck') {
    const rows = (d.top || []).map((x, i) =>
      '<tr><td>' + (i + 1) + '</td><td>' + escHtml(x.node_id) + '</td><td>' + x.visits +
      '</td><td>' + fmtDur(x.avg_dwell_sec) + '</td><td>' + fmtDur(x.min_dwell_sec) + '~' +
      fmtDur(x.max_dwell_sec) + '</td></tr>').join('');
    return '<details><summary style="cursor:pointer;color:#2a6fb0;font-size:12px">查看堵点明细（' +
      escHtml(range.label) + '，' + d.cars + ' 台车）</summary>' +
      '<table style="font-size:12px;margin-top:4px;border-collapse:collapse">' +
      '<tr><th>#</th><th>节点</th><th>次数</th><th>平均停留</th><th>范围</th></tr>' + rows + '</table>' +
      '<div style="font-size:11px;color:#777;margin-top:4px">数据截至 ' + escHtml(d.latest_iso || '-') +
      '；停留 = 同车同站点最后上报 − 最早上报，已剔除 &lt;' + (d.min_dwell_sec || 2) + '秒 与 &gt;' +
      Math.round((d.max_dwell_sec || 900) / 60) + '分钟 的异常段' +
      (fb ? '；最近区间无新数据，已改用库中最近数据' : '') + '</div></details>';
  }
  if (intent === 'history_node_detail') {
    const t = (d.top || [])[0];
    const stale = d.latest_ts && d.latest_ts < range.start;
    const row = t ? '<tr><td>' + escHtml(t.node_id) + '</td><td>' + t.visits + '</td><td>' +
      fmtDur(t.avg_dwell_sec) + '</td><td>' + fmtDur(t.min_dwell_sec) + '~' +
      fmtDur(t.max_dwell_sec) + '</td></tr>' : '';
    return '<details><summary style="cursor:pointer;color:#2a6fb0;font-size:12px">查看节点明细（' +
      escHtml(range.label) + '）</summary>' +
      '<div style="font-size:12px;margin-top:4px">节点：' + escHtml(d.node_id || '-') +
      '<br>过车台次：' + (d.pass_count == null ? '-' : d.pass_count) +
      '；去重车辆：' + (d.unique_vehicles == null ? '-' : d.unique_vehicles) +
      ((d.node_candidates && d.node_candidates.length > 1)
        ? '<br>跨线同名节点：' + escHtml(d.node_candidates.join('、')) + '（当前按 ' + escHtml(d.node_id) + ' 统计）'
        : '') + '</div>' +
      (row ? '<table style="font-size:12px;margin-top:4px;border-collapse:collapse">' +
        '<tr><th>节点</th><th>次数</th><th>平均停留</th><th>范围</th></tr>' + row + '</table>' : '') +
      '<div style="font-size:11px;color:#777;margin-top:4px">数据截至 ' + escHtml(d.latest_iso || '-') +
      '；停留 = 同车同站点最后上报 − 最早上报，已剔除 &lt;' + (d.min_dwell_sec || 2) + '秒 与 &gt;' +
      Math.round((d.max_dwell_sec || 900) / 60) + '分钟 的异常段；过车按 5 分钟去重' +
      (fb ? '；最近区间无新数据，已改用库中最近数据'
          : (stale ? '；数据库最近记录停在该时段之前' : '')) + '</div></details>';
  }
  if (intent === 'history_count') {
    const stale = d.latest_ts && d.latest_ts < range.start;
    return '<details><summary style="cursor:pointer;color:#2a6fb0;font-size:12px">查看统计口径</summary>' +
      '<div style="font-size:12px;margin-top:4px">范围：' + escHtml(range.label) + '（' +
      escHtml(d.start_iso) + ' ~ ' + escHtml(d.end_iso) + '）<br>对象：' + escHtml(d.scope) +
      '<br>过车台次：' + d.pass_count + '；去重车辆：' + d.unique_vehicles +
      ((d.node_candidates && d.node_candidates.length > 1)
        ? '<br>跨线同名节点：' + escHtml(d.node_candidates.join('、')) + '（当前按 ' + escHtml(d.scope) + ' 统计）'
        : '') +
      '<br>过车去重：同一车辆 ' + Math.round((d.window_sec || 300) / 60) +
      ' 分钟内计 1 台次；数据源：' + escHtml(d.source || '-') +
      '<br>数据截至：' + escHtml(d.latest_iso || '-') +
      (fb ? '<br><b>最近区间无新数据，已改用库中最近数据</b>'
          : (stale ? '<br><b>数据库最近记录停在该时段之前，本区间无新数据</b>' : '')) +
      '</div></details>';
  }
  const c = d.car || {}, segs = d.segments || [];
  if (!segs.length) return '';                       // 空轨迹不渲染空表
  const rows = segs.map(x =>
    '<tr><td>' + x.seq + '</td><td>' + escHtml(x.node_id) + '</td><td>' + escHtml(x.enter_iso) +
    '</td><td>' + (x.leave_iso ? escHtml(x.leave_iso) : (x.ongoing ? '仍在' : '—')) +
    '</td><td>' + fmtDur(x.dwell_sec) + '</td></tr>').join('');
  return '<details><summary style="cursor:pointer;color:#2a6fb0;font-size:12px">查看完整轨迹（' +
    (d.returned_segments || segs.length) + ' 站 / 共 ' + (d.total_nodes || d.total_segments) + ' 站）</summary>' +
    '<div style="font-size:12px;margin-top:4px">PIN ' + escHtml(c.pin_code || '') +
    (c.sled_no ? ' · 雪橇 ' + escHtml(c.sled_no) : '') +
    (c.vehicle_type ? ' · 车型 ' + escHtml(c.vehicle_type) : '') + '</div>' +
    '<table style="font-size:12px;margin-top:4px;border-collapse:collapse">' +
    '<tr><th>#</th><th>节点</th><th>进入</th><th>离开</th><th>停留</th></tr>' + rows + '</table></details>';
}
function histBrief(intent, d, range, fb) {
  if (intent === 'history_bottleneck') {
    return '统计区间 ' + d.start_iso + ' 至 ' + d.end_iso + '，共 ' + d.cars + ' 台车。堵点前 ' +
      (d.top || []).length + '：' + (d.top || []).map((x, i) =>
        (i + 1) + '. ' + x.node_id + ' 平均停留 ' + fmtDur(x.avg_dwell_sec) + '（' + x.visits + ' 次）'
      ).join('；') + '。' + (fb ? '（最近区间无新数据，已改用库中最近数据）' : '') +
      '请只输出一句 80 字内口语汇报。';
  }
  if (intent === 'history_count') {
    const risk = d._risk ? '，目前 ' + d.scope + ' 区域有拥堵风险' : '';
    return '统计区间 ' + d.start_iso + ' 至 ' + d.end_iso + '，范围 ' + d.scope + '。过车台次 ' +
      d.pass_count + '，去重车辆 ' + d.unique_vehicles + ' 台。' +
      (d._risk ? '该区域存在平均停留偏高的节点(拥堵风险)。' : '') +
      (fb ? '（最近区间无新数据，已改用库中最近数据）' : '') +
      '请只输出一句 80 字内口语汇报，句式参考“统计期内 ' + d.scope + ' 过车 ' + d.pass_count +
      ' 台次（去重 ' + d.unique_vehicles + ' 台）' + risk + '”。' +
      '必须包含过车台次和去重车辆；不要出现“去重窗口/窗口期/300秒/统计口径”等术语，' +
      '不要臆造“是堵点”或任何未给出的数据。';
  }
  if (intent === 'history_node_detail') {
    const t = (d.top || [])[0];
    return '统计区间 ' + d.start_iso + ' 至 ' + d.end_iso + '，节点 ' + (d.node_id || '-') + '。' +
      (t ? '平均停留 ' + fmtDur(t.avg_dwell_sec) + '，共 ' + t.visits + ' 次（范围 ' +
           fmtDur(t.min_dwell_sec) + '~' + fmtDur(t.max_dwell_sec) + '）。'
         : '该节点没有有效停留记录。') +
      '同期过车 ' + (d.pass_count == null ? '-' : d.pass_count) + ' 台次（去重 ' +
      (d.unique_vehicles == null ? '-' : d.unique_vehicles) + ' 台）。' +
      (fb ? '（最近区间无新数据，已改用库中最近数据）' : '') +
      '请只输出一句 80 字内口语汇报，必须包含节点号、平均停留和过车台次；' +
      '用户问句里的时长若与数据不一致，以数据为准；不要念排行榜，不要编造原因。';
  }
  const c = d.car || {}, total = Number(d.total_segments) || (d.segments || []).length;
  return '车辆 PIN ' + c.pin_code + '，雪橇 ' + (c.sled_no || '-') + '，车型 ' + (c.vehicle_type || '-') +
    '，共 ' + total + ' 个节点。前 ' + (d.segments || []).length + ' 站：' +
    (d.segments || []).map(x => x.label + ' 停留 ' + fmtDur(x.dwell_sec)).join('；') +
    '。数据截至 ' + (d.latest_iso || '-') + '。' +
    '请只输出一句 80 字内口语汇报，只陈述事实，句式参考“该车经过了 ' + total +
    ' 个节点，在 A 点停留了 X 分 X 秒”；' +
    '严禁推断原因，严禁出现“堵车/拥堵/堵塞/严重/需重点关注/建议/故障”等输入没有的结论。' +
    (total <= 1 ? '本次只有 1 站，必须补充“该车可能刚刚进站，历史轨迹数据较少”。' : '');
}

// 图表配置(纯数据, 渲染在 renderQaChart 里做, 以便 CDN 未加载时安全降级)
function histChart(intent, d, range) {
  if (intent === 'history_bottleneck') {
    const top = d.top || [];
    if (!top.length) return null;
    return { type: 'bar', option: {
      animationDuration: 600,
      grid: { left: 8, right: 46, top: 10, bottom: 6, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: p => { const it = top[p[0].dataIndex] || {};
          return it.node_id + '<br>平均停留 ' + fmtDur(it.avg_dwell_sec) + '（' + it.visits + ' 次）'; } },
      xAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#667', formatter: '{value}s' },
               splitLine: { lineStyle: { color: '#eef1f5' } } },
      yAxis: { type: 'category', inverse: true,
               data: top.map(x => String(x.node_id || '').split('_').pop()),
               axisLabel: { fontSize: 10, color: '#333' }, axisTick: { show: false } },
      series: [{ type: 'bar', data: top.map(x => x.avg_dwell_sec), barMaxWidth: 16,
        itemStyle: { borderRadius: [0, 4, 4, 0],
          color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
                   colorStops: [{ offset: 0, color: '#3ba7ff' }, { offset: 1, color: '#ff8a3d' }] } },
        label: { show: true, position: 'right', fontSize: 10, color: '#445',
                 formatter: p => fmtDur(p.value) } }]
    }};
  }
  if (intent === 'history_count') {
    const hrs = d.by_hour || [];
    if (!hrs.length || !d.pass_count) return null;
    const longSpan = ((d.end_time || 0) - (d.start_time || 0)) > 2 * 86400;
    const p2 = n => (n < 10 ? '0' : '') + n;
    const lab = ts => { const t = new Date(ts * 1000);
      return longSpan ? (p2(t.getMonth() + 1) + '-' + p2(t.getDate()) + ' ' + p2(t.getHours()) + '时')
                      : (p2(t.getHours()) + ':00'); };
    return { type: 'line', option: {
      animationDuration: 600,
      grid: { left: 8, right: 14, top: 12, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', boundaryGap: false, data: hrs.map(h => lab(h.ts)),
               axisLabel: { fontSize: 9, color: '#667', hideOverlap: true } },
      yAxis: { type: 'value', minInterval: 1,
               axisLabel: { fontSize: 10, color: '#667', formatter: '{value}台' },
               splitLine: { lineStyle: { color: '#eef1f5' } } },
      series: [{ name: '过车台次', type: 'line', smooth: true, symbol: 'circle', symbolSize: 4,
        data: hrs.map(h => h.pass_count),
        lineStyle: { width: 2, color: '#2d6cdf' }, itemStyle: { color: '#2d6cdf' },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(45,108,223,.35)' },
                       { offset: 1, color: 'rgba(45,108,223,.03)' }] } } }]
    }};
  }
  return null;
}

// 历史意图统一入口: 调 8766 API → JSON → Ollama 口语化; Ollama 不可用则本地成型
async function historyAns(hint, raw) {
  const intent = hint.intent;
  let d, range = null, fb = false, mapHi = false;
  if (intent === 'history_bottleneck') {
    range = histRange(hint.time_range || '最近24小时');
    d = await histApi('/api/bottlenecks', { start_time: range.start, end_time: range.end,
                                            limit: 5, min_visits: 3 });
    if ((!d.top || !d.top.length) && d.latest_ts && d.latest_ts < range.start) {
      const span = Math.max(3600, range.end - range.start);
      const r2 = { start: d.latest_ts - span, end: d.latest_ts, label: histSpanLabel(span) };
      const d2 = await histApi('/api/bottlenecks', { start_time: r2.start, end_time: r2.end,
                                                    limit: 5, min_visits: 3 });
      if (d2.top && d2.top.length) { d = d2; range = r2; fb = true; }
    }
  } else if (intent === 'history_count') {
    range = histRange(hint.time_range || '最近24小时');
    // 短节点名有跨线重名(1D235RB 在 Line_1/Line_2/Line_4 都有): 带区域时补成 Line_4_1D235RB
    const nodeArea = String(hint.targetArea || '').split(',')[0];
    const nodeArg = hint.node_id
      ? ((nodeArea && hint.node_id.indexOf('_') < 0)
          ? nodeArea + '_' + hint.node_id : hint.node_id)
      : '';
    const p = { start_time: range.start, end_time: range.end, window: 300 };
    if (nodeArg) p.node_id = nodeArg;
    else if (hint.targetArea) p.area = hint.targetArea;
    d = await histApi('/api/point_traffic', p);
    if (d.pass_count === 0 && d.latest_ts && d.latest_ts < range.start) {
      const span = Math.max(3600, range.end - range.start);
      const r2 = { start: d.latest_ts - span, end: d.latest_ts, label: histSpanLabel(span) };
      const p2 = { start_time: r2.start, end_time: r2.end, window: 300 };
      if (nodeArg) p2.node_id = nodeArg;
      else if (hint.targetArea) p2.area = hint.targetArea;
      const d2 = await histApi('/api/point_traffic', p2);
      if (d2.pass_count > 0) { d = d2; range = r2; fb = true; }
    }
    // "拥堵风险"必须有数据依据: 同区域存在平均停留 >=60 秒且 >=2 次的节点才说
    d._risk = false;
    if (!nodeArg && hint.targetArea) {
      try {
        const bn = await histApi('/api/bottlenecks', {
          start_time: range.start, end_time: range.end,
          area: hint.targetArea, limit: 3, min_visits: 2 });
        d._risk = !!(bn.top && bn.top.length && bn.top[0].avg_dwell_sec >= 60);
      } catch (e) { console.warn('[历史] 拥堵风险查询失败:', (e && e.message) || e); }
    }
  } else if (intent === 'history_node_detail') {
    range = histRange(hint.time_range || '最近24小时');
    const nodeArea = String(hint.targetArea || '').split(',')[0];
    const nodeArg = hint.node_id
      ? ((nodeArea && hint.node_id.indexOf('_') < 0)
          ? nodeArea + '_' + hint.node_id : hint.node_id)
      : deviceTok(raw);
    if (!nodeArg) return { a: '请给出具体节点号（如 <b>Line_3_1C330RB</b>），我再查它的停留和过车量。', area: '', vehicle: '' };
    const p = { start_time: range.start, end_time: range.end, node_id: nodeArg,
                limit: 1, min_visits: 1, window: 300 };
    d = await histApi('/api/bottlenecks', p);
    if ((!d.top || !d.top.length) && d.latest_ts && d.latest_ts < range.start) {
      const span = Math.max(3600, range.end - range.start);
      const r2 = { start: d.latest_ts - span, end: d.latest_ts, label: histSpanLabel(span) };
      const d2 = await histApi('/api/bottlenecks', {
        start_time: r2.start, end_time: r2.end, node_id: nodeArg,
        limit: 1, min_visits: 1, window: 300 });
      if (d2.pass_count > 0 || (d2.top && d2.top.length)) { d = d2; range = r2; fb = true; }
    }
  } else if (intent === 'history_track') {
    clearTrackHighlight();                           // 每次新轨迹查询先清掉上一辆车的橙色轨迹
    const kw = hint.targetKeyword || lastContext.vehicleKeyword || deviceTok(raw);
    if (!kw) return null;
    d = await histApi('/api/vehicle_history', { query: kw, limit: 200 });
    if (!d.found || !(d.nodes || []).length) {
      return { a: '未找到该车轨迹（<b>' + escHtml(kw) + '</b> 在数据库中没有 place 事件）。',
               area: '', vehicle: kw, vehicleKeyword: kw, nodes: [] };
    }
    range = { start: 0, end: 0, label: '历史全量' };
    mapHi = highlightTrack(d.nodes);                 // 节点链到图后立刻橙色高亮 + 取景
  } else return null;

  const fallbackText = histFallbackText(intent, d, range, fb);
  let summary = fallbackText;
  // 0 台车 / 节点无有效停留时小模型容易编, 直接用本地成型文本;
  // 轨迹固定用本地成型话术, 保证“节点数/末节点/已停留”与 nodes 数组完全一致.
  const skipLlm = intent === 'history_track' ||
                  (intent === 'history_count' && !d.pass_count) ||
                  (intent === 'history_node_detail' && !(d.top || []).length);
  if (!skipLlm) {
    try {
      llmWait(true, '⏳ 本地 Ollama 正在总结历史统计…');
      const llm = await askHistorySummary(histBrief(intent, d, range, fb));
      if (histSummaryOk(intent, d, llm)) {
        summary = llm;
        const note = histStaleNote(d, range, fb);   // 模型常漏时效说明, 兜底补上
        if (note && summary.indexOf('无新数据') < 0 &&
            summary.indexOf('停在') < 0 && summary.indexOf('库中最近') < 0)
          summary = summary.replace(/[。.]?$/, '。') + note;
      } else {
        console.warn('[Ollama] 历史汇总未包含关键数字, 用本地成型文本');
      }
    } catch (e) {
      console.warn('[Ollama] 历史汇总失败, 用本地成型文本:', (e && e.message) || e);
    }
  }
  if (intent === 'history_track' && mapHi)
    summary += '已在左侧地图为您高亮该车的历史轨迹。';
  const icon = intent === 'history_bottleneck' ? '🚦 '
             : (intent === 'history_count' ? '📊 '
             : (intent === 'history_node_detail' ? '📍 ' : '🚗 '));
  const html = '<div class="qa-sum">' + icon + escHtml(summary) + '</div>' +
               histDetails(intent, d, range, fb);
  const area = intent === 'history_count'
    ? (hint.targetArea || (hint.node_id ? lineOfNode(hint.node_id) : ''))
    : (intent === 'history_node_detail'
        ? (d.node_id ? lineOfNode(d.node_id) : (hint.targetArea || ''))
        : '');
  const vehicle = intent === 'history_track' ? ((d.car && d.car.pin_code) || hint.targetKeyword || '') : '';
  const vehicleKeyword = intent === 'history_track'
    ? (hint.targetKeyword || (d.car && d.car.sled_no) || (d.car && d.car.pin_code) || '')
    : '';
  let track = null;
  if (intent === 'history_track') {
    const segs = d.segments || [];
    let sec = 0, ongoingSec = 0;
    for (const s of segs) {
      const n = Math.max(0, Number(s.dwell_sec) || 0);
      sec += n;
      if (s.ongoing) ongoingSec += n;
    }
    track = {
      vehicle: (d.car && d.car.sled_no) ? ('雪橇 ' + d.car.sled_no) : (vehicleKeyword || ''),
      nodes: (d.nodes || []).length, segs: segs.length, sec: sec,
      ongoing: !!(d.current && d.current.ongoing), ongoingSec: ongoingSec,
      truncated: !!d.truncated
    };
  }
  return { a: html, area: area, vehicle: vehicle, vehicleKeyword: vehicleKeyword,
           nodes: intent === 'history_track' ? (d.nodes || []) : [],
           track: track, chart: histChart(intent, d, range) };
}
const escHtml = s => String(s == null ? '' : s).replace(/[&<>]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
function llmWait(on, text) {
  let el = document.getElementById('llmwait');
  if (on) {
    if (!el) {
      qaClearHint();
      el = document.createElement('div');
      el.id = 'llmwait';
      (QABOX || document.body).appendChild(el);
    }
    el.textContent = text || '⏳ 本地 Ollama 解析中…';
    if (QABOX) QABOX.scrollTop = QABOX.scrollHeight;
  } else if (el) { el.remove(); }
}
// 车辆/设备查找: 用 LLM 提取的关键词(targetKeyword)精确查找; 查不到返回 null → 本地正则兜底
function vehicleAns(kw) {
  const K = norm(kw).replace(/["'“”]/g, '');
  if (!K) return null;
  const idx = vehIndex();
  let hit = idx.find(x => x.sled && x.sled === K);           // 雪橇号优先(≤6位)
  let kind = hit ? 'sled' : '';
  if (!hit) { hit = idx.find(x => (x.pin && x.pin === K) || (x.body && x.body === K)); kind = hit ? 'body' : ''; }
  if (hit) {
    const hidden = !locate(hit.node);
    return (kind === 'sled' ? '雪橇 ' + hit.v.sled_no + ' 在 ' : '车身 ' + (hit.v.pin_code || hit.v.body_no || '') + ' 在 ') +
           hit.label + '（' + hit.node + '）' +
           (hidden ? '（该节点不在当前视图，请切到 ' + lineOfNode(hit.node) + ' 线查看）' : '') +
           '<br>' + vehCardHtml(hit.v);
  }
  // 关键词是设备号/站名 → 定位该设备
  const nodes = ALL_NODES.filter(n => K.length >= 3 &&
    (norm(n.id).indexOf(K) >= 0 || norm(n.label).indexOf(K) >= 0));
  if (nodes.length === 1) {
    const id = nodes[0].id, vis = locate(id), v = VEHICLE[id];
    return (v ? LABEL[id] + ' 有车：<br>' + vehCardHtml(v) : LABEL[id] + '（' + id + '）当前无车停靠') +
           (vis ? '' : '（该节点不在当前视图，请切到 ' + lineOfNode(id) + ' 线查看）');
  }
  if (nodes.length > 1) return '匹配设备 ' + nodes.length + ' 个：' + nodes.map(x => LABEL[x.id]).slice(0, 8).join('、') + '（输入完整设备号可精确高亮）';
  return null;
}
// 设备号识别: "Line_4_1A025LT" / "1B100" 这类 token 应走设备查找, 防止被模型误判成区域统计
function deviceTok(q) {
  const U = String(q || '').toUpperCase();
  const m = U.match(/(?:LINE|EXIT)_\d+_[A-Z0-9]+/) ||
            U.match(/(?:^|[^A-Z0-9])(\d[A-Z]\d{3}[A-Z]{0,3})(?:[^A-Z0-9]|$)/) ||
            U.match(/(?:^|[^A-Z0-9])(\d{2,4}[A-Z]{2,3})(?=[^A-Z0-9]|$)/);
  return m ? (m[1] || m[0]) : '';
}
let _askPending = false;
async function askSmart(_q) {
  const raw = String(_q || '').trim();
  if (!raw || _askPending) return;
  if (handleTrackCommand(raw)) return;                     // 鹰眼追踪/取消指令: 本地即时响应, 不等 Ollama
  const qc = cleanQ(raw) || raw;
  const gs = globalScope(qc);
  // 省略式耗时追问不送 LLM, 直接按上一轮轨迹/路线上下文回答
  if (wantsElapsedFollowUp(qc, lastContext)) {
    const a = elapsedFollowAnswer(lastContext);
    if (a) { qa({ q: raw, a: a }); lastContext.lastQ = raw; return; }
  }
  const ctxLine = lastCtxBrief();                       // 上一轮问题+上下文 → 一并给 Ollama
  _askPending = true;
  llmWait(true, '⏳ 本地 Ollama(' + LLM_MODEL + ') 识别意图中…（超时 ' + Math.round(LLM_TIMEOUT / 1000) + 's 自动回退）');
  try {
    let hint = null;
    try { hint = fillHintContext(normLLMHint(await askLLM(raw, ctxLine)), qc); }
    catch (e) { console.warn('[Ollama] 解析失败, 回退本地正则:', (e && e.message) || e); }
    // 拿到新一轮意图后、发起任何请求前, 无条件撤掉上一次轨迹/路线高亮, 防止多次查询叠加
    clearTrackHighlight();
    // 模型明确不确定 → 不猜, 交给原有本地正则解析兜底
    if (!hint || hint.intent === 'unknown') { ask(raw); return; }
    if (hint && hint._vehicleMissing) {                  // 车不在实时状态: 明确告知, 不拿旧轨迹/旧路线硬套
      lastContext.vehicle = ''; lastContext.vehicleKeyword = '';
      qa({ q: raw, a: '未在实时状态中找到雪橇 <b>' + escHtml(hint._vehicleMissing) +
                   '</b> 的当前位置，无法计算到 ' + escHtml(hint.end || '目的地') + ' 的路线耗时。' });
      return;
    }
    if (hint && (hint.intent === 'history_bottleneck' ||
                 hint.intent === 'history_count' ||
                 hint.intent === 'history_node_detail' ||
                 hint.intent === 'history_track')) {        // ① 历史统计(MySQL 8766 API)
      try {
        const res = await historyAns(hint, raw);
        if (res) {
          lastContext.intent = hint.intent;
          if (res.area) lastContext.area = res.area;
          if (res.vehicle) lastContext.vehicle = res.vehicle;
          if (res.vehicleKeyword) lastContext.vehicleKeyword = res.vehicleKeyword;
          if (hint.intent === 'history_track') lastContext.track = res.track || null;
          const ansEl = qa({ q: raw, a: res.a });
          if (res.chart) renderQaChart(ansEl, res.chart);   // 文字总结之后淡入图表
          return;
        }
      } catch (e) {
        console.warn('[历史] 统计/汇总失败, 回退本地问答:', (e && e.message) || e);
      }
      ask(raw);                                          // API/Ollama 失败 → 原有本地兜底
      return;
    }
    if (hint && hint.intent === 'count') {               // ② 区域/全厂车辆统计
      const dt = deviceTok(raw);                         // 句中有具体设备号 → 优先按设备查
      const da = dt ? vehicleAns(dt) : null;
      if (da) { qa({ q: raw, a: da }); return; }
      const co = {};
      const a = countAns(qc, hint.targetArea, co);
      lastContext.intent = 'count';
      lastContext.area = co.area || '';
      if (hint.platform) lastContext.platform = hint.platform; else if (gs) lastContext.platform = '';
      qa({ q: raw, a: a });
      return;
    }
    if (hint && hint.intent === 'vehicle') {             // ③ 具体车辆/设备查找
      const vk = hint.targetKeyword || deviceTok(raw);
      const a = vehicleAns(vk);
      if (a) {
        lastContext.intent = 'vehicle';
        lastContext.vehicle = vk;
        lastContext.vehicleKeyword = /^\d{3,17}$/.test(String(vk || '')) ? vk : '';
        qa({ q: raw, a: a }); return;
      }
      // 当前视图没找到也记下号码, 供下一句"这辆车"继续查历史轨迹
      if (/^\d{3,17}$/.test(String(vk || ''))) {
        lastContext.vehicle = vk; lastContext.vehicleKeyword = vk;
      }
      ask(raw);                                          // 关键词查不到 → 本地正则兜底
      return;
    }
    // ④ 路线(route 或模型未给出 intent): 原有 Yen 路由 + 前3条汇总
    const meta = {};
    let r = null;
    try { r = hint ? routeAns(qc, hint, meta) : null; }  // 用 LLM 参数跑现有 Yen 路由, 回填前 3 条摘要
    catch (e) { console.warn('[Ollama] 应用解析结果失败, 回退本地正则:', (e && e.message) || e); }
    if (r) {
      lastContext.intent = 'route';
      if (hint && hint._vehRoute) { lastContext.vehicle = ''; lastContext.vehicleKeyword = ''; }
      if (meta.startTok) lastContext.routeStart = meta.startTok;   // 记下起终点, 供 "那条路线" 追问
      if (meta.endTok) lastContext.routeEnd = meta.endTok;
      lastContext.routeFrom = meta.from || lastContext.routeStart;
      lastContext.routeTo = meta.to || lastContext.routeEnd;
      if (meta.paths && meta.paths.length) lastContext.routeCost = meta.paths[0].cost;
      else lastContext.routeCost = null;
      if (meta.platform) lastContext.platform = meta.platform;
      let summary = '';
      if (meta.paths && meta.paths.length) {
        llmWait(true, '⏳ 本地 Ollama 正在总结最优路线…');
        try { summary = await askLLMSummary(buildPathBrief(meta)); }
        catch (e) { console.warn('[Ollama] 路线汇总失败, 直接显示详细数据:', (e && e.message) || e); }
      }
      if (summary) {                                     // 绿色加粗结论置顶, 详细数据折叠
        const ans =
          '<div class="qa-sum">🚚 ' +
          escHtml(summary) + '</div>' +
          '<details><summary style="cursor:pointer;color:#2a6fb0;font-size:12px">查看详细路径数据（前 ' +
          meta.paths.length + ' 条 / 共 ' + meta.total + ' 条可行）</summary>' +
          '<div style="margin-top:4px">' + r + '</div></details>';
        qa({ q: raw, a: ans });
      } else {
        qa({ q: raw, a: r });                            // 降级: 原详细文本
      }
      return;
    }
    ask(raw);                                            // 兜底: 原有解析/路由
  } finally {
    _askPending = false;
    llmWait(false);
    lastContext.lastQ = raw;                             // 供下一轮代词解析引用
  }
}

// 实时刷新: 只更新 RB 节点颜色/悬停卡片 + 右侧车辆清单, 不重建网络(保持缩放/拖拽/选中)
function updateLive(VEH, POS) {
  VEHICLE = VEH || {};
  POSITIONS = POS || {};
  trackPoll();                                     // 先按最新车辆状态刷新追踪目标/自动解锁
  const line = document.getElementById('line').value;
  fold = document.getElementById('fold').checked;
  if (NODE_DS) {
    const upd = [];
    for (const n of ALL_NODES) {
      if (!VISIBLE_ID.has(n.id)) continue;      // 只更新当前视图内的节点, 避免把被过滤的节点加回网络
      if (TRACK_HL.nodes[n.id]) continue;       // 轨迹高亮中的节点: 不被实时状态刷新覆盖成绿/灰
      if (n.tccell) {                          // TC 位置格(R/F): 位置信号=在此位则按有车绿/空灰, 不在=暗灰
        const at = POSITIONS[n.id] === '1';
        const v = VEHICLE[n.id];
        upd.push({ id: n.id,
          color: at ? (v ? { background: '#4CAF50', border: '#2E7D32' }
                         : { background: '#9E9E9E', border: '#333' })
                    : { background: '#616161', border: '#333' },
          borderWidth: (at && v) ? 2 : 1,
          title: (at && v) ? (n._baseTitle + '\n' + vehCard(v)) : n._baseTitle });
        continue;
      }
      if (n._tt90) {                           // 十字TT本体: DrPos2=90°竖放(瘦高+竖排数字), 否则横放(似普通滚床)
        const _v90 = POSITIONS[n._tt90] === '1';
        const v = VEHICLE[n.id];
        const o = { id: n.id };
        if (_v90) {
          o.label = ('' + n._num).split('').join('\n');
          o.widthConstraint = { minimum: 22, maximum: 28 };
          o.heightConstraint = { minimum: 60, maximum: 66 };
          o.font = { size: 10 };
        } else {
          o.label = n._baseLabel;
          o.widthConstraint = { minimum: 60, maximum: 63 };
          o.heightConstraint = null;
          o.font = { size: 11 };
        }
        o.color = v ? { background: '#4CAF50', border: '#2E7D32' }
                    : { background: '#9E9E9E', border: '#333' };
        o.borderWidth = v ? 2 : 1;
        o.title = v ? (n._baseTitle + '\n' + vehCard(v)) : n._baseTitle;
        upd.push(o);
        continue;
      }
      if (!n.vehpos) continue;                  // 普通车辆承载位(RB/LT/TT/SL/HE)
      const v = VEHICLE[n._parkId || n.id];     // 纯LT对位WT: 车挂在配对LT节点
      upd.push({
        id: n.id,
        color: v ? { background: '#4CAF50', border: '#2E7D32' }
                 : { background: '#9E9E9E', border: '#333' },
        borderWidth: v ? 2 : 1,
        title: v ? (n._baseTitle + '\n' + vehCard(v)) : n._baseTitle
      });
    }
    NODE_DS.update(upd);
  }
  renderVehList(line, fold);
  setStats();
  renderTrackBadges();                             // 跟随 updateLive 把光环/车号旗标移到新节点
  refreshTrackPick();                              // 点中的车开走了/换了 → 按钮跟着更新或消失
}

// 仅 HTTP 打开时可轮询(save 为 file:// 时 fetch 被浏览器禁止)
(function livePoll() {
  if (!/^https?:$/.test(location.protocol)) return;
  let busy = false;
  setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch('vehicle_state.json?_=' + Date.now(), { cache: 'no-store' });
      if (r.ok) {
        const d = await r.json();
        updateLive(d.vehicles || d, d.positions || {});   // 兼容旧版纯车辆映射(d 即车辆表)
        DECSTATE = d.dec || {};                           // 决策表实时值(富表悬浮层读用)
        if (_decCur) renderDecPop();                      // 层开着则随轮询刷新当页
      }
    } catch (e) {}
    busy = false;
  }, 1000);                                          // 与 EMOS 1s 采集同步 → 鹰眼旗标每秒随车移动
})();
// 加载实测类别基准表(config/edge_model.json; 仅 HTTP 下 fetch 可用, file:// 时 M 空 → edgeModel 回退抽象权值)
(function loadEdgeTime() {
  if (!/^https?:$/.test(location.protocol)) return;
  fetch('config/edge_model.json?_=' + Date.now(), { cache: 'no-store' })
    .then(r => r.ok ? r.json() : null).then(d => { M = d || {}; })
    .catch(e => {});
})();
document.getElementById('line').addEventListener('change', apply);
document.getElementById('fold').addEventListener('change', apply);
document.getElementById('ldm').addEventListener('change', apply);

// ---- 决策表富浮层(与现场 HMI 一致, demo 同款: 4页×12行分组表; 悬停/点按触发, 读实时 dec 段) ----
let HP = null, _decCur = null, _decPage = 1;
function decCell(dec, p, r, name) { const v = (DECSTATE[dec] || {})[p + '_' + r + '_' + name]; return (v == null ? '' : String(v)); }
// 真线 EMOS 只回 8 个决策字段(Dest/Use/Prior/Filtr01-05); demo 的 命中(Data1..5Match)/全配(FullMatch)
// 真线拿不到真实值, 富表不显示这两组。展示仍用 demo/HMI 同款分组表头 + 页签 + 启用行高亮 + 优先级星。
const DEC_GROUPS = [
  ['AS.I9999_Filtr01', 'AS.I9999_Filtr02', 'AS.I9999_Filtr03', 'AS.I9999_Filtr04', 'AS.I9999_Filtr05'],
  ['AS.I9999_Dest'], ['SB.S9999_Use']];
const DEC_GNAME = ['判断条件', '结果', '启用'];
const DEC_PLATE = { 'AS.I9999_Filtr01': 'VIN', 'AS.I9999_Filtr02': 'CodeF', 'AS.I9999_Filtr03': 'CodeB',
  'AS.I9999_Filtr04': '车型', 'AS.I9999_Filtr05': '平台', 'AS.I9999_Dest': '道号', 'SB.S9999_Use': '启用' };
function renderDecPage(p) {
  const grid = __Q('decgrid'); if (!grid) return;
  let h = '<table><tr><th>行</th>';
  for (let gi = 0; gi < DEC_GROUPS.length; gi++) h += `<th class="g" colspan="${DEC_GROUPS[gi].length}">${DEC_GNAME[gi]}</th>`;
  h += '</tr><tr><th></th>';
  for (const g of DEC_GROUPS) for (const f of g) h += `<th>${DEC_PLATE[f]}</th>`;
  h += '</tr>';
  for (let r = 1; r <= 12; r++) {
    const use = decCell(_decCur, p, r, 'SB.S9999_Use') === '1';
    h += `<tr class="${use ? 'use-row' : ''}"><td>${r}</td>`;
    for (const g of DEC_GROUPS) for (const f of g) {
      const v = decCell(_decCur, p, r, f);
      if (f === 'SB.S9999_Use') { h += `<td class="${use ? 'use-on' : 'use-off'}">${use ? '●' : '○'}</td>`; continue; }
            if (f === 'AS.I9999_Dest') { h += `<td class="hit">${(v == null || v === '') ? '−' : v}</td>`; continue; }
      if (f.startsWith('AS.I9999_Filtr')) { h += (v == null || v === '') ? '<td class="empty">−</td>' : `<td class="cond" title="${String(v).replace(/"/g, '')}">${String(v).slice(0, 10)}</td>`; continue; }
      h += (v == null || v === '') ? '<td class="empty">−</td>' : `<td class="f">${String(v).slice(0, 10)}</td>`;
    }
    h += '</tr>';
  }
  h += '</table>';
  grid.innerHTML = h;
}
// 决策点屏幕坐标: 优先用 vis 渲染器把画布坐标转 DOM; 失败则退回事件指针坐标(悬停时指针必在节点上)。
// 保证浮层跟着被悬停的决策点走, 不再落到固定的窗口中心。
function _decDomAnchor(id, ev) {
  const host = __Q('net');
  try {
    const dom = net ? _canvasToDOM(net.getPosition(id)) : null;
    if (dom && isFinite(dom.x) && isFinite(dom.y)) {
      const r = host.getBoundingClientRect();
      return { x: r.left + dom.x, y: r.top + dom.y };
    }
  } catch (e) {}
  if (ev && ev.pointer && ev.pointer.DOM) {
    const r = host.getBoundingClientRect();
    return { x: r.left + ev.pointer.DOM.x, y: r.top + ev.pointer.DOM.y };
  }
  return { x: window.innerWidth / 2, y: window.innerHeight / 3 };
}
function showDecPop(id, ev) {
  _decCur = id; _decPage = 1;
  const pop = __Q('decpop'), meta = DEC_META[id] || {}, dm = DEC_MAP[id] || {};
  const chips = Object.keys(dm).length ? Object.keys(dm).map(d => `<span class="chip">道${d}→${String(dm[d]).split('_').pop()}</span>`).join('') : '<span class="empty">无 dest_map</span>';
  let tab = '';
  for (let p = 1; p <= 4; p++) tab += `<button data-p="${p}" ${p === 1 ? 'class="on"' : ''}>页${p}</button>`;
  pop.innerHTML = `<h4>${meta.label || id} · ${meta.db || ''} · 转发自 ${meta.anchor || ''}</h4>` +
    `<div id="popsum">${chips}</div><div id="poptab">${tab}</div><div id="decgrid"></div>`;
  pop.style.display = 'block';                       // 先显示, 才能量到浮层实际尺寸
  const a = _decDomAnchor(id, ev), w = pop.offsetWidth, hh = pop.offsetHeight;
  let left = a.x + 22, top = a.y + 16;               // 首选: 决策点右下
  if (left + w > window.innerWidth - 8) left = a.x - 22 - w;    // 右侧放不下 → 翻到左下
  if (top + hh > window.innerHeight - 8) top = a.y - 18 - hh;   // 下方放不下 → 翻到右上
  left = Math.max(8, Math.min(left, window.innerWidth - 8 - w));
  top = Math.max(8, Math.min(top, window.innerHeight - 8 - hh));
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
  pop.onmouseenter = () => clearTimeout(HP);          // 浮层内 hover 保持(可点页面签), 离开关闭
  pop.onmouseleave = () => hidePop();
  pop.querySelectorAll('#poptab button').forEach(b => b.onclick = () => {
    pop.querySelectorAll('#poptab button').forEach(z => z.classList.remove('on')); b.classList.add('on'); renderDecPage(+b.dataset.p); });
  renderDecPage(1);
}
function hidePop() { const pop = __Q('decpop'); if (pop) pop.style.display = 'none'; }
document.body.insertAdjacentHTML('beforeend', '<div id="decpop"></div>');
const _isDec = id => (ALL_NODES.find(_ => _.id === id) || {}).type === 'DEC';

function wireNet() {
  net.on('afterDrawing', () => { positionTrackBadges(); positionTrackPick(); });   // 拖拽/缩放/容器改变后让旗标贴住节点
  // DEC 富表: 悬停/点按触发, 参数是 node(单数, 与点击的 nodes 数组不同); 路径规划模式(r0已设)时让位不打断
  net.on('hoverNode', p => { const id = p.node; if (id && _isDec(id) && r0 == null) { clearTimeout(HP); HP = setTimeout(() => showDecPop(id, p), 0); } });
  net.on('blurNode', () => { clearTimeout(HP); HP = setTimeout(hidePop, 350); });
  net.on('click', p => {
    const id = p.nodes && p.nodes[0];
    if (id && _isDec(id) && r0 == null) {
      hideTrackPick();                                  // DEC 节点 → 只弹决策表
      clearTimeout(HP); HP = setTimeout(() => showDecPop(id, p), 0);
    } else if (id) {
      hidePop();
      showTrackPick(id);                                // 有车设备 → 浮出"🎯 追踪 xxx"按钮
    } else {
      hideTrackPick();
      hidePop();
    }
  });
  net.on('doubleClick', p => {
    hideTrackPick();                                    // 双击开始规划路径, 收起追踪按钮
    let id = p.nodes[0];
    if (!id) {                          // 没精准点中节点 → 吸附最近节点(屏幕容差≈48px), 缩小后仍可点中
      const dp = p.pointer.DOM;
      let near = null, best = 48;
      for (const n of ALL_NODES) {
        try {
          const dom = _canvasToDOM(net.getPosition(n.id));
          const d = Math.hypot(dom.x - dp.x, dom.y - dp.y);
          if (d < best) { best = d; near = n.id; }
        } catch (e) {}
      }
      id = near;
    }
    if (!id) { out('未点中设备，请双击画面上的节点'); return; }
    if (r0 == null) { r0 = id; net.selectNodes([id]); out('起点 ' + LABEL[id] + ' — 请双击终点'); return; }
    const path = dijkstra(r0, id);
    try { net.selectNodes((path ? path.nodes : [id]).filter(x => VISIBLE_ID.has(x))); } catch (e) {}
    if (path) {
      const show = path.nodes.length > 24 ? fmtChain(path.nodes.slice(0, 24)).join(' → ') + ' …'
                                          : fmtChain(path.nodes).join(' → ');
      out('可达 · 耗时约' + path.cost + 's · ' + path.nodes.length + '步: ' + show);
    } else {
      out('不可达：' + (LABEL[r0] || r0) + ' → ' + (LABEL[id] || id));
    }
    r0 = null;
  });
  const inp = document.getElementById('search');
  inp.addEventListener('input', e => {
    const q = e.target.value.trim(); out('');
    if (!q) { net.unselectAll(); return; }
    const m = ALL_NODES.filter(n => n.id.includes(q) || n.label.includes(q));
    try { net.selectNodes(m.map(n => n.id).filter(x => VISIBLE_ID.has(x))); } catch (e) {}
    out('匹配 ' + m.length + ' 个: ' + m.map(n => n.label).slice(0, 12).join(' '));
  });
  const submitAsk = () => {                        // 统一发送: 取词 → 提问 → 立刻清空输入框
    const v = inp.value.trim();
    if (!v || _askPending) return;                 // 空输入/上一问仍在处理时不丢内容
    askSmart(v);
    inp.value = '';
    try { inp.focus(); } catch (e) {}
  };
  inp.addEventListener('keydown', e => {           // 回车提交对话式问答(保留实时输入高亮)
    if (e.key === 'Enter') { e.preventDefault(); submitAsk(); }
  });
  const askBtn = document.getElementById('askbtn');
  if (askBtn) askBtn.addEventListener('click', submitAsk);
  // 车辆卡片上的"🎯 追踪 xxx"按钮 → 直接锁定/解锁, 不经过输入框
  if (QABOX && !QABOX._vtWired) {
    QABOX._vtWired = true;
    QABOX.addEventListener('click', e => {
      const btn = e.target && e.target.closest ? e.target.closest('.vctrack') : null;
      if (!btn) return;
      e.preventDefault(); e.stopPropagation();
      const no = norm(btn.getAttribute('data-no') || '');
      if (!no) return;
      const on = trackList.indexOf(no) >= 0;
      btn.disabled = true;
      sysMsg(trackCmdReply({ action: on ? 'remove' : 'add', ids: [no] }));
      document.querySelectorAll('#qachat .vctrack').forEach(b => {   // 同步所有同车按钮文案
        const k = norm(b.getAttribute('data-no') || '');
        if (k !== no) return;
        const now = trackList.indexOf(k) >= 0;
        b.classList.toggle('on', now);
        b.textContent = (now ? '🔓 取消追踪 ' : '🎯 追踪 ') + k;
        b.disabled = false;
      });
      if (_trackPickNode) refreshTrackPick();
      positionTrackBadges();
    });
  }
  // 右侧对话侧边栏: 滑出/收起 + 地图自适应(仅绑定一次, apply 会被线/布局切换重复调用)
  const toggle = document.getElementById('chatToggle');
  const panel = document.getElementById('chatpanel');
  if (toggle && panel && !toggle._wired) {
    toggle._wired = true;
    const setChat = open => {
      document.body.classList.toggle('chat-open', open);
      panel.classList.toggle('open', open);
      toggle.classList.toggle('open', open);
      toggle.title = open ? '收起对话' : '打开产线智能助手';
      toggle.firstElementChild.textContent = open ? '➡️' : '💬';
      if (open) setTimeout(() => { try { inp.focus(); } catch (e) {} }, 320);
      setTimeout(() => fitNetToViewport(true), 310);      // 等收起/展开动画结束再重算画布
    };
    toggle.addEventListener('click', () => setChat(!panel.classList.contains('open')));
    const closeBtn = document.getElementById('chatClose');
    if (closeBtn) closeBtn.addEventListener('click', () => {
      setChat(false);
      clearTrackHighlight();                   // ❌ 关闭侧边栏 → 地图恢复默认绿/灰
    });
    window.addEventListener('resize', () => fitNetToViewport(false));
  }
}

// 侧边栏开合后让 vis 画布跟随容器宽度, 并保持整图可见(不被侧栏遮挡)
function fitNetToViewport(refit) {
  const el = document.getElementById('net');
  if (!net || !el) return;
  try {
    net.setSize(el.clientWidth + 'px', el.clientHeight + 'px');
    net.redraw();
    if (refit) net.fit({ animation: { duration: 300, easingFunction: 'easeInOutQuad' } });
  } catch (e) {}
}
apply();
"""
SCRIPT = SCRIPT.replace('__NODES__', json.dumps(all_nodes, ensure_ascii=False)) \
               .replace('__EDGES__', json.dumps(all_edges, ensure_ascii=False)) \
               .replace('__LINES__', json.dumps(line_ids)) \
               .replace('__EXIT_CODES__', json.dumps(list(EXIT_CODES))) \
               .replace('__LINE_ENTRY__', json.dumps(line_entries)) \
               .replace('__LINE_EXIT__', json.dumps(line_exits)) \
               .replace('__VEHICLE__', json.dumps(VEHICLE, ensure_ascii=False)) \
               .replace('__POSITIONS__', json.dumps(POSITIONS, ensure_ascii=False)) \
               .replace('__DEC_EMPTY__', '{}') \
               .replace('__DEC_MAP__', json.dumps(DEC_MAP, ensure_ascii=False)) \
               .replace('__DEC_META__', json.dumps(DEC_META, ensure_ascii=False)) \
               .replace('__AREA_ALIAS__', json.dumps(AREA_ALIAS, ensure_ascii=False, sort_keys=True)) \
               .replace('__AREA_TERMS__', json.dumps(sorted(AREA_ALIAS, key=len, reverse=True), ensure_ascii=False)) \
               .replace('__MODEL_INFO__', json.dumps(MODEL_INFO, ensure_ascii=False)) \
               .replace('__MODEL_ALIAS__', json.dumps(MODEL_ALIAS, ensure_ascii=False)) \
               .replace('__MODEL_SORT__', json.dumps(MODEL_SORT)) \
               .replace('__PLATFORMS__', json.dumps((CAR_MODEL.get('meta') or {}).get('platformOrder') or
                                                    [p.get('code') for p in CAR_MODEL.get('platforms', [])])) \
               .replace('__CAR_DICT__', json.dumps(CAR_DICT_TEXT, ensure_ascii=False)) \
               .replace('__AREA_GLOSSARY__', json.dumps(AREA_GLOSSARY_TEXT, ensure_ascii=False))

HEAD = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>输送系统全输送网 · 流程式</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
"""
STYLE = """<style>
  body{margin:0;font-family:Microsoft YaHei,sans-serif;background:#121212;color:#eee}
  /* 顶部工具条(普通文档流, 保证图例/控件任何情况下可见, 不盖图) */
  #topbar{background:#ffffff;color:#000;padding:6px 14px;box-shadow:0 2px 6px rgba(0,0,0,.4);line-height:1.7}
  #topbar .t1{font-size:15px;font-weight:bold}
  #topbar .t1 .leg{font-weight:normal;font-size:13px;margin-left:10px}
  #topbar .t2{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:13px}
  #topbar .t2 select{background:#fff;color:#000;border:1px solid #888;padding:2px 6px;border-radius:4px}
  #topbar label{display:inline-flex;align-items:center;gap:4px}
  #stats{color:#0a7d2f;font-weight:bold}
  #topbar .t3{font-size:12px;color:#333;border-top:1px dashed #bbb;margin-top:3px;padding-top:2px}
  .sw{display:inline-block;width:13px;height:13px;border-radius:3px;vertical-align:-2px;margin:0 2px;border:1px solid #666}
  /* 决策表富浮层(与现场 HMI 一致, demo_line_4 同款: 4页×12行分组表; vis tooltip 仅纯文本放不下) */
  #decpop{position:fixed;display:none;background:#0d141c;border:1px solid #33506a;border-radius:8px;
    z-index:100001;box-shadow:0 6px 26px rgba(0,0,0,.6);padding:10px;font-size:11px;width:720px;max-height:82vh;overflow:auto;color:#cfe3ff}
  #decpop h4{margin:0 0 6px;font-size:13px;color:#9cd0ff}
  #poptab,#pop .poptab,#decpop #poptab{display:flex;gap:4px;margin-bottom:8px;position:sticky;top:0;background:#0d141c;padding:2px 0}
  #decpop #poptab button{background:#1c2a3a;color:#cfe3ff;border:1px solid #2e4670;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px}
  #decpop #poptab button.on{background:#2f6fae;border-color:#4c9dff;color:#fff}
  #decpop #decgrid table{width:100%;border-collapse:collapse;font-size:10.5px}
  #decpop th,#decpop td{border:1px solid #22303f;padding:2px 3px;text-align:center;white-space:nowrap}
  #decpop th{background:#182638;color:#9db8d6}
  #decpop td.f{max-width:60px;overflow:hidden;text-overflow:ellipsis}
  #decpop .hit{background:#114a28;color:#b6ffc8;font-weight:700} #decpop .empty{color:#5a6b7d}
  #decpop .use-on{color:#6ef09a;font-weight:700} #decpop .use-off{color:#e06f6f}
  #decpop .cond{background:#0c2b45;color:#8fd3ff;font-weight:700}
  #decpop th.g{background:#12263f;color:#dfeaff;font-size:9px}
  #decpop tr.use-row td{background:rgba(110,240,154,.06)}
  #decpop #popsum{display:flex;gap:6px;margin:7px 0 4px;align-items:center;font-size:11px;color:#c9d7e8;flex-wrap:wrap}
  #decpop .chip{background:#223144;border:1px solid #34475f;padding:1px 7px;border-radius:10px;color:#d5e4f5}
  #decpop .empty2{color:#5a6b7d}
  /* 右下提示 */
  #hint{position:fixed;right:10px;bottom:14px;z-index:99999;background:#fafafa;color:#333;
    padding:6px 10px;border-radius:8px;font-size:12px;transition:right .28s ease}
  #stdout{position:fixed;left:12px;bottom:14px;z-index:99998;width:340px;color:#7CFC94;font:12px mono}
  /* 右侧对话: 悬浮把手(平时贴边) */
  #chatToggle{position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:100003;
    width:42px;height:96px;border:none;border-radius:12px 0 0 12px;background:#2d6cdf;color:#fff;
    font-size:16px;cursor:pointer;box-shadow:-2px 2px 10px rgba(0,0,0,.35);
    transition:right .28s ease,background .2s;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:5px;padding:0}
  #chatToggle span{font-size:11px;writing-mode:vertical-rl;letter-spacing:2px}
  #chatToggle:hover{background:#3b7ef0}
  #chatToggle.open{right:370px}
  /* 右侧对话侧边栏: 白底黑字, 100vh, 平滑滑出 */
  #chatpanel{position:fixed;right:0;top:0;width:370px;height:100vh;z-index:100002;
    background:#fff;color:#111;box-shadow:-6px 0 24px rgba(0,0,0,.28);display:flex;flex-direction:column;
    transform:translateX(100%);visibility:hidden;transition:transform .28s ease,visibility .28s}
  #chatpanel.open{transform:translateX(0);visibility:visible}
  #chatpanel .ch-head{display:flex;align-items:center;justify-content:space-between;
    padding:14px 16px;border-bottom:1px solid #e3e6ea;background:#f7f9fb;flex:0 0 auto}
  #chatpanel .ch-head b{font-size:15px;color:#1a1a1a}
  #chatpanel #chatClose{border:none;background:transparent;font-size:16px;cursor:pointer;
    color:#555;padding:2px 6px;border-radius:6px;line-height:1.2}
  #chatpanel #chatClose:hover{background:#e6e9ee}
  /* 对话历史区(可滚动, 白底黑字) */
  #qachat{flex:1 1 auto;overflow-y:auto;padding:12px 14px;background:#fff;color:#111;font-size:13px;
    box-sizing:border-box;min-height:0}
  #qachat .qa-empty{color:#8a8f98;line-height:1.7}
  #qachat .qa-q{background:#eef3fa;color:#182430;font-weight:bold;margin:10px 0 4px;
    padding:7px 10px;border-radius:8px;line-height:1.5}
  #qachat .qa-a{color:#111;white-space:pre-wrap;margin:0 0 6px;padding:2px;line-height:1.65}
  #qachat .qa-q:first-child{margin-top:0}
  #qachat .qa-sum{color:#0a8f3c;font-weight:bold;font-size:13.5px;line-height:1.55;margin:2px 0 7px}
  #qachat details summary{color:#2a6fb0}
  #chatpanel .ch-foot{flex:0 0 auto;display:flex;gap:8px;padding:10px 12px;
    border-top:1px solid #e3e6ea;background:#f7f9fb}
  #chatpanel #search{flex:1 1 auto;min-width:0;padding:9px 11px;border:1px solid #c6ccd4;
    border-radius:8px;background:#fff;color:#000;font-size:13px}
  #chatpanel #search:focus{outline:none;border-color:#2d6cdf;box-shadow:0 0 0 2px rgba(45,108,223,.15)}
  #chatpanel #askbtn{flex:0 0 auto;padding:9px 16px;border:1px solid #2d6cdf;border-radius:8px;
    background:#2d6cdf;color:#fff;font-size:13px;cursor:pointer}
  #chatpanel #askbtn:hover{background:#3b7ef0}
  #llmwait{color:#2a6fb0;font-size:12px;margin-top:6px}
  /* 历史统计图表卡片: 白底半透明, 文字总结之后平滑淡入 */
  #qachat .qa-chart{height:190px;margin:8px 0 4px;padding:2px;background:rgba(255,255,255,.97);
    border:1px solid #e6ebf2;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.10);
    opacity:0;transform:translateY(8px);
    transition:opacity .5s ease,transform .5s ease;box-sizing:border-box}
  #qachat .qa-chart.on{opacity:1;transform:none}
  #qachat .qa-chart-miss{color:#98a1ad;font-size:12px;line-height:186px;text-align:center}
  #qachat .qa-sys{color:#42627f;background:#f2f7fc;border-left:3px solid #7eb6e8;
    margin:8px 0 4px;padding:6px 9px;border-radius:6px;font-size:12px;line-height:1.55}
  /* 鹰眼追踪: 地图上的双色光环 + 随车移动的车号旗标 */
  #trackLayer{position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;
    z-index:20;overflow:hidden}
  .track-halo{position:absolute;display:block;box-sizing:border-box;border:2px solid var(--tc);
    border-radius:7px;box-shadow:0 0 0 2px rgba(255,255,255,.16),0 0 10px var(--tc);
    animation:trackPulse 1.35s ease-in-out infinite}
  .track-badge{position:absolute;transform:translate(-50%,-100%);z-index:2;
    padding:2px 8px;border-radius:9px;border:1px solid #fff;white-space:nowrap;
    font:700 12px/1.55 Microsoft YaHei,sans-serif;letter-spacing:.2px;
    box-shadow:0 2px 10px rgba(0,0,0,.55),0 0 12px var(--tc)}
  .track-badge:after{content:'';position:absolute;left:50%;bottom:-6px;transform:translateX(-50%);
    border:6px solid transparent;border-top-color:var(--tc)}
  .track-badge.below{transform:translate(-50%,0)}
  .track-badge.below:after{bottom:auto;top:-6px;border-top-color:transparent;border-bottom-color:var(--tc)}
  /* 追踪信号暂时丢失(宽限期内): 旗标半透明闪烁提示数据延迟, 不删除 */
  .track-badge.lost{opacity:.42;animation:trackBlink 1s ease-in-out infinite}
  @keyframes trackBlink{0%,100%{opacity:.22}50%{opacity:.72}}
  @keyframes trackPulse{
    0%,100%{box-shadow:0 0 0 2px rgba(255,255,255,.16),0 0 8px var(--tc)}
    50%{box-shadow:0 0 0 3px rgba(255,255,255,.28),0 0 20px var(--tc)}
  }
  /* 地图点选 → 追踪按钮(浮在点上, 免打字) */
  #trackpick{position:absolute;display:none;transform:translate(-50%,-100%);
    z-index:30;cursor:pointer;padding:3px 10px;border-radius:9px;
    border:1px solid #00FF66;background:#0b3d1f;color:#9BFFC4;white-space:nowrap;
    font:700 12px/1.6 Microsoft YaHei,sans-serif;letter-spacing:.2px;
    box-shadow:0 2px 10px rgba(0,0,0,.6),0 0 10px rgba(0,255,102,.55)}
  #trackpick:hover{background:#135229}
  #trackpick.on{border-color:#FFD600;color:#FFEC9E;background:#40350b;
    box-shadow:0 2px 10px rgba(0,0,0,.6),0 0 10px rgba(255,214,0,.55)}
  #trackpick.below{transform:translate(-50%,0)}
  /* 对话里车辆卡片上的追踪按钮 */
  #qachat .vctrack{margin-top:4px;cursor:pointer;padding:2px 9px;border-radius:8px;
    border:1px solid #00A64A;background:#0e3d22;color:#9BFFC4;
    font:700 12px/1.6 Microsoft YaHei,sans-serif;letter-spacing:.2px}
  #qachat .vctrack:hover{background:#135229}
  #qachat .vctrack.on{border-color:#B59A00;background:#3d340b;color:#FFEC9E}
  /* 侧边栏弹出时地图收缩, 不被完全遮挡 */
  #net{transition:width .28s ease}
  body.chat-open #net{width:calc(100vw - 370px) !important}
  body.chat-open #veh{right:380px}
  body.chat-open #hint{right:380px}
  /* 运行时车辆面板(右侧) */
  #veh{position:fixed;right:10px;top:12px;z-index:99999;width:235px;max-height:70vh;overflow:auto;
    background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:8px 10px;font-size:12px;
    transition:right .28s ease}
  #veh .vh{color:#FFD835;font-weight:bold;margin-bottom:6px}
  #veh #vehlist div{padding:2px 0;border-bottom:1px dashed #333}
  #veh.hide{display:none}
  #net{background:#121212}
  #net canvas{background:#121212}
</style>"""
BODY = """</head><body>
<div id="topbar">
  <div class="t1">输送系统全输送网 · 流程式
    <span class="leg"><span class="sw" style="background:#9E9E9E"></span>辊床(空) <span class="sw" style="background:#4CAF50;border-color:#2E7D32"></span>辊床(有车) <span class="sw" style="background:#FFB74D"></span>LT高
      <span class="sw" style="background:#FF8A65"></span>LT低 <span class="sw" style="background:#00BCD4"></span>LT中
      <span class="sw" style="background:#E53935"></span>CP <span class="sw" style="background:#AB47BC"></span>TC
      <span class="sw" style="background:#90A4AE"></span>外接 <span class="sw" style="background:#5C6BC0"></span>TT
      <span class="sw" style="background:#00FF66;border-color:#C6FFDD;box-shadow:0 0 5px #00FF66"></span>追踪①
      <span class="sw" style="background:#2979FF;border-color:#D6E4FF;box-shadow:0 0 5px #2979FF"></span>追踪②</span>
  </div>
  <div class="t2">
    线: <select id="line"><option value="ALL">全部</option>""" + \
       "".join(f'<option value="{pf}">{pf}</option>' for pf in lines) + """</select>
    布局: <select id="ldm"><option value="flow">流程式·流向</option><option value="force">力导向</option></select>
    <label><input type="checkbox" id="fold" checked>主干(LT折叠)</label>
    <span id="stats">加载中…</span>
  </div>
  <div class="t3"><b>各线节点/边 · 入口/出口：</b>""" + "；".join(line_info) + """</div>
</div>
<div id="net" style="position:relative;width:100vw;height:calc(100vh - 74px)"></div>
<div id="hint">双击路线：先点起点，再点终点，高亮最短路径并显示耗时（可跨线）</div>
<button id="chatToggle" type="button" title="打开产线智能助手"><span>💬</span><span>对话</span></button>
<div id="chatpanel">
  <div class="ch-head"><b>产线智能助手</b><button id="chatClose" type="button" title="收起">❌</button></div>
  <div id="qachat"><div class="qa-empty">可问：Line_1入口到Line_4出口怎么走 / Line_2有几台车 / 雪橇6210在哪<br>追踪：跟踪雪橇1234 / 跟踪雪橇5678（最多2台）/ 取消全部跟踪</div></div>
  <div class="ch-foot">
    <input id="search" placeholder="输入雪橇号/车身PIN/设备号或问题，如“跟踪雪橇1234”…">
    <button id="askbtn" type="button">发送</button>
  </div>
</div>
<div id="stdout"></div>
<script>""" + SCRIPT + "</script></body></html>"

OUT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'conveyor_visual.html'))
with open(OUT, 'w', encoding='utf-8') as f:
    f.write(HEAD + LIB_TAG + STYLE + BODY)

print("已生成:", OUT)
print("节点数:", len(all_nodes), "边数:", len(all_edges), "| 线:", ", ".join(lines))