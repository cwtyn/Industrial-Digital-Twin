# -*- coding: utf-8 -*-
"""
跨线接口权威注册表 loader / 注入 / 校验。

单一数据源: ./interfaces.json —— 所有线间接口(含转台出、占位)在这里统一声明。
- `for_line(reg, prefix)`、`apply_module(cfg)` 按 owner 线把注册表投影成该线的
  EXT_EXITS / EXT_INBOUND / EXT_TURN, 注入配置模块, 使 common.py / layout_flow.py
  无需改动(仍读 cfg 下划线小写 dict)即可消费。
- `verify(reg)` 构建全部线, 交叉校验: owner 的 dev 真实存在、real 接口 ext 指向真实邻线节点
  而非占位/同线、无重复边。消除旧时"config 手写 EXT + line_meta.csv"双维护不一致的风险。
- build_line.py 入口调用 `apply_module`; `--verify` 触发 `verify`。

注意: `apply_module` 写的是模块**大写**属相(EXT_EXITS 等), 各脚本随后 `{k.lower()}` 转小写 dict。
"""
import json, os

from common import line_of

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
REG_PATH = os.path.join(_ROOT, 'interfaces.json')


def load(reg_path=REG_PATH):
    with open(reg_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def for_line(reg, prefix):
    """按 owner 线投影: 返回 {'ext_exits':[(ext,dev)...], 'ext_inbound':[(ext,dev)...], 'ext_turn':[(dev,ext)]}。"""
    ex_ex, ex_in, ex_turn = [], [], []
    for e in reg.get('edges', []):
        if e.get('owner') != prefix:
            continue
        dev, ext = e['dev'], e['ext']
        if e.get('kind') == 'tt':
            ex_turn.append((dev, ext))
        elif e.get('dir') == 'inbound':
            ex_in.append((ext, dev))
        else:  # dir == 'exit'
            ex_ex.append((ext, dev))
    return {'ext_exits': ex_ex, 'ext_inbound': ex_in, 'ext_turn': ex_turn}


def apply_module(cfg_module, reg=None):
    """注入注册表到配置模块(以 interfaces.json 为唯一数据源, 覆盖模块内可能残留的手写 EXT)。"""
    if reg is None:
        reg = load()
    prefix = getattr(cfg_module, 'PREFIX', None)
    if not prefix:
        return
    d = for_line(reg, prefix)
    cfg_module.EXT_EXITS = d['ext_exits']
    cfg_module.EXT_INBOUND = d['ext_inbound']
    cfg_module.EXT_TURN = d['ext_turn']


def export_csv(reg=None, out=None):
    """由注册表重新生成 line_meta.csv(消除手工双维护)。列与旧版一致。"""
    import build_line as BL
    if reg is None:
        reg = load()
    if out is None:
        out = os.path.join(_ROOT, 'line_meta.csv')
    bug = {}
    for pf in BL.available_lines():
        _c, r, _e = BL.build_line(pf)
        bug[pf] = r
    def node(owner, dev): return bug[owner]['rb'](dev)
    def prefix_of(nodeid): return line_of(nodeid)
    rows = []
    pair_n = {}
    def nid(fr, to):
        k = f"{fr}-{to}"; pair_n[k] = pair_n.get(k, 0) + 1
        return f"{fr}-{to}-{pair_n[k]}"
    for e in reg.get('edges', []):
        owner, dev, ext = e['owner'], e['dev'], e['ext']
        kind, dir_, status = e.get('kind', 'real'), e.get('dir', 'exit'), e.get('status', '')
        typ = 'tt' if kind == 'tt' else 'ext'
        if dir_ == 'inbound':
            _id, _f, _fn = nid(prefix_of(ext), owner), prefix_of(ext), ext
            _l, _n = owner, node(owner, dev)
        else:
            _id, _f, _fn = nid(prefix_of(ext), owner), owner, node(owner, dev)
            _l, _n = prefix_of(ext), ext
        rows.append((_id, _f, _fn, _l, _n, typ, e.get('note', ''), status))
    import csv as _c
    with open(out, 'w', encoding='utf-8-sig', newline='') as f:
        w = _c.writer(f)
        w.writerow(['接口ID', 'from_line', 'from_node', 'to_line', 'to_node', '类型', '说明', '状态'])
        for r in rows:
            w.writerow(r)
    print(f"  已生成 {out}  {len(rows)} 行(由 interfaces.json 导出, 勿手改)")


def verify(reg=None):
    """构建全部线并交叉校验注册表。返回问题列表(空=全通过); 顺带打印各线接口计数。"""
    import build_line as BL
    if reg is None:
        reg = load()
    lines = BL.available_lines()
    built = {}        # prefix -> r
    node_owner_type = {}   # node_id -> (owner_line, type)
    for pf in lines:
        _cfg, r, _e = BL.build_line(pf)          # build_line 内部已 apply_module
        built[pf] = r
        for n, d in r['G'].nodes(data=True):
            node_owner_type[n] = (pf, d.get('type', ''))
    for pf in lines:
        c = for_line(reg, pf)
        print(f"  [{pf}] 注入接口: exit={len(c['ext_exits'])} inbound={len(c['ext_inbound'])} turn={len(c['ext_turn'])}")
    problems = []
    seen = set()
    for e in reg.get('edges', []):
        owner, dev, ext = e['owner'], e['dev'], e['ext']
        dir_, kind = e.get('dir', 'exit'), e.get('kind', 'real')
        key = (owner, dir_, dev, ext)
        if key in seen:
            problems.append(f"重复边: {key}")
        seen.add(key)
        if owner not in built:
            problems.append(f"owner 线未配置或未构建: {owner}")
            continue
        r = built[owner]
        if not r['G'].has_node(r['rb'](dev)):
            problems.append(f"{owner} dev-{dev} 无辊床节点 {r['rb'](dev)}(检查 zones)")
        owner_line, _t = node_owner_type.get(ext, (None, None))
        if kind == 'real':
            if ext not in node_owner_type:
                problems.append(f"[{owner}] real 接口 ext 非任何已建节点: {ext}")
            elif owner_line == owner:
                problems.append(f"[{owner}] real 接口 ext 落在本线(非异线接口): {ext}")
            elif _t == 'EXT' or not _t:
                problems.append(f"[{owner}] real 接口 ext 是占位/无类型, 应指向真实邻线节点: {ext}")
        else:
            if ext in node_owner_type and owner_line != owner:
                pass  # 占位/外接但该节点已被他线建立为真实节点 -> 视为真实接口, 不再重复校验
    return problems