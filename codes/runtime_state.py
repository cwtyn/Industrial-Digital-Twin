# -*- coding: utf-8 -*-
"""
运行时车辆状态层 — 以滚床节点ID为键的车辆占用表 + 统一数据源适配器接口。

分层(与静态拓扑/坐标/图彻底分离):
- `Vehicle`: 单车的运行态数据(车身号 / 颜色 / 雪橇号 / 入站时间 / 所在节点)。可承载任意附加字段。
- `VehicleState`: "此刻每台滚床上停着什么车"的占用表(键=滚床节点ID)。
  纯状态层, 不碰图/布局。落盘为 `vehicle_state.json`(供可视化引擎叠加显示)。
- `VehicleSource`: 统一数据源协议。真实车间 websocket 订阅"每滚床数据"推送后, 只需实现
  本协议把报文解析成统一事件, 其余(状态表 / 快照 / 渲染)全部复用。接入真实源=换一个 Source。
- `consume()`: 事件消费主循环 —— place/clear/snapshot 更新占用, 定时落快照。

统一事件格式(source.events() 需逐条 yield 的元组):
    ("place",    node_id, vehicle_dict)   车到达某节点(占用该滚床)
    ("clear",    node_id)                 车离开该节点(释放滚床)
    ("skid",     node_id, sled_no)        更新该节点车辆的雪橇号(独立订阅点位 I1030_RBDataSkidNo)
    ("target",   node_id, target)         更新该节点车辆的目标地址(独立订阅点位 I1031_RBDataTarget)
    ("snapshot", {node_id: vehicle_dict}) 整表快照(用于一次复位/全量同步)
    ("print",    text)                    附加日志(如 websocket 心跳/连接状态)

说明: "扩展字段"/报文字段名 → vehicle_dict 的键名为贯通约定; 真实 websocket 适配器在解析
报文时把车间字段影射成本层键名(见 README 运行时一节)。
"""
import json, os, time, statistics
from abc import ABC, abstractmethod

# 运行时状态快照默认输出(可视化引擎 make_line_viz 读取叠加显示)
SNAPSHOT_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vehicle_state.json'))

# 边传送时间统计独立落盘(与 vehicle_state.json 分离, 避免与 serve 轮询的读-写竞争)
EDGE_STATS_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'edge_time_stats.json'))

# 传送时间采集口径(用户定夺):
EDGE_CAP_SEC = 60        # 整体节拍上限: δ>60s 视为堵塞/异常, 舍去并入 discarded
EDGE_MIN_SAMPLES = 5     # 达到该样本数取中位数锁定该边; 未达则持续跨班累计
TRAJ_TIMEOUT = 7200      # 在途轨迹条目超时(2h)剔除, 防内存涨


def _atomic_dump(obj, path):
    """原子写 JSON: 先写同目录临时文件再 os.replace。读者永远读到完整文件,
    不会撞上 open('w') 截断瞬间的 0/半截内容(现场事件密集时按条落盘, 该窗口几乎恒在)。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    # 页面/HTTP 轮询可能恰好以不含 FILE_SHARE_DELETE 的方式打开目标文件, Windows 上
    # os.replace 会 WinError 5。短暂重试几次即可; 仍失败则抛给调用方(下个事件会再写)。
    for i in range(4):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 3:
                raise
            time.sleep(0.05)


class Vehicle:
    """单车运行态。用简单属性 + 任意附加字段, 不引 ORM。"""
    __slots__ = ('body_no', 'color', 'sled_no', 'node_id', 'enter_ts', 'extra')

    def __init__(self, body_no='', color='', sled_no='', node_id=None, enter_ts=None, **extra):
        self.body_no = str(body_no)
        self.color = str(color)
        self.sled_no = str(sled_no)
        self.node_id = node_id
        self.enter_ts = enter_ts if enter_ts is not None else int(time.time())
        self.extra = dict(extra)

    def to_dict(self):
        d = {'body_no': self.body_no, 'color': self.color, 'sled_no': self.sled_no,
             'node_id': self.node_id, 'enter_ts': self.enter_ts}
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        extra = dict(d.get('extra') or {})
        node_id = d.pop('node_id', None)
        body_no = d.pop('body_no', '')
        color = d.pop('color', '')
        sled_no = d.pop('sled_no', '')
        enter_ts = d.pop('enter_ts', None)
        d.pop('extra', None)
        extra.update(d)                      # 其余顶层字段(pin_code/vehicle_type/platform…)并入 extra
        return cls(node_id=node_id, body_no=body_no, color=color,
                   sled_no=sled_no, enter_ts=enter_ts, **extra)

    def __repr__(self):
        return f"<V {self.body_no} {self.color} {self.sled_no} @{self.node_id}>"


class VehicleState:
    """滚床节点ID -> 车辆 的占用表。"""
    def __init__(self):
        self._occ = {}
        self.positions = {}          # 附加位信号(如 SL/TT/TC 到位状态): node -> value
        self.dec = {}                # 决策表DB: {dec_id: {'p_r_field': value}}; 低频, 供路径规划/推算
        self._traj = {}              # pin_code -> {node, ent_ts, occ_snapshot}: 每车最近可停站轨迹(供边耗时配对)
        self.edge_stats = {}         # (A,B) -> {samples, locked, value, discarded}: 边传送时间统计表
        self.no_edge_stat = False    # True=关闭边耗时自动采集(三期已弃, 改类别基准表; 页面/轨迹不受影响)

    def place(self, node_id, vehicle):
        vehicle = vehicle if isinstance(vehicle, Vehicle) else Vehicle.from_dict(dict(vehicle))
        vehicle.node_id = node_id
        # 同车重复上报(占用点心跳重订)只有 body 字段, 别把已取到的雪橇/目标冲掉
        old = self._occ.get(node_id)
        if old is not None and old.body_no == vehicle.body_no:
            if not vehicle.sled_no:
                vehicle.sled_no = old.sled_no
            merged = dict(old.extra)
            merged.update(vehicle.extra)
            vehicle.extra = merged
        if not self.no_edge_stat:
            self._record_hop(node_id, vehicle)   # 边耗采集(默认); --no-edge-stat 时跳过
        self._occ[node_id] = vehicle

    def clear(self, node_id):
        self._occ.pop(node_id, None)

    # ---- 边传送时间采集(口径见模块顶部 EDGE_* 常量) ----
    def _record_hop(self, node_id, vehicle):
        """车进可停站 node_id 时, 用它的上一可停站(轨迹)配对, 判定一个 A→B 样本是否有效并计入统计。"""
        pin = vehicle.extra.get('pin_code') or (vehicle.body_no or '')[:14]
        if not pin:
            return
        prev = self._traj.get(pin)
        if prev and prev['node'] != node_id:                       # 同车确实换站 → 候选迁移对
            delta = vehicle.enter_ts - prev['ent_ts']
            # 有效性: 进 A 那一刻 B 未被占用(a1); 且 δ 在节拍上限内
            if node_id not in prev['occ_snapshot'] and 0 <= delta <= EDGE_CAP_SEC:
                st = self.edge_stats.setdefault(
                    (prev['node'], node_id),
                    {'samples': [], 'locked': False, 'value': None, 'discarded': 0})
                if not st['locked']:
                    st['samples'].append(delta)
                    if len(st['samples']) >= EDGE_MIN_SAMPLES:
                        st['value'] = round(statistics.median(st['samples']), 1)
                        st['locked'] = True
                        st['samples'] = st['samples'][:]            # 保留(仍可持久化), 语义: 已锁
            elif delta > EDGE_CAP_SEC:
                st = self.edge_stats.setdefault(
                    (prev['node'], node_id),
                    {'samples': [], 'locked': False, 'value': None, 'discarded': 0})
                st['discarded'] += 1
        # 更新轨迹: 记录进当前站的占用快照(供下一次配对判"进 A 时 B 空")
        self._traj[pin] = {'node': node_id, 'ent_ts': vehicle.enter_ts,
                           'occ_snapshot': frozenset(self._occ.keys())}
        self._prune_traj()

    def _prune_traj(self):
        now = time.time()
        stale = [p for p, t in self._traj.items() if now - t['ent_ts'] > TRAJ_TIMEOUT]
        for p in stale:
            del self._traj[p]

    # ---- 边传送时间表: 落盘 / 读取 / 展示 ----
    def edge_stats_persist(self, path=None):
        """edge_stats -> {edges: {"A→B": …}} 落盘(与 vehicle_state.json 分离)。"""
        import json as _json
        path = path or EDGE_STATS_PATH
        try:
            data = {'edges': {f"{a}→{b}": dict(v) for (a, b), v in self.edge_stats.items()}}
            _atomic_dump(data, path)
        except OSError as e:
            print(f"  [warn] 写边耗时表失败 {path}: {e}")

    def load_edge_stats(self, path=None):
        path = path or EDGE_STATS_PATH
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    d = json.load(f)
            except (OSError, ValueError):
                return self
            for key, v in (d.get('edges') or {}).items():
                a, _, b = key.partition('→')
                self.edge_stats[(a, b)] = v
        return self

    def print_edges(self):
        rows = []
        for (a, b), st in sorted(self.edge_stats.items()):
            v = st.get('value')
            med = f"{v:>5}s" if v is not None else "   --"
            p10 = ""
            if st.get('samples'):
                p10 = f"  P10:{round(statistics.quantiles(st['samples'], n=10)[0], 1):>5}s" \
                      if len(st['samples']) >= 10 else ""
            rows.append(f"  {a} → {b}  中位{med}{p10}  "
                        f"{'锁定' if st.get('locked') else '采集中':4s}  "
                        f"样本{len(st.get('samples') or []):>2}  舍弃{st.get('discarded', 0)}")
        if not rows:
            print("  (尚无有效边传送时间样本)")
        else:
            print(f"  共 {len(self.edge_stats)} 条边耗时统计:")
            print("\n".join(rows))

    def reset_from(self, snapshot):
        self._occ.clear()
        for nid, v in (snapshot or {}).items():
            self.place(nid, v)

    def occupied(self, node_id): return node_id in self._occ
    def get(self, node_id): return self._occ.get(node_id)
    def count(self): return len(self._occ)
    def all(self): return dict(self._occ)

    def to_snapshot(self):
        return {nid: v.to_dict() for nid, v in self._occ.items()}

    def load_snapshot(self, path=None):
        path = path or SNAPSHOT_PATH
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            veh = d.get('vehicles') if isinstance(d, dict) and 'vehicles' in d else d
            self.reset_from(veh)
            if isinstance(d, dict) and 'positions' in d:
                pos = d.get('positions') or {}
                self.positions.clear(); self.positions.update(pos)
            if isinstance(d, dict) and 'dec' in d:
                dec = d.get('dec') or {}
                self.dec.clear(); self.dec.update(dec)
        return self


class VehicleSource(ABC):
    """统一数据源协议: 实现 events() 即可接入任意车间推送(websocket / 文件 / mock)。"""
    @abstractmethod
    def events(self):
        """生成器, 逐条 yield 统一事件元组(见模块 docstring)。数据源中断/正常结束则 return。"""


def consume(source, state=None, out_path=None, snapshot_every=1, log=print, live=False, on_event=None,
            no_edge_stat=False, snapshot_min_sec=1.0):
    """事件消费主循环: 把 source.events() 应用到 VehicleState, 周期性落快照。

    on_event : 可选回调(kind, node_id, payload), 在 place/clear/skid/target 分支
               状态变更后调用(place/clear 传 vehicle dict, skid/target 传新值)。
               用于把轨迹事件外送(如落 MySQL), 纯状态层不感知具体存储, 默认 None 无副作用。

    no_edge_stat : True 关闭边耗时自动采集与落盘(三期已弃, 改类别基准表; 页面/EMOS/轨迹照常)。

    返回最终 VehicleState。真实 websocket 接好 = 传一个解析普通报文的 VehicleSource 即可。
    """
    state = state or VehicleState()
    state.no_edge_stat = no_edge_stat
    if not no_edge_stat:
        state.load_edge_stats()          # 续累计边传送时间表(跨班/重启不丢失)
    out_path = out_path or SNAPSHOT_PATH
    step = 0
    last_snap = 0.0
    for ev in source.events():
        kind = ev[0]
        if kind == 'place':
            state.place(ev[1], ev[2])
            if live: log(f"  -> 车@{ev[1]}: {ev[2].get('body_no','')} {ev[2].get('color','')} {ev[2].get('sled_no','')}")
        elif kind == 'clear':
            state.clear(ev[1])
            if live: log(f"  <- 释放@{ev[1]}")
        elif kind == 'skid':
            v = state.get(ev[1])
            if v is not None:
                v.sled_no = ev[2]
                if live: log(f"  ~ 雪橇@{ev[1]} = {ev[2]}")
        elif kind == 'target':
            v = state.get(ev[1])
            if v is not None:
                v.extra['target'] = ev[2]
                if live: log(f"  ~ 目标@{ev[1]} = {ev[2]}")
        elif kind == 'snapshot':
            state.reset_from(ev[1])
            if live: log(f"  = 整表快照 {len(ev[1])} 车")
        elif kind == 'print':
            log(ev[1])          # 连接/断线/重订等运行日志: 与 --live 无关, 常开
        elif kind == 'dec':
            _p, _r, _fld, _val = ev[2]
            state.dec.setdefault(ev[1], {})[f'{_p}_{_r}_{_fld}'] = _val
            if live: log(f"  ◆ 决策表@{ev[1]} [{_p},{_r}].{_fld} = {_val}")
        else:                                # 附加位信号(非车辆数据): 位信号/TC位置 等, kind 任意非保留字
            state.positions[ev[1]] = ev[2]
            if live: log(f"  ◆ 位置@{ev[1]} = {ev[2]}")
        if on_event is not None and kind in ('place', 'clear', 'skid', 'target'):
            on_event(kind, ev[1], ev[2] if len(ev) > 2 else None)
        step += 1
        # 占用点心跳重订后事件可达 ~100/s: 快照/边耗表按条落盘会把磁盘打满,
        # 限成每秒最多一次(页面本来 2s 轮询, 1s 新鲜度足够)。
        if snapshot_every and step % snapshot_every == 0:
            now = time.time()
            if snapshot_min_sec <= 0 or last_snap == 0.0 or now - last_snap >= snapshot_min_sec:
                _write_snapshot(state, out_path)
                if not no_edge_stat:
                    state.edge_stats_persist()
                last_snap = now
    _write_snapshot(state, out_path)
    if not no_edge_stat:
        state.edge_stats_persist()
    return state


def _write_snapshot(state, path):
    """落盘快照: 车辆占用表 vehicles + 附加位信号 positions(供可视化上色 TC/SL/TT 位置)。

    结构 {'vehicles': {node: vehicle}, 'positions': {node: value}}。
    旧版纯车辆映射(顶层 node->vehicle)仍兼容读取(见 load_snapshot)。
    """
    try:
        data = {'vehicles': state.to_snapshot(), 'positions': dict(state.positions),
                'dec': dict(state.dec)}
        _atomic_dump(data, path)
    except OSError as e:
        print(f"  [warn] 写快照失败 {path}: {e}")


def print_table(state, G=None, label_of=None):
    """把当前占用表打印成人类可读清单。label_of: node_id->显示名 回调。"""
    occ = state.all()
    if not occ:
        print("  (当前无车停靠)")
        return
    rows = []
    for nid, v in occ.items():
        nm = label_of(nid) if label_of else nid
        rows.append(f"  {nm:18s}  {v.body_no or '-':10s} {v.color or '-':6s} 雪橇:{v.sled_no or '-'}")
    print(f"  {len(occ)} 台滚床有车停靠:")
    print("\n".join(sorted(rows)))


if __name__ == '__main__':
    print("import 模块, 通过 run_runtime.py 跑模拟; 真实 websocket 源接入时实现 VehicleSource。")