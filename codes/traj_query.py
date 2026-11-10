# -*- coding: utf-8 -*-
"""
命令行车辆轨迹回溯 — 查某台车从入库起经过的滚床节点链 + 各站停留时长。

数据源: MySQL `conveyor_traj.vehicle_events`(由 run_runtime --traj 落库)。

用法:
    python codes/traj_query.py <PIN|雪橇号|车身号前缀>        # 模糊匹配定位一台车
    python codes/traj_query.py --pin 78202664513471           # 按 PIN 精确
    python codes/traj_query.py --file cars.txt                # 每行一个查询
    python codes/traj_query.py --recent 10                    # 最近 10 条落库事件(看有没有数据)

回溯 键 = pin_code(body 前14位唯一识别车). 路径由 place 事件按 ts 重建, clear 闭合停留;
当前未 clear 的站标「在场」, 停留 = now − enter.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from traj_store import load_db_config, TrajWriter  # noqa: E402
from common import dev_of  # noqa: E402


# ---------- 可读标签(轻量复刻 make_line_viz.label, 不触发整图重算) ----------
def _short_node(n):
    """Line_4_1C275RB -> 1C275RB; 附类型后缀不含在节点名里的补上."""
    return dev_of(n)


# 全局节点类型(由 build_line 汇总一次; 失败则退回纯短名)
_NODE_TYPE = None


def _node_type(n):
    global _NODE_TYPE
    if _NODE_TYPE is None:
        _NODE_TYPE = {}
        try:
            import build_line as BL
            for pf in BL.available_lines():
                _, raw, _ = BL.build_line(pf)
                for nd, d in raw['G'].nodes(data=True):
                    _NODE_TYPE[nd] = d.get('type', '')
        except Exception:
            pass
    return _NODE_TYPE.get(n, '')


def label(n):
    if not n:
        return n
    s = _short_node(n)
    t = _node_type(n)
    if t == 'TT':
        s += '·转'
    elif t == 'SL':
        s += '·剪'
    elif t == 'LT':
        s += '·升'
    elif t == 'HE':
        s += '·提'
    return s


# ---------- 数据库 ----------
def _connect():
    cfg = load_db_config()
    w = TrajWriter(cfg)
    c = w.connect()
    return cfg, c


# ---------- 查询 ----------
def _resolve_car(cur, query):
    """把 PIN/雪橇/车身前缀解析成单一 pin_code. 返回 (查到的事件行 dict) 或 None."""
    q = str(query).strip()
    if not q:
        return None
    like = f"%{q}%"
    cur.execute(
        "SELECT pin_code, body_no, sled_no, color, vehicle_type, platform FROM vehicle_events "
        "WHERE pin_code LIKE %s OR body_no LIKE %s OR sled_no LIKE %s OR node_id=%s "
        "ORDER BY id DESC LIMIT 20", (like, like, like, q))
    rows = cur.fetchall()
    if not rows:
        return None
    # 挑 pin_code 非空的唯一车的最近一行作为该车档案
    seen = {}
    for r in rows:
        if r[0]:
            seen.setdefault(r[0], r)
    if len(seen) > 1:
        print(f"  命中 {len(seen)} 台车, 取最近活动的一台:")
        for pin, r in list(seen.items())[:5]:
            print(f"    PIN {pin}  雪橇 {r[2] or '-'}  车型 {r[4] or '-'}  颜色 {r[3] or '-'}")
        pin = max(seen, key=lambda p: p)   # 稳定挑选
        print(f"  → 选用 PIN {pin}")
        return seen[pin]
    return next(iter(seen.values()))


def _fetch_path(cur, pin):
    cur.execute(
        "SELECT node_id, ev, ts_epoch FROM vehicle_events WHERE pin_code=%s "
        "ORDER BY ts_epoch ASC, id ASC", (pin,))
    return cur.fetchall()


def _reconstruct(events, now):
    """events:(node_id, ev, ts). 重建 [(node, enter_ts, leave_ts)], leave=None 表示在场."""
    segs = []
    cur_node, cur_enter = None, None
    for node, ev, ts in events:
        if ev == 'place':
            if cur_node is not None and node != cur_node:   # 换站: 闭合上一段
                segs.append((cur_node, cur_enter, ts))
            cur_node, cur_enter = node, ts
        elif ev == 'clear' and node == cur_node:            # 离开当前站: 闭合
            segs.append((cur_node, cur_enter, ts))
            cur_node, cur_enter = None, None
    if cur_node is not None:
        segs.append((cur_node, cur_enter, None))            # 在场(未 clear)
    return segs


def _fmt_ts(epoch):
    try:
        return time.strftime('%H:%M:%S', time.localtime(epoch))
    except Exception:
        return str(epoch)


# ---------- 输出 ----------
def print_car(rows, now=None):
    now = time.time() if now is None else now
    print("\n" + "=" * 66)
    print(f" 车: PIN {rows[0][0] or '-'}  雪橇 {rows[0][2] or '-'}  车型 {rows[0][4] or '-'}  "
          f"颜色 {rows[0][3] or '-'}  平台 {rows[0][5] or '-'}")
    print(f" 车身号: {rows[0][1] or '-'}")
    print("=" * 66)


def print_path(pin, cur, now=None):
    now = time.time() if now is None else now
    events = _fetch_path(cur, pin)
    if not events:
        print(f"  (无轨迹: 数据库未落该车事件)")
        return
    segs = _reconstruct(events, now)
    chain = " → ".join(label(s[0]) for s in segs)
    print(f"\n PIN {pin}  共 {len(segs)} 站:")
    print(f" 路径: {chain}")
    print("-" * 66)
    for i, (node, enter, leave) in enumerate(segs):
        dwell = round((leave if leave is not None else now) - enter)
        status = "·在场" if leave is None else ""
        start = _fmt_ts(enter)
        end = _fmt_ts(leave) if leave else "―(now)"
        mark = "  <<入口" if i == 0 else ("  <<当前位" if leave is None else "")
        print(f"  {start}  [{mark.strip() or '    '}]  {label(node):20s} "
              f"入{start}  出{end}  停留{dwell:>4}s{status}")
    # 全链节点ID序列(带前缀, 供机器/后续图高亮用)
    print("-" * 66)
    print(" 节点ID序列:", " → ".join(s[0] for s in segs))


def main():
    ap = argparse.ArgumentParser(description='车辆轨迹回溯(MySQL conveyor_traj.vehicle_events)')
    ap.add_argument('query', nargs='?', help='PIN / 雪橇号 / 车身号前缀')
    ap.add_argument('--pin', help='按 PIN 精确查询')
    ap.add_argument('--file', help='每行一个查询, 批量输出')
    ap.add_argument('--recent', type=int, default=0, help='打印最近 N 条落库事件(验证有数据)')
    a = ap.parse_args()

    cfg, c = _connect()
    cur = c.cursor()
    if a.recent:
        cur.execute("SELECT id, node_id, ev, ts_epoch, pin_code FROM vehicle_events "
                    "ORDER BY id DESC LIMIT %s", (a.recent,))
        rows = cur.fetchall()
        print(f"最近 {len(rows)} 条落库事件 (DB {cfg['database']}):")
        for r in rows:
            print(f"  #{r[0]} {_fmt_ts(r[3])}  {r[1]:22s}  {r[2]:5s}  {r[4] or ''}")
        return

    queries = []
    if a.file:
        with open(a.file, 'r', encoding='utf-8') as f:
            queries = [ln.strip() for ln in f if ln.strip()]
    elif a.pin:
        queries = [a.pin]
    elif a.query:
        queries = [a.query]
    if not queries:
        ap.print_help()
        return

    for q in queries:
        hit = _resolve_car(cur, q)
        if not hit:
            print(f"  未找到匹配 {q!r} 的轨迹")
            continue
        print_car([hit])
        print_path(hit[0], cur)


if __name__ == '__main__':
    main()