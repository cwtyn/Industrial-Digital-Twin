#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""布局编辑器·阶段A 自包含建图引擎（纯 stdlib，不依赖 E: codes/）。

用法:
    python graph.py            # 读 samples/ 或 ./spec.json，校验 + 生成 preview_data.js + 打印诊断
    python graph.py spec.json  # 指定线路规格文件

输入: 线路规格 spec JSON（editor.html 导出，schema 见 spec_schema.md）
输出: preview_data.js（前端 preview.html 用 <script src> 载入，file:// 可用）
校验: 元素 id 唯一 / 线前缀一致 / 边端点在场 / 无重复边 / 连通性(从首元素可达) 提示。
"""
import json, sys, os, math, re

DEFAULT_SCENE = (900, 640)


def short_id(full_id):
    """Line_5_1A040TC -> 1A040TC; 兼容无前缀设备号。"""
    s = str(full_id or '')
    return re.sub(r'^(?:Line|Exit)_\d+_', '', s)


def line_id_of(full_id):
    m = re.match(r'^(Line_\d+|Exit_\d+)', str(full_id or ''))
    return m.group(1) if m else ''

# 节点显示（与 make_line_viz 大致对齐，仅本目录内用；自包含，不从 codes 引）
KIND_STYLE = {
    # 高度 20 与 RB 一致；宽度统一 66（=TC），所有元素等长、完整显示标签
    'RB':  dict(shape='rect',  color='#E53935',  w=66, h=20),   # 辊床
    'WT':  dict(shape='rect',  color='#BDBDBD',  w=66, h=20),   # 离线/上线(系统边界)
    'LT':  dict(shape='box',   color='#FB8C00',  w=66, h=20),   # 升降/工位
    'TT':  dict(shape='box',   color='#00897B',  w=66, h=20),   # 转台
    'SL':  dict(shape='box',   color='#039BE5',  w=66, h=20),   # 剪式升降
    'HE':  dict(shape='box',   color='#8D6E63',  w=66, h=20),   # 提升机
    'CP':  dict(shape='ellipse', color='#E53935', w=66, h=20),  # 横移链
    'TC':  dict(shape='rect',  color='#AB47BC', w=66, h=20),    # 横移小车：横向轨道长条(标签水平)
    'EXT': dict(shape='rect',  color='#616161',  w=66, h=20),   # 跨线接口：出口(车由此出)
    'ENT': dict(shape='rect',  color='#515151',  w=66, h=20),   # 跨线接口：入口(车由此进)
    'DEC': dict(shape='rect',  color='#29B6F6', w=34, h=34),    # 决策点(分道DecList，非输送设备；方形·天蓝)
}
EDGE_STYLE = {
    'roller': dict(color='#666', dashes=False, width=2),
    'load':   dict(color='#1976D2', dashes=False, width=2),
    'cross':  dict(color='#E53935', dashes=False, width=3),
    'lift':   dict(color='#9575CD', dashes=True,  width=2),
    'tt':     dict(color='#00897B', dashes=False, width=2),
    'sl':     dict(color='#039BE5', dashes=False, width=2),
    'tc':     dict(color='#AB47BC', dashes=True,  width=2),
    'ext':    dict(color='#424242', dashes=True,  width=2),
}


def load_spec(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate(spec, check_prefix=True):
    errs = []
    line = spec.get('line', '')
    if not line:
        errs.append('缺 line（输送编号）')
    els = spec.get('elements') or []
    ids = {}; kinds = {}
    for i, e in enumerate(els):
        eid = e.get('id')
        if not eid:
            errs.append(f'element#{i} 缺 id'); continue
        if eid in ids:
            errs.append(f'id 重复: {eid}')
        ids[eid] = i; kinds[eid] = e.get('kind')
        if check_prefix and line and not eid.startswith(line + '_'):
            errs.append(f'{eid} 未以线前缀 {line}_ 开头')
        if e.get('kind') not in KIND_STYLE:
            errs.append(f'{eid} kind 未知: {e.get("kind")}')
        if e.get('kind') == 'LT' and (e.get('attrs') or {}).get('pair') not in (None, 'RB', 'WT'):
            errs.append(f'{eid} LT 配位 pair 只能是 RB/WT')
        if e.get('kind') in ('EXT', 'ENT') and not ((e.get('attrs') or {}).get('partner') or '').strip():
            errs.append(f'{eid} {e["kind"]} 未填跨线对端(partner)')
    edges = spec.get('edges') or []
    seen = set()
    for j, ed in enumerate(edges):
        frm, to = ed.get('from'), ed.get('to')
        if frm in ids and to in ids:
            key = (frm, to)
            if key in seen:
                errs.append(f'重复边 {frm}→{to}')
            seen.add(key)
        else:
            absent = [x for x in (frm, to) if x not in ids]
            errs.append(f'edge#{j} 端点不在元素表: {absent}')
        if ed.get('etri') not in EDGE_STYLE:
            errs.append(f'edge#{j} etri 未知: {ed.get("etri")}')
    # CP stops 引用必须指向存在的元素（通常为 LT 站）
    for e in els:
        if e.get('kind') == 'CP':
            attrs = e.get('attrs') or {}
            for s in attrs.get('stops') or []:
                if s not in ids:
                    errs.append(f'{e["id"]} 的 stops 引用了不存在的元素: {s}')
            if attrs.get('dir') not in (None, 'down', 'up'):
                errs.append(f'{e["id"]} 的传送方向 dir 只能是 down/up: {attrs.get("dir")}')
    # 决策点 DEC：挂靠设备 + Dest 映射(道号1..12, 不重复; to 必须存在, 允许同编号 LT↔RB 绑定对=同一物理工位)
    plex = lambda id_: id_[:-2] + ('LT' if id_.endswith('RB') else 'RB') if id_ and id_[-2:] in ('RB', 'LT') else None
    for e in els:
        if e.get('kind') != 'DEC':
            continue
        a = e.get('attrs') or {}
        if not (a.get('device') or '').strip():
            errs.append(f'{e["id"]} 决策点未填挂靠设备 device')
        seen = {}
        for r in a.get('dest_map') or []:
            d = r.get('dest')
            if d in seen:
                errs.append(f'{e["id"]} 决策点 Dest 道号重复: {d}')
            seen[d] = 1
            to = r.get('to')
            if to and to not in ids and plex(to) not in ids:
                errs.append(f'{e["id"]} 决策点 Dest 道号{d} 的后续滚床不存在: {to}')
    return errs


def _node(id_, kind, label, x, y, attrs=None):
    st = KIND_STYLE.get(kind, KIND_STYLE['RB'])
    attrs = attrs or {}
    # TT = 转台·单设备单中心：0°接收位为节点本体，各 attrs.angles 为可到转角位(扇臂)
    angles = list(attrs.get('angles') or ([attrs['turn']] if kind == 'TT' and 'turn' in attrs else [])) if kind == 'TT' else []
    return {'id': id_, 'label': label, 'type': kind, 'color': st['color'],
            'shape': st['shape'], 'w': st['w'], 'h': st['h'], 'x': x, 'y': y, 'angles': angles,
            'turn': attrs.get('turn') if kind == 'RB' else 0}


def expand(spec):
    """把 spec 展开成真图：所有元素单节点（LT 站不拆配位/低位，简化为一节点；
    pair 仅用于 tag/语义，不建额外节点）；CP stops→CP→各受控站 load 边（CP 作用=LT/LT 或 LT/WT 间传送）。"""
    els = spec.get('elements') or []
    nodes, edges = [], []
    def put_edge(a, b, etri):
        st = EDGE_STYLE.get(etri, EDGE_STYLE['roller'])
        # 与编辑器一致：虚线 = 同一多位置设备(SL/TC/HE)内部(.1↔.2) 或 跨线接口(ext)
        p = a.rsplit('.', 1); q = b.rsplit('.', 1)
        intra = bool(p[-1].isdigit()) and bool(q[-1].isdigit()) and a != b and p[0] == q[0]
        dashes = intra or etri == 'ext'
        edges.append({'from': a, 'to': b, 'etri': etri,
                      'color': st['color'], 'dashes': dashes, 'width': st['width']})
    for e in els:
        x = (e.get('pos') or {}).get('x', 0); y = (e.get('pos') or {}).get('y', 0)
        nodes.append(_node(e['id'], e['kind'], e.get('label') or short_id(e['id']), x, y, e.get('attrs')))
    for ed in (spec.get('edges') or []):
        put_edge(ed['from'], ed['to'], ed.get('etri', 'roller'))
    # CP：控制边 CP→stops[0](紧邻站, load)；站间传送链恒按索引序 stop[i]→stop[i+1]（dir 只决定物理排布/标签，不改边向）
    for e in els:
        if e['kind'] == 'CP':
            stops = (e.get('attrs') or {}).get('stops') or []
            if stops:
                put_edge(e['id'], stops[0], 'load')
                for i in range(len(stops)-1):
                    put_edge(stops[i], stops[i+1], 'load')
    return nodes, edges


def build_renderable(spec):
    nodes, edges = expand(spec)
    return {'line': spec.get('line', ''), 'nodes': nodes, 'edges': edges}


def connectivity(spec):
    """多源 DFS，报告不可达孤岛。每个「无入边」节点=入口源（含跨线 EXT 外部供入），
    从全部源出发覆盖，避免外部供入/多入口被误报孤岛。EXT=跨线接口、CP=机制设备，均不计入车流。"""
    els = spec.get('elements') or []
    if not els:
        return [], []
    adj = {}; has_in = set()
    for ed in (spec.get('edges') or []):
        adj.setdefault(ed['from'], []).append(ed['to']); has_in.add(ed['to'])
    # 与 expand() 一致：CP→stops[0] 控制 + 站间传送链(恒索引序)
    for e in els:
        if e.get('kind') == 'CP':
            stops = (e.get('attrs') or {}).get('stops') or []
            if stops:
                adj.setdefault(e['id'], []).append(stops[0]); has_in.add(stops[0])
                for i in range(len(stops)-1):
                    adj.setdefault(stops[i], []).append(stops[i+1]); has_in.add(stops[i+1])
    sources = [e['id'] for e in els if e['id'] not in has_in] or [els[0]['id']]
    seen = set()
    for s in sources:
        stack = [s]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            for v in adj.get(u, []):
                if v not in seen:
                    stack.append(v)
    kinds = {e['id']: e.get('kind') for e in els}
    unreach = [x for x in [e['id'] for e in els] if x not in seen and kinds.get(x) not in ('CP', 'EXT', 'ENT', 'DEC')]
    return seen, unreach


def merge_specs(specs):
    """跨线合并：多线按「partner 互指」接成一张总图。
    约定：EXT=出口(车由此出)、ENT=入口(车由此进)；
    每条 EXT 的 attrs.partner 指向对线 ENT(反之亦然)，严格互指成对。
    返回 (merged_spec, pairs, unpaired)。"""
    lines = [s.get('line', '') for s in specs]
    by_line = {s.get('line'): s for s in specs}
    elements = [e for s in specs for e in (s.get('elements') or [])]
    edges = [ed for s in specs for ed in (s.get('edges') or [])]
    # 接口表: id -> (line, kind, partner)
    IF = {}
    for s in specs:
        ln = s.get('line', '')
        for e in (s.get('elements') or []):
            if e.get('kind') in ('EXT', 'ENT'):
                IF[e['id']] = (ln, e['kind'], (e.get('attrs') or {}).get('partner') or '')
    # 严格互指配对：partner 指向在 IF 中、线不同、kind 互补，且反向指回
    pairs, unpaired = [], []
    used = set()
    for xid, (ln, k, p) in IF.items():
        if xid in used:
            continue
        peer = None
        for yid, (l2, k2, q) in IF.items():
            if yid in used or l2 == ln:
                continue
            if p == yid and q == xid and k != k2:      # 互指 + 角色互补
                peer = yid
                break
        if peer:
            # 方向：车从 EXT(出口) 流到 ENT(入口)
            e1, e2 = (xid, peer) if k == 'EXT' else (peer, xid)
            used.add(xid); used.add(peer)
            pairs.append((e1, e2))
        else:
            unpaired.append((xid, ln, k, p))
    cross = [{'from': a, 'to': b, 'etri': 'ext'} for a, b in pairs]
    merged = {'line': '+'.join(lines), 'comment': '跨线合并',
              'elements': elements, 'edges': edges + cross}
    return merged, pairs, unpaired, by_line


def emit_js(renderable, scene, path):
    data = {'line': renderable['line'], 'scene': scene, 'nodes': renderable['nodes'],
            'edges': renderable['edges']}
    js = 'window._PREVIEW_DATA = ' + json.dumps(data, ensure_ascii=False) + ';\n'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(js)


def merge_render(specs, pairs):
    """把配对接口(EXT/ENT 标记)折叠成真实设备间的过渡，并按「直线输送」整体平移各线。
    原则：车沿直线滚动，设备与它传给的下一个设备必须同一水平线(同 Y)；
    仅 TT 转角后的下一设备带其转角。配对接口不再作为设备渲染；真实设备间画跨线虚线边。
    未配对接口(如通未来线)保留为标记。平移以各线第一条成对接口为准(精确水平对齐)，
    其余成对接口如与本线差异(多输送道平行度不一致)，以残留偏移警告返回。"""
    by_line = {s.get('line'): s for s in specs}
    pos, kind, LOF = {}, {}, {}
    for s in specs:
        for e in (s.get('elements') or []):
            pos[e['id']] = ((e.get('pos') or {}).get('x', 0), (e.get('pos') or {}).get('y', 0))
            kind[e['id']] = e['kind']; LOF[e['id']] = s.get('line')
    IF = {i for i, k in kind.items() if k in ('EXT', 'ENT')}
    GAP = 8                                   # 出口设备右沿到入口设备右沿的净间距
    def adjacent(xid):
        for s in specs:
            for ed in (s.get('edges') or []):
                if ed['from'] == xid and ed['to'] not in IF: return ed['to']
                if ed['to'] == xid and ed['from'] not in IF: return ed['from']
        return None
    # a=出口EXT, b=入口ENT；ea=出口设备(本线出)，eb=入口设备(对线进)
    anchor = specs[0]['line']
    T = {anchor: (0, 0)}; placed = {anchor}; warnings = []
    all_lines = {LOF[a] for a, b in pairs} | {LOF[b] for a, b in pairs}
    while placed != all_lines and len(placed) < len(all_lines):
        grown = False
        for a, b in pairs:
            ea, eb = adjacent(a), adjacent(b)
            la, lb = LOF[a], LOF[b]                        # la=出口线, lb=入口线
            if not (ea and eb): continue
            if la in placed and lb not in placed:
                # 入口线放到出口设备右沿外、同一水平线 → 入口沿 X 正向接住出口
                T[lb] = (round(pos[ea][0] + 66 + GAP - pos[eb][0]),
                         round(pos[ea][1] - pos[eb][1]))
                placed.add(lb); grown = True
            elif lb in placed and la not in placed:
                # 出口线放到入口设备左沿外、同一水平线 → 出口在 X 反向供给入口
                T[la] = (round(pos[eb][0] - 66 - GAP - pos[ea][0]),
                         round(pos[eb][1] - pos[ea][1]))
                placed.add(la); grown = True
        if not grown: break                                # 无进展(环路/孤立)则终止
    # 对所有成对接口做残差检查：同一接口两侧设备平移后应同 Y 且 X 相邻
    for a, b in pairs:
        ea, eb = adjacent(a), adjacent(b)
        la, lb = LOF[a], LOF[b]
        if not (ea and eb): continue
        ax = pos[ea][0] + T[la][0]; ay = pos[ea][1] + T[la][1]
        bx = pos[eb][0] + T[lb][0]; byy = pos[eb][1] + T[lb][1]
        dy = round(byy - ay)
        if abs(dy) > 1:
            warnings.append(f'{ea}→{eb} 竖向错位 {dy}px（本线刚性平移无法兼顾多条输送道，需在编辑器统一各线道间距）')
    # 折叠: 去掉【已配对】接口 + 其进出边；保留未配对接口(边界)不动
    paired_if = {a for a, b in pairs} | {b for a, b in pairs}
    keep = {i for i in kind if i not in paired_if}
    els = []
    for s in specs:
        for e in (s.get('elements') or []):
            if e['id'] in keep:
                tx, ty = T[LOF[e['id']]]
                ep = dict(e.get('pos') or {})
                ep['x'] = (ep.get('x', 0) + tx); ep['y'] = (ep.get('y', 0) + ty)
                e2 = dict(e); e2['pos'] = ep; els.append(e2)
    edges = [dict(ed) for s in specs for ed in (s.get('edges') or [])
             if ed['from'] in keep and ed['to'] in keep]
    for a, b in pairs:                                # 跨线：出口真实设备 → 入口真实设备
        ea, eb = adjacent(a), adjacent(b)
        if ea and eb:
            edges.append({'from': ea, 'to': eb, 'etri': 'ext'})
    merged_spec = {'line': '+'.join(by_line), 'elements': els, 'edges': edges}
    return merged_spec, T, paired_if, warnings


def main(argv):
    try: sys.stdout.reconfigure(encoding='utf-8')               # 兼容 Windows GBK 控制台打 ✓/⚠/中文
    except Exception: pass
    home = os.path.dirname(os.path.abspath(__file__))          # 输出与本脚本同目录（preview.html 相对引用）
    args = list(argv)
    if '--merge' in args:
        args.remove('--merge')
        paths = args or [os.path.join(home, 'spec.json')]
        files = [p for p in paths if os.path.exists(p)]
        gone = [p for p in paths if not os.path.exists(p)]
        if not files:
            sys.exit(f'找不到规格文件: {paths}')
        specs = [load_spec(p) for p in files]
        print('· 单线校验：')
        for p, spec in zip(files, specs):
            errs = validate(spec)
            print(('  ✓ ' if not errs else '  ⚠ ') + f'{p} ({spec.get("line")}): ' +
                  ('校验通过' if not errs else f'{len(errs)} 个问题: ' + '; '.join(errs[:3])))
        merged, pairs, unpaired, by_line = merge_specs(specs)
        render_spec, T, folded, warnings = merge_render(specs, pairs)
        print(f'\n· 跨线配对 {len(pairs)} 对：')
        for a, b in pairs:
            print(f'  ✓ {a}  ->  {b}')
        print(f'· 各线相对 {specs[0]["line"]} 平移（接口贴合）：')
        for ln, (tx, ty) in T.items():
            print(f'  {ln}: +({tx}, {ty})   折叠配对接口 {len(folded)} 个')
        for xid, ln, k, p in unpaired:
            tag = '指向未建线' if p and line_id_of(p) not in [s.get('line') for s in specs] else '未配对'
            print(f'  ? 单边 [{ln}] {xid} ({k}) partner={p}　{tag}')
        if warnings:
            print('\n· 接口竖向错位(需在编辑器统一各线道间距)：')
            for wmsg in warnings: print('  ⚠ ' + wmsg)
        errs2 = validate(merged, check_prefix=False)
        renderable = build_renderable(render_spec)
        base = os.path.join(home, 'preview_data.js')
        emit_js(renderable, DEFAULT_SCENE, base.replace('preview_data', 'preview_data_merge'))
        if errs2:
            print(f'\n⚠ 总图校验 {len(errs2)} 个问题（仍已生成 preview_data_merge.js）：')
            for e in errs2: print('  -', e)
        else:
            print('\n✓ 总图校验通过，已生成 preview_data_merge.js')
        seen, unreach = connectivity(merged)
        flow = [n for n in renderable['nodes'] if n['type'] not in ('CP', 'EXT', 'ENT', 'DEC')]
        reach = len(flow) - len(unreach)
        print(f'  整产线连通性: 可达 {reach}/{len(flow)} 个车流节点'
              + (f'；不可达 {len(unreach)}: {unreach[:10]}' if unreach else '；全部可达'))
        if gone:
            print(f'  跳过缺失文件: {gone}')
        return
    path = argv[0] if argv else (os.path.join(home, 'spec.json')
                                 if os.path.exists(os.path.join(home, 'spec.json'))
                                 else os.path.join(home, 'samples', 'sample_spec.json'))
    if not os.path.exists(path):
        sys.exit(f'找不到规格文件: {path}（生成 spec.json 或传路径）')
    spec = load_spec(path)
    errs = validate(spec)
    renderable = build_renderable(spec)
    out = os.path.join(home, 'preview_data.js')
    emit_js(renderable, DEFAULT_SCENE, out)
    if errs:
        print(f'⚠ 校验发现 {len(errs)} 个问题（仍已生成 {out}，但请修复）：')
        for e in errs:
            print('  -', e)
    else:
        print(f'✓ 校验通过，已生成 {out}（line={spec.get("line")}）')
    seen, unreach = connectivity(spec)
    flow = [n for n in renderable['nodes'] if n['type'] not in ('CP', 'EXT', 'ENT', 'DEC')]   # 车流节点(不含 CP 机制/EXT/ENT 接口)
    reach = len(flow) - len(unreach)
    print(f'  连通性: 可达 {reach}/{len(flow)} 个车流节点（CP/EXT 接口不计）'
          + (f'；不可达 {len(unreach)}: {unreach[:8]}' if unreach else '；全部可达'))


if __name__ == '__main__':
    main(sys.argv[1:])