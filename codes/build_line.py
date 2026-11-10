# -*- coding: utf-8 -*-
"""
FJSP-T 分线建图 CLI — 读取 lines 各 config_XX.py, 用共享引擎建图。
用法:  python codes/build_line.py Line_1 [--csv] [--demo]
        python codes/build_line.py --all [--csv] [--demo]     (所有已配置线)
输出:  ./lines/<线>/<线>_nodes.csv / <线>_edges.csv
"""
import sys, os, re, json, importlib
import networkx as nx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import interface_registry as IR

LINES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lines'))
NAME_RE = re.compile(r'^config_(Line_\d+)$', re.I)

def available_lines():
    hits = []
    for fn in os.listdir(os.path.dirname(os.path.abspath(__file__))):
        if not fn.endswith('.py'): continue
        m = NAME_RE.match(fn[:-3])
        if m: hits.append(m.group(1))
    return sorted(hits)

def build_line(line):
    cfg = importlib.import_module('config_' + line)
    IR.apply_module(cfg)      # 注入 interfaces.json 的跨线接口(唯一数据源)
    d = {k.lower(): v for k, v in vars(cfg).items() if not k.startswith('_')}
    r = common.build(d)
    entries = [r['rb'](d) for d in cfg.ENTRIES]
    return cfg, r, entries

def connectivity(line, r, entries):
    G = r['G']
    print(f"[{line}] 图节点数: {G.number_of_nodes()}  边数: {len(G.edges)}")
    exits = [n for n in G.nodes if G.out_degree(n) == 0]
    print("  [入口] " + ", ".join(entries))
    print("  [出口 无出边(含外接占位)] " + ", ".join(sorted(exits)))
    reach = set()
    for e in entries:
        reach |= set(nx.descendants(G, e)) | {e}
    iso = [n for n in G.nodes if n not in reach]
    if iso:
        print(f"  [不可达区] {len(iso)} 节点: " + ", ".join(sorted(iso)))
    else:
        print("  [不可达区] 0 — 所有节点均可达")

def run_demos(line, cfg, r):
    G = r['G']; passed = 0
    for label, s, t in getattr(cfg, 'DEMOS', []):
        try:
            c = nx.dijkstra_path_length(G, s, t, weight='weight')
            passed += 1
            print(f"  ✓ {label}  → {c}s")
        except nx.NetworkXNoPath:
            print(f"  ✗ {label}  → 不可达!")
    print(f"  演示通过 {passed}/{len(getattr(cfg,'DEMOS',[]))}")

def write_csv(line, cfg, r):
    pf = line; G = r['G']
    ld = os.path.join(LINES_DIR, pf)
    os.makedirs(ld, exist_ok=True)
    nf = os.path.join(ld, f"{pf}_nodes.csv"); ef = os.path.join(ld, f"{pf}_edges.csv")
    import csv as _c
    with open(nf, 'w', encoding='utf-8-sig', newline='') as f:
        w = _c.writer(f); w.writerow(['node_id', 'type', 'line_code', 'name'])
        for n, d in G.nodes(data=True):
            w.writerow([n, d.get('type', ''), pf, ''])
    with open(ef, 'w', encoding='utf-8-sig', newline='') as f:
        w = _c.writer(f); w.writerow(['source', 'target', 'type', 'weight'])
        for a, b, d in G.edges(data=True):
            w.writerow([a, b, d['type'], d['weight']])
    print("  [csv] 已写:", nf, "/", ef)

def main(argv):
    demo = '--demo' in argv; cwise = '--csv' in argv
    verify = '--verify' in argv; export = '--export' in argv
    explicit = [a for a in argv if not a.startswith('--')]
    if export or verify:
        reg = IR.load()
        if export:
            IR.export_csv(reg)
        if verify:
            probs = IR.verify(reg)
            if probs:
                print("[接口校验] 发现问题:")
                for p in probs:
                    print("  -", p)
            else:
                print(f"[接口校验] 全部通过 (共 {len(reg['edges'])} 条边)")
        if '--all' not in argv and not explicit:
            return
    lines = explicit
    if '--all' in argv or not lines:
        lines = available_lines()
    for line in lines:
        cfg, r, entries = build_line(line)
        connectivity(line, r, entries)
        if cwise or demo:
            write_csv(line, cfg, r)
        if demo:
            run_demos(line, cfg, r)
        print()

if __name__ == '__main__':
    main(sys.argv[1:])