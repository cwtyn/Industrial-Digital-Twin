# -*- coding: utf-8 -*-
"""
模拟车辆数据源 — 用已建 Line_1/Line_2/Line_3 真实图, 让几辆车沿辊床链随机游走,
每步产生 place/clear 事件, 模拟"每滚床车位数据"推送。

用途: 让运行时状态层不依赖真实车间 websocket 就能端到端跑通。
真实 websocket 订阅到手后, 只需实现 runtime_state.VehicleSource(把普通报文翻译成
统一事件: place/clear/snapshot), 其余(状态表/快照/可视化)全部复用, mock 即可废弃。
"""
import random
import build_line as BL
from runtime_state import VehicleSource


class MockVehicleSource(VehicleSource):
    def __init__(self, n_cars=8, steps=40, seed=7, lines=None):
        self.n_cars = n_cars
        self.steps = steps
        self._rng = random.Random(seed)
        # 汇总真实线图, 从中抽取可停靠的辊床(RB, 非外接占位/功能件)用于车辆游走
        graphs = {}
        for pf in (lines or BL.available_lines()):
            _c, r, _e = BL.build_line(pf)
            graphs[pf] = r['G']
        G_all = graphs[list(graphs)[0]]
        for pf in graphs:
            if pf != list(graphs)[0]:
                G_all = BL.nx.compose(G_all, graphs[pf])
        self.G = G_all
        rbs = [n for n, d in G_all.nodes(data=True)
               if d.get('type') == 'RB' and d.get('can_park', True)]
        self._rbs = rbs
        # 避免车一开始全挤一格: 隔段散开
        self._pos = {c: rbs[(c * 5) % len(rbs) if len(rbs) else c] for c in range(n_cars)}
        self._occupied = set(self._pos.values())          # 已被车占的滚床(避免撞位)
        self._palette = ['白', '银', '灰', '黑', '蓝', '红']
        self._body = {c: f"BD-{1000 + c * 13}" for c in range(n_cars)}
        self._sled = {c: f"SL-{200 + c * 7}" for c in range(n_cars)}

    def _outgoing(self, node):
        """可选移动目标: 沿辊床/横移/层间边前进, 排除占位/被别车占用的节点。"""
        cand = []
        for _n, b, d in self.G.edges(node, data=True):
            et = d.get('type')
            if et in ('roller', 'cross', 'load', 'lift') and b in self._rbs and b not in self._occupied:
                cand.append(b)
        return cand

    def _veh(self, c):
        return {'body_no': self._body[c], 'color': self._palette[c % len(self._palette)],
                'sled_no': self._sled[c]}

    def events(self):
        """逐条 yield 统一事件。先发整表快照(所有车初始占位), 每步各车尝试前移=place/clear。"""
        yield ('snapshot', {n: self._veh(c) for c, n in self._pos.items()})
        for step in range(self.steps):
            for c in self._cars:
                cur = self._pos[c]
                nxts = self._outgoing(cur)
                if not nxts:
                    continue
                nxt = nxts[self._rng.randrange(len(nxts))]
                yield ('clear', cur)                       # 离开原站(该位已空)
                self._occupied.discard(cur)
                self._pos[c] = nxt
                self._occupied.add(nxt)
                yield ('place', nxt, self._veh(c))        # 到达新车位
            yield ('print', f"[step {step}] 车辆在途 …")

    @property
    def _cars(self):
        return range(self.n_cars)


if __name__ == '__main__':
    print("供 run_runtime.py 调用。")