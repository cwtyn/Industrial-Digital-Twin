# -*- coding: utf-8 -*-
"""
FJSP-T 共享建图引擎 — 把"线配置(config_XX.py) + 通用机理"转成有向加权图。
- 每条线 = 一个纯数据 config 模块, 经本引擎统一建图, 保证各线口径一致。
- 现场概念/机理变更改本文件; 单线拓扑改对应 config 即可, 互不牵动。
- 图用 NetworkX DiGraph, 权重单位秒; Dijkstra(非负权)为寻路主算法。
"""
import re

import networkx as nx

# 默认权重(秒, 示意; 现场节拍校准后在各线 config 里覆盖)
W_DEFAULT = {'roller':3,'cross':4,'lift':3,'load':1,'tc':4,'repos':2,'tt':4,'sl':4}

# 泛化后的线代号含下划线(Line_1), 不能再简单 split('_')[0] 取线前缀。
# 统一用下列帮助函数拆节点ID, 兼容旧的 Line_1 形式与外部占位节点。
LINE_CODES = ('Line_1', 'Line_2', 'Line_3', 'Line_4')
EXIT_CODES = ('Exit_1', 'Exit_2', 'Exit_3')
_LINE_PREFIX_RE = re.compile(r'^(Line_\d+|Exit_\d+)_')

def split_node(nid):
    """节点ID -> (线/区域代号, 设备段)。'Line_1_1A045RB' -> ('Line_1','1A045RB')。

    用正则整体剥离前缀, 兼容未配置的外部线(Line_5/Line_6 等)与新旧 Line_x 形式。
    """
    s = str(nid or '')
    m = _LINE_PREFIX_RE.match(s)
    if m:
        return m.group(1), s[m.end():]
    if '_' in s:
        return tuple(s.split('_', 1))
    return '', s

def line_of(nid):
    return split_node(nid)[0]

def dev_of(nid):
    return split_node(nid)[1]

def build(cfg):
    """cfg 为 dict(见各 config_XX.py 顶注)。返回 dict: G(图) + 节点ID帮助函数。"""
    pf = cfg['prefix']; z = cfg['zones']
    W = dict(W_DEFAULT); W.update(cfg.get('weights', {}))
    # ---- 节点ID帮助(统一命名: 线前缀+段号+设备号+类型后缀) ----
    def rb(d):   return f"{pf}_{z[d]}{d:03d}RB"
    def lth(d):  return f"{pf}_{z[d]}{d:03d}LT_high"
    def ltm(d):  return f"{pf}_{z[d]}{d:03d}LT_mid"
    def ltl(d):  return f"{pf}_{z[d]}{d:03d}LT_low"
    def ltp(d):  return f"{pf}_{z[d]}{d:03d}LT"      # 纯LT存位: 停车节点(名以LT结尾, X1=X2=LT)
    def wt(d):   return f"{pf}_{z[d]}{d:03d}WT"      # 纯LT存位: 对位节点(CP停靠站, 无订阅)
    def cp(cpid, d): return f"{pf}_{cpid}_at_{d:03d}"
    def tcp(n, td=None):
        td = cfg.get('tc', {}).get('dev') if td is None else td
        return f"{pf}_{z[td]}{td:03d}TC_pos{n}"
    H = dict(rb=rb, lth=lth, ltm=ltm, ltl=ltl, ltp=ltp, wt=wt, cp=cp, tcp=tcp, W=W)

    G = nx.DiGraph()
    def R(n, tp): G.add_node(n, type=tp); return n

    pos3 = set(cfg.get('pos3', []))

    # 1) 辊床主线: 每链为"向前"顺序; RB 默认单向(沿链向前), 无反向回链。
    #    需双向的(若描述明确)用 cfg['bi_rb'] 补反向边。
    bi = set(cfg.get('bi_rb', []))
    for chain in cfg['chains']:
        for a, b in zip(chain, chain[1:]):
            R(rb(a),'RB'); R(rb(b),'RB')
            G.add_edge(rb(a), rb(b), type='roller', weight=W['roller'])
            if a in bi or b in bi:
                G.add_edge(rb(b), rb(a), type='roller', weight=W['roller'])

    # 1.5) 十字转台脊柱: cfg['cross_tt'] = 十字脊柱设备序列(如 [250,260,340,350,355]),
    #     相邻两两之间建**双向 tt 边**(0°主链边走 1)链, 这里的 90°叉臂可达性; 前进后退都可达)。
    #     TT 类型/过渡标记由 tt_devs / transition_rb 在 5)6) 覆盖。满足"边全建、位置信号只控显示"。
    for a, b in zip(cfg.get('cross_tt', []), cfg.get('cross_tt', [])[1:]):
        R(rb(a),'RB'); R(rb(b),'RB')
        G.add_edge(rb(a), rb(b), type='tt', weight=W['tt'])
        G.add_edge(rb(b), rb(a), type='tt', weight=W['tt'])

    # 2) LT(=RB组合): 高位=主线同层(直通/回主线); 低位=上CP发车/穿过; 中位(仅3位)=接车对位
    for dev in cfg.get('lt_combos', []):
        R(rb(dev),'RB'); R(lth(dev),'LT_high'); R(ltl(dev),'LT_low')
        if dev in pos3:
            R(ltm(dev),'LT_mid')
            for a, b in [(lth(dev), ltm(dev)), (ltm(dev), ltl(dev))]:
                G.add_edge(a, b, type='lift', weight=W['lift'])
                G.add_edge(b, a, type='lift', weight=W['lift'])
        else:
            G.add_edge(lth(dev), ltl(dev), type='lift', weight=W['lift'])
            G.add_edge(ltl(dev), lth(dev), type='lift', weight=W['lift'])
        G.add_edge(rb(dev), lth(dev), type='load', weight=W['load'])
        G.add_edge(lth(dev), rb(dev), type='load', weight=W['load'])

    # 2.5) 纯LT存位(=LT/LT, 无RB): 停车节点LT + 对位节点WT双向; CP停靠落在WT(见3)
    for d in cfg.get('pure_lt', []):
        R(ltp(d), 'LT'); R(wt(d), 'WT')
        G.add_edge(ltp(d), wt(d), type='load', weight=W['load'])
        G.add_edge(wt(d), ltp(d), type='load', weight=W['load'])

    # 3) CP 横移链(单向): 站序列=连接顺序; 3位发车低位上CP/接车中位对位, 2位均低位
    purelt_set = set(cfg.get('pure_lt', []))
    for _z, cpid, stations in cfg.get('cp_groups', []):
        for dev in stations:
            R(cp(cpid, dev),'CP_pos')
            if dev in purelt_set:                       # 纯LT存位: CP 停靠在对位节点 WT
                G.add_edge(wt(dev), cp(cpid, dev), type='load', weight=W['load'])
                G.add_edge(cp(cpid, dev), wt(dev), type='load', weight=W['load'])
            elif dev in pos3:
                G.add_edge(ltl(dev), cp(cpid, dev), type='load', weight=W['load'])
                G.add_edge(cp(cpid, dev), ltm(dev), type='load', weight=W['load'])
            else:
                G.add_edge(ltl(dev), cp(cpid, dev), type='load', weight=W['load'])
                G.add_edge(cp(cpid, dev), ltl(dev), type='load', weight=W['load'])
        for a, b in zip(stations, stations[1:]):
            G.add_edge(cp(cpid, a), cp(cpid, b), type='cross', weight=W['cross'])
    # 跨CP联动桥接等自定义单向边(如 420CP@430 → 440CP@445)
    for a, b, et in cfg.get('extra_edges', []):
        G.add_edge(a, b, type=et, weight=W.get(et, W['cross']))

    # 4) 横移车 TC(载 RB): 接车仅 Reverse(pos1) -> pos1(链A) / pos1->pos2(链B)
    if 'tc' in cfg:
        td = cfg['tc']['dev']; docks = cfg['tc']['docks']
        R(rb(td),'RB')
        for n, lane in docks.items():
            R(tcp(n, td),'TC_pos'); R(rb(lane),'RB')
        G.add_edge(rb(td), tcp(1, td), type='tc', weight=W['tc'])
        G.add_edge(tcp(1, td), rb(docks[1]), type='load', weight=W['load'])
        G.add_edge(tcp(1, td), tcp(2, td), type='repos', weight=W['repos'])
        G.add_edge(tcp(2, td), rb(docks[2]), type='load', weight=W['load'])

    # 5) 功能件类型标注: SL(跨层) / TT(转台90°)
    for d in cfg.get('sl_devs', []):
        if G.has_node(rb(d)): G.nodes[rb(d)]['type'] = 'SL'
    for d in cfg.get('tt_devs', []):
        if G.has_node(rb(d)): G.nodes[rb(d)]['type'] = 'TT'
    #   转台分支出线(如 135TT 转90° -> Line_6)
    for dev, ext in cfg.get('ext_turn', []):
        R(ext, 'EXT'); G.add_edge(rb(dev), ext, type='tt', weight=W['tt'])

    # 6) 过渡(不可停车)辊床标注
    for d in cfg.get('transition_rb', []):
        if G.has_node(rb(d)): G.nodes[rb(d)]['can_park'] = False

    # 7) 外接输送线占位/真实邻线接口
    for ext, dev in cfg.get('ext_exits', []):      # 本线 -> 邻线(出)
        R(ext, 'EXT')
        if G.has_node(rb(dev)):
            G.add_edge(rb(dev), ext, type='ext', weight=W['roller'])
    for ext, dev in cfg.get('ext_inbound', []):    # 邻线 -> 本线(入/反向接口)
        R(ext, 'EXT')
        if G.has_node(rb(dev)):
            G.add_edge(ext, rb(dev), type='ext', weight=W['roller'])

    return dict(G=G, rb=rb, lth=lth, ltm=ltm, ltl=ltl, ltp=ltp, wt=wt, cp=cp, tcp=tcp, W=W)