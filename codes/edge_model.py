# -*- coding: utf-8 -*-
"""
三期边耗时【类别基准表】Python 侧镜像, 与前端 make_line_viz.js 的 edgeModel() 完全同逻辑。

数据源唯一 = config/edge_model.json(用户现场实测的类别值 + exceptions 特例)。
读 build_line 合成的全网图(节点/边 type, can_park), 按边类型 + 端点节点类型分类取秒。

用法:
  from edge_model import edge_time, build_route_cost
  edge_time('Line_1_1A060RB', 'Line_1_1A065RB')        # -> 14
  build_route_cost(['Line_1_1A060RB', ... , 'Line_4_...'])  # 沿节点链累加(不含堵塞)
"""
import json, os, re

from common import line_of as _line_of

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_PATH = os.path.normpath(os.path.join(_HERE, '..', 'config', 'edge_model.json'))

_model = None


def load_model():
    global _model
    if _model is None:
        with open(_MODEL_PATH, 'r', encoding='utf-8') as f:
            _model = json.load(f)
    return _model


def get_graph():
    """构建全网(共享接口EXT合并), 返回 (node_type, edge_type, line_of, park)。惰性单例。"""
    if getattr(get_graph, '_cached', None) is not None:
        return get_graph._cached
    import build_line as BL
    lines = BL.available_lines()
    G = BL.build_line(lines[0])[1]['G']
    import networkx as nx
    for pf in lines[1:]:
        G = nx.compose(G, BL.build_line(pf)[1]['G'])
    nt = {n: (G.nodes[n].get('type') or '') for n in G.nodes}
    et = {(a + '→' + b): (d.get('type') or 'roller')
          for a, b, d in G.edges(data=True)}
    line = {n: _line_of(n) for n in G.nodes}
    park = {n: bool(G.nodes[n].get('can_park', True)) for n in G.nodes}
    get_graph._cached = (nt, et, line, park)
    return get_graph._cached


def edge_time(u, v, meta=None):
    """边 u→v 的类别基准秒。meta 可选 (nt,et,line,park); 不传则自建图。"""
    M = load_model()
    k = u + '→' + v
    if M.get('exceptions') and k in M['exceptions']:
        return M['exceptions'][k]
    if meta is None:
        nt, et, line, park = get_graph()
    else:
        nt, et, line, park = meta
    tu, tv = nt.get(u, ''), nt.get(v, '')
    xl = line.get(u) != line.get(v)
    ety = et.get(k, 'roller')
    g = lambda *p, d=0: M.get(p[0], d)
    if ety == 'lift':
        return M.get('lift', 6)
    if ety == 'cross':
        return M.get('cross', g('roller'))
    if ety == 'load':
        if tv == 'CP_pos' or tu == 'CP_pos':
            return M.get('lt_cp', 18)
        if tu == 'RB' and tv == 'LT_high':
            return M.get('rb_lt', 14)
        if tu == 'LT_high' and tv == 'RB':
            return M.get('lt_rb', 12)
        return M.get('rb_lt', 14)
    if tu == 'TT' or tv == 'TT':
        return M.get('tt', 10)
    if tu == 'SL' or tv == 'SL':
        return M.get('sl', 10)
    if tu == 'TC_pos' and tv == 'TC_pos':
        return M.get('tc', 16)
    if ety == 'ext':
        return M.get('cross_line', M.get('roller', 14)) if xl else M.get('roller', 14)
    if xl:  # 跨线: 无过渡按跳; 仅当端点是过渡(不可停)时 +6
        pen = M.get('cross_penalty', 0) if (not park.get(u, True) or not park.get(v, True)) else 0
        return M.get('cross_line', 14) + pen
    return M.get('roller', 14)


def node_type(n, meta=None):
    if meta is None:
        meta = get_graph()
    nt, *_ = meta
    return nt.get(n, '')


if __name__ == '__main__':
    M = load_model()
    nt, et, line, park = get_graph()
    print('edge_model.json 类别:', {k: v for k, v in M.items() if not isinstance(v, (dict,))})
    print('EXCEPTIONS:', M.get('exceptions'))
    # 自检几个代表边
    for u, v in [
        ('Line_4_1B080RB', 'Line_4_1B095RB'),          # RB→RB 相邻应 14
        ('Line_4_1B105RB', 'Line_4_1B105LT_high'),      # RB→LT 应 14
        ('Line_4_1B105LT_high', 'Line_4_1B105RB'),      # LT→RB 应 12
        ('Line_4_1B105LT_high', 'Line_4_1B105LT_mid'),  # lift 应 0
        ('Line_4_1B105LT_low', 'Line_4_1B105CP'),       # LT⇄CP(若有)应 18
        ('Line_2_1D335RB', 'Line_4_1A025RB'),           # 跨线 RB→RB 应 14(+6 if 过渡)
    ]:
        if u in nt and v in nt:
            print(f'  {u} → {v}  = {edge_time(u, v, (nt, et, line, park))}s')
        else:
            print(f'  {u} → {v}  (图中无此边/节点, 跳过)')