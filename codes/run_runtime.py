# -*- coding: utf-8 -*-
"""
运行时车辆状态 运行入口 — 端到端跑通"每滚床车位数据"链路。

用法:
  python codes/run_runtime.py [--steps 40] [--cars 8] [--out ..\\vehicle_state.json] [--noregen] [--live]

流程:
  MockVehicleSource(模拟"每滚床数据"推送) → VehicleState 占用表(键=滚床节点ID)
    → 落盘 vehicle_state.json → 打印停靠清单 → 重生成 conveyor_visual.html 显示车辆。

接真实车间 websocket 后, 只需写一个 runtime_state.VehicleSource(把普通报文解析成
place/clear/snapshot 事件)替换这里的 MockVehicleSource, 后面一律复用。
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime_state import (VehicleState, consume, print_table,
                           SNAPSHOT_PATH, EDGE_STATS_PATH)
from mock_vehicles import MockVehicleSource

# 车间 ws 数据源: 真机实时 / 离线回放(重放录制报文走同一条翻译路径)
from workshop_source import (WorkshopSource, ReplaySource, build_subscriptions,
                             load_points, make_node_resolver, make_extra_resolver,
                             build_dec_subscriptions)


# 非标准 tag(现场 PLC 程序笔误): 段号后多插一段, 如 Line_1 355/390 的 14AS/15AS。
# 实测(2026-10-20): `.L3L1_1E_14AS_1E355SL_1E355RB.IL...` 与 `.L3L1_1E_15AS_1E390SL_1E390RB.IL...` 均回数据。
# key=节点ID, value=插入段。正常点位不在表内。
SPECIAL_TAG_MID = {
    'Line_1_1E355RB': '14AS',
    'Line_1_1E390RB': '15AS',
}


def tag_mid_of(node):
    """节点的非标准中间段(14AS/15AS...); 正常点位返回 None。"""
    return SPECIAL_TAG_MID.get(node)


def subscription_roster():
    """权威订阅清册: (node_ids, dtype_of)。node->功能件类型, 决定 X1 段。

    - 包含所有**可停靠 RB 位置**: 纯 RB + 被标成 TT/SL/HE 的 RB 节点; 排除过渡辊床(can_park=False)。
    - 含 **TC 承载体**(横移车, 如 Line_1 402TC / Line_2 485TC/530TC): 取节点 type 照常入册,
      dtype 记为 'TC', 使 X1 用 TC 设备号、X2=该位置 RB。
    原则: X1 取设备体(RB/LT/TT/SL/TC/HE), X2 恒为该位置 RB。
    """
    import build_line as BL
    reg = []            # (node, dtype)
    lt_nodes = {}       # node -> 'LT'  (LT 工位: RB 依附在升降台上, X1=LT)
    carriers = {}       # node -> 'TC'  (横移车承载体)
    for pf in BL.available_lines():
        cfg, raw, _ = BL.build_line(pf)
        G = raw['G']; rb = raw['rb']
        for n, d in G.nodes(data=True):
            if d.get('type') in ('RB', 'TT', 'SL', 'HE') and d.get('can_park', True):
                reg.append((n, d.get('type')))
        # LT 工位: 升降台(=RB组合), X1=X2=LT (现场取 LT_LT, 弃 LT_RB)
        for dev in getattr(cfg, 'LT_COMBOS', []) or []:
            n = rb(dev)
            if n in G:
                lt_nodes[n] = 'LT'
        # 纯LT存位(无RB, X1=X2=LT): 停车节点 = ltp(dev)
        for dev in getattr(cfg, 'PURE_LT', []) or []:
            n = raw['ltp'](dev)
            if n in G:
                lt_nodes[n] = 'LT'
        # TC 承载体: X1 走设备号(dev), X2=自身 RB
        tc_devs = []
        if getattr(cfg, 'TC', None):
            tc_devs.append(cfg.TC['dev'])
        for e in getattr(cfg, 'TC_DISPLAY', []) or []:
            tc_devs.append(e['dev'])
        for dev in tc_devs:
            n = rb(dev)
            if n in G and n not in carriers:
                carriers[n] = 'TC'
    dtype_of = dict(reg)
    dtype_of.update(lt_nodes)       # LT 覆盖(纯 RB 名称节点)
    dtype_of.update(carriers)       # TC 承载体优先级最高
    return sorted(dtype_of), dtype_of


def dec_subscription_points():
    """从各线 config 的 DEC_POINTS 展开决策表订阅点(单一源=config_Line_x.DEC_POINTS)。
    当前仅 Line_4 有 DEC_POINTS(Line_3 无分支决策点), Line_1/Line_2 待补；返回该线缺省已含所有线的点表。"""
    import build_line as BL
    out = []
    for pf in BL.available_lines():
        cfg, _raw, _ = BL.build_line(pf)
        decs = getattr(cfg, 'DEC_POINTS', None) or []
        if decs:
            out.extend(build_dec_subscriptions({pf: decs}))
    return out


def _resolve_points(args):
    """取订阅点: 优先 --points(权威设备config: 目录=每条线一个文件 / 单文件=flat points),
    否则按拓扑默认约定生成占位点。两者都并入决策表 DEC 订阅点(kind='dec')。"""
    if args.points:
        pts = load_points(args.points)
    else:
        nodes, dtype_of = subscription_roster()
        pts = build_subscriptions(nodes, dtype_of=dtype_of.get, mid_of=tag_mid_of)
        print(f"[info] 无 --points, 按默认约定为 {len(pts)} 个滚床生成订阅点 "
              f"(真机请用 --points 覆盖真实设备 config)")
    decs = dec_subscription_points()
    if decs:
        pts = pts + decs
        print(f"[info] 并入决策表 DEC 订阅 {len(decs)} 条")
    return pts


def make_source(args):
    """按 --source 选择数据源: mock(模拟) / workshop(EMOS实时) / replay(离线回放)。"""
    if args.source == 'mock':
        return MockVehicleSource(n_cars=args.cars, steps=args.steps)

    if args.source == 'replay':
        if not args.replay:
            raise SystemExit("--source replay 需要 --replay <jsonl文件>")
        # 回放也用与实时一致的点表做 tag->节点 映射(子串匹配, 兼容服务器规范化前缀)
        pts = _resolve_points(args)
        return ReplaySource(path=args.replay,
                            node_of_tag=make_node_resolver(pts),
                            extra_resolver=make_extra_resolver(pts))

    # workshop: 订阅点(权威或占位生成)
    points = _resolve_points(args)
    return WorkshopSource(url=args.ws_url, points=points,
                          heartbeat_ms=args.heartbeat, reconnect_ms=args.reconnect,
                          max_reconnect=args.maxreconnect, poll_ms=args.poll_ms)


HERE = os.path.dirname(os.path.abspath(__file__))


def regen_viz():
    """重生成 conveyor_visual.html(内嵌当前 vehicle_state.json 快照)。"""
    import subprocess
    print("=== 重生成可视化(叠加车辆信息) ===", flush=True)
    subprocess.run([sys.executable, os.path.join(HERE, 'make_line_viz.py')], check=True)


def serve(port):
    """在项目根目录起本地 HTTP 服务: file:// 下浏览器禁止 fetch, 实时刷新需 HTTP 打开。"""
    import http.server, socketserver, threading
    root = os.path.dirname(HERE)
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)
        def log_message(self, *a):
            pass
    httpd = socketserver.ThreadingTCPServer(('127.0.0.1', port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[info] 实时页面: http://127.0.0.1:{port}/conveyor_visual.html")
    print(f"[info] 页面每 2s 轮询 vehicle_state.json, 无需手动刷新(file:// 打开不会刷新)")


def main(argv=None):
    # 控制台/重定向到文件时统一 UTF-8, 避免中文日志乱码(仅影响显示)
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--steps', type=int, default=40, help='游走步数(mock)')
    ap.add_argument('--cars', type=int, default=8, help='车辆数(mock)')
    ap.add_argument('--out', default=SNAPSHOT_PATH)
    ap.add_argument('--noregen', action='store_true', help='不重生成可视化 HTML')
    ap.add_argument('--live', action='store_true', help='逐步打印事件')
    ap.add_argument('--source', choices=['mock', 'workshop', 'replay'], default='mock',
                    help='数据源: mock 模拟 / workshop EMOS实时 / replay 离线回放')
    ap.add_argument('--ws-url', default=os.environ.get('EMOS_WS_URL', '127.0.0.1/ws/emosweb'),
                    help='EMOS ws 地址(可用环境变量 EMOS_WS_URL)')
    ap.add_argument('--points', default=None, help='订阅点 JSON(权威设备config)')
    ap.add_argument('--replay', default=None, help='回放 JSONL 报文文件(离线自测)')
    ap.add_argument('--heartbeat', type=int, default=2000, help='心跳间隔 ms')
    ap.add_argument('--poll-ms', type=int, default=1000,
                    help='占用点周期重订间隔 ms(0=关闭): EMOS 变化推送下, 停留统计需要重复上报')
    ap.add_argument('--reconnect', type=int, default=5000, help='重连间隔 ms')
    ap.add_argument('--maxreconnect', type=int, default=-1, help='最大重连次数(-1=无限)')
    ap.add_argument('--serve', type=int, default=0, metavar='PORT',
                    help='起本地 HTTP 服务(如 8765), 浏览器打开该地址即可实时刷新')
    ap.add_argument('--dump-edges', action='store_true',
                    help='只打印边传送时间表(读 edge_time_stats.json)并退出')
    ap.add_argument('--traj', action='store_true',
                    help='启用车辆轨迹落 MySQL(conveyor_traj.vehicle_events)')
    ap.add_argument('--no-edge-stat', action='store_true',
                    help='关闭边耗时自动采集与落盘(三期已弃, 改类别基准表; 页面/轨迹不受影响)')
    ap.add_argument('--traj-init', action='store_true',
                    help='只建轨迹库表后退出(不启动数据源)')
    a = ap.parse_args(argv)

    if a.dump_edges:                       # 边耗时表独立查询, 不启动源/消费
        st = VehicleState(); s2 = st.load_edge_stats()
        print(f"=== 边传送时间表 ({EDGE_STATS_PATH}) ===")
        s2.print_edges()
        return

    # 轨迹落 MySQL: 可选(--traj 启用). --traj-init 只建库表并退出.
    writer = None
    if a.traj or a.traj_init:
        from traj_store import TrajWriter
        writer = TrajWriter()
        writer.ensure_schema()             # 幂等建库建表(失败仅 warn, 不卡启动)
        if a.traj_init:
            print("[info] 轨迹库表已就绪 (conveyor_traj.vehicle_events)")
            return

    src = make_source(a)
    print(f"=== 运行时状态(数据源: {type(src).__name__}) ===", flush=True)

    # 热启动(仅 EMOS 实时): 先载上一次快照(车辆/位置/DEC), 页面立刻有数据;
    # 随后 EMOS 初始 advise 会逐点用当前值覆盖, 已离场车辆由空值回推 clear。
    state = VehicleState()
    if a.source == 'workshop':
        try:
            state.load_snapshot(a.out)
            if state.count() or state.dec:
                print(f"[info] 热启动: 载入上次快照 车辆 {state.count()} 台 / 位置 {len(state.positions)} / "
                      f"DEC {len(state.dec)} 点(等待 EMOS 回填覆盖)", flush=True)
        except Exception as e:
            print(f"[warn] 载入上次快照失败, 从空状态开始: {e}", flush=True)

    # 实时源 consume() 不返回, 页面必须在开跑前先出
    if not a.noregen:
        regen_viz()
    if a.serve:
        serve(a.serve)

    if writer:
        writer.start()
    try:
        state = consume(src, state, out_path=a.out, live=a.live,
                        on_event=(writer.record if writer else None),
                        no_edge_stat=a.no_edge_stat)
    finally:
        if writer:
            writer.close()                 # 停写线程且 flush 缓冲
    print_table(state)

    if not a.noregen:
        regen_viz()


if __name__ == '__main__':
    main()