# -*- coding: utf-8 -*-
"""
车间 WebSocket 数据源 — 对接 EMOS ws, 订阅"每滚床车身号", 翻译成统一事件。

协议来源: 分析自 existing 系统 polling_monitor_V2 后端
  (websocket.js / config.js / config/deviceConfig.json, 链接 ws://<EMOS_HOST>/ws/emosweb):

  连接      ws://<host>/ws/emosweb                 (车间 EMOS 服务器)
  订阅      每条设备发 {"id":"","type":"advise","plc":"<L3Lx>","tag":"<点位tag>"}
            一个滚床一条 tag; 订阅后服务器会立即回推该 tag 当前值(=初始占用快照)。
  点位tag   .L3Lx_<段>_<滚床>_<滚床>.IL[O].SD.M1003_BodyID, 如 .L3L1_1D_1D325RB_1D325RB.ILO.SD.M1003_BodyID
            (前导点, L3Lx 之间无点; 格式以能跑通的 skid_count 工作项目为准)
           同路径其它点位: .I1030_RBDataSkidNo(雪橇号) / .I1031_RBDataTarget(车身目标地址)。
  心跳      每 <=N ms 发 {"type":"info","info":"alive"}
  回推      {"tag":"...M1003_BodyID","value":"<26位车身号>","quality":..,"source":..,"ts":..}
  车身号    26位: PIN[0:14] 车型[14:19] 颜色[19:23] 平台[23:26]; 空值 = '---'/'...'/全横杠或空白
  重连      断线后每 R ms 重试, 重连成功后重订阅

本模块实现 runtime_state.VehicleSource, 把回推报文翻译成 place/clear 事件, 其余
(占用表 / 落盘 / 可视化)全部复用 —— 与 MockVehicleSource 同层换源即可。
真机未接通时, 用 ReplaySource 回放录制的报文走同一条翻译路径, 离线路跑通。
"""
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime_state import VehicleSource, SNAPSHOT_PATH
from common import line_of as _split_line_of

try:
    import websocket
    _HAVE_WS = True
except Exception:
    _HAVE_WS = False


# ---------------------------------------------------------------------------
# 30位车身号　解析 / 空值判断
# ---------------------------------------------------------------------------
class VehicleId:
    """把工位回推的 30 位完整编号拆成字段, 并判断是否为空(无车)。

    分割(用户给定, 例 '782026843013792N54Y2T2TP_A000-'):
        PIN[0:14] 车型[14:19] 颜色[19:23] 平台[23:26] Code[26:29]  (末尾字符忽略)
        '78202684301379' '2N54Y' '2T2T' 'P_A' '000'
    """
    LEN = 29

    @staticmethod
    def is_empty(value):
        if not value or not isinstance(value, str):
            return True
        t = value.strip().replace('\n', '')
        if not t:
            return True
        if t in ('---', '...'):
            return True
        if not any(c.isalnum() for c in t):
            return True          # 全横杠/点/空白
        return False

    @staticmethod
    def parse(value):
        t = (value or '').strip().replace('\n', '')
        t = t.ljust(VehicleId.LEN)
        return {
            'pin_code':     t[0:14].strip(),
            'vehicle_type': t[14:19].strip(),
            'color_code':   t[19:23].strip(),
            'platform':     t[23:26].strip(),
            'code':         t[26:29].strip(),
        }

    @staticmethod
    def to_vehicle(value, extra=None):
        """30位编号 -> Vehicle 能吃的 dict(body_no=完整编号)。"""
        f = VehicleId.parse(value)
        v = {
            'body_no': (value or '').strip().replace('\n', ''),
            'color': f['color_code'],          # 源系统直接展示 4 位颜色码(如 2T2T)
            'sled_no': '',
            'pin_code': f['pin_code'],
            'vehicle_type': f['vehicle_type'],
            'platform': f['platform'],
            'code': f['code'],
        }
        if extra:
            v.update(extra)
        return v


# ---------------------------------------------------------------------------
# 节点ID <-> EMOS tag　(命名约定可配置)
# ---------------------------------------------------------------------------
PLC_SYS_PREFIX = 'L3'     # 车间 PLC 代号前缀: L3 + 短线代号(如 L1) => 订阅报文 plc 字段 = L3L1
# 泛化线名 -> PLC 短线代号。Line_n => Ln, Exit_n => Xn, 保持 tag 语法 .L3L1_<段>_...
_LINE_SHORT_RE = re.compile(r'^Line_(\d+)$')
_EXIT_SHORT_RE = re.compile(r'^Exit_(\d+)$')
_LINE_PLC_RE = re.compile(r'^L(\d+)$')
_EXIT_PLC_RE = re.compile(r'^X(\d+)$')
# EMOS tag 真实格式: 前导点 + .L3L1_段_滚床_滚床.IL[O].SD.M1003_BodyID
#   .L3L1_1A_1A010RB_1A010RB.IL.SD.M1003_BodyID   (1A 段为 .IL.)
#   .L3L1_1B_14AS_1B020RB_1B020RB.ILO.SD.M1003_BodyID (1B 之后多为 .ILO.)
# L3 与短线代号之间没有点; 前导点 TAG_NS='.' 可配, 若真机确认不带点改成 '' 即可。
TAG_NS = '.'
# 台体分支: .IL.(单点台) / .ILO.(多点台). 2026-10-20 实测 1A020RB 纯RB 必须用 .IL.,
# 用了 .ILO. 服务器回 WSS.Error(订阅到不存在的 tag, 并非认证). 默认取 .IL., 真机按 deviceConfig 逐台覆盖.
TAG_BRANCH = 'IL'


def _segment_of(rb_index):
    """滚床编号的段号。1D325RB -> 1D; 1A010RB -> 1A(取首段字母+数字)。"""
    m = re.match(r'(\d*[A-Z]+)\d+', rb_index or '')
    return m.group(1) or '1D'


def _tag_path(plc, rb_index, device=None, branch=None, device_area=None, point='M1003_BodyID', mid=None):
    """生成 .L3Lx_<段>[_<mid>]_<X1>_<X2>.IL.SD.<point> 的公共路径(车身号/雪橇号复用)。

    两段规则(用户给定):
      X1 = 实际停靠设备   纯滚床 RB(如 1A020RB); 升降台 LT(如 1A010LT); TT/SL/TC/HE 同此带设备类型。
      X2 = 该位置的 RB   恒为 RB 身份(纯RB时与 X1 同号; 设备时是该位置的 RB 号)。
      branch: 台体分支 —— 默认 .IL.(单点台), 个别用 .ILO.(多点台)。默认取 TAG_BRANCH;
              需要 .ILO. 的节点由 deviceConfig 每台显式给 branch='ILO'。
    mid  : 非标准写法——段号后额外插一段(如 Line_1 355/390 的 14AS/15AS, 编程笔误)。正常点位为 None。
    """
    x1 = device or rb_index
    seg = device_area or _segment_of(x1)
    mid_part = f"_{mid}" if mid else ""
    branch = branch or TAG_BRANCH
    return f"{TAG_NS}{plc_code(plc)}_{seg}{mid_part}_{x1}_{rb_index}.{branch}.SD.{point}"


def default_tag(plc, rb_index, device=None, branch=None, device_area=None, mid=None):
    """车身号点位 tag: .L3L1_<段>[_<mid>]_<X1>_<X2>.[IL|ILO].SD.M1003_BodyID。规则见 _tag_path。"""
    return _tag_path(plc, rb_index, device=device, branch=branch, device_area=device_area,
                     point='M1003_BodyID', mid=mid)


def skid_tag(plc, rb_index, device=None, branch=None, device_area=None, mid=None):
    """雪橇号点位 tag: .L3L1_<段>[_<mid>]_<X1>_<X2>.[IL|ILO].SD.I1030_RBDataSkidNo。"""
    return _tag_path(plc, rb_index, device=device, branch=branch, device_area=device_area,
                     point='I1030_RBDataSkidNo', mid=mid)


def target_tag(plc, rb_index, device=None, branch=None, device_area=None, mid=None):
    """车身目标地址点位 tag: .L3L1_<段>[_<mid>]_<X1>_<X2>.[IL|ILO].SD.I1031_RBDataTarget。
    该停靠点车辆的下游目标(目的地)地址, 随车辆卡展示。"""
    return _tag_path(plc, rb_index, device=device, branch=branch, device_area=device_area,
                     point='I1031_RBDataTarget', mid=mid)


def _short_plc(short):
    """泛化线名 -> PLC 短线代号。Line_3 -> L3, Line_7 -> L7, Exit_2 -> X2。"""
    s = str(short or '')
    m = _LINE_SHORT_RE.match(s)
    if m:
        return 'L' + m.group(1)
    m = _EXIT_SHORT_RE.match(s)
    if m:
        return 'X' + m.group(1)
    return s


def plc_code(short):
    """线/短线代号 -> 车间 PLC 代号。Line_1 -> L3L1; 已带前缀的短线代号原样拼接。"""
    s = str(short or '')
    if s.startswith(PLC_SYS_PREFIX):
        return s
    return PLC_SYS_PREFIX + _short_plc(s)


def plc_short_of_plc(plc):
    """车间 PLC 代号 -> 线代码。L3L1 -> Line_1; 未知时剥掉 L3 返回。"""
    p = str(plc or '')
    code = p[2:] if p.startswith(PLC_SYS_PREFIX) else p
    m = _LINE_PLC_RE.match(code)
    if m:
        return 'Line_' + m.group(1)
    m = _EXIT_PLC_RE.match(code)
    if m:
        return 'Exit_' + m.group(1)
    return code


def rb_index_of(node_id):
    """从节点ID提取滚床编号。Line_1_1D325RB -> 1D325RB; 1A005RB -> 1A005RB。"""
    for tok in node_id.split('_'):
        if tok and any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok) and tok.endswith('RB'):
            return tok
    return node_id.split('_')[-1]


def line_of(node_id):
    """从节点ID取线前缀。Line_1_1D325RB -> Line_1。"""
    return _split_line_of(node_id)


DEV_SUFFIXES = ('RB', 'LT', 'TC', 'TT', 'SL', 'HE')


def node_device_x1x2(node_id, dtype=None):
    """把一个拓扑节点的两个中段(X1 设备串, X2 RB串)还原成 EMOS tag 需要的两段。

    规则(用户给定): X1=实际停靠设备(纯RB=RB/LT/TT/SL/TC 各带类型), X2=该位置的 RB。
      纯RB 节点  Line_1_1A020RB        -> ('1A020RB', '1A020RB')
      LT 节点    Line_1_1A010LT_high   -> ('1A010LT', '1A010RB')   # 状态后缀 high/mid/low 不属于点位
      SL/TT      Line_1_1D135RB(改类型) -> ('1D135TT', '1D135RB')  # 节点名是 RB, 但功能件被标成 TT/SL, X1 按其类型
      TC 节点(横移车)               -> ('1X005TC', '1X005RB')   # X2 落对应位置的 RB
    位置号 = 段字母+数字(如 1A010)。解析失败退回纯RB形式。

    dtype : 可选, 拓扑给节点标的功能件类型('TT'/'SL'/'LT'/'TC')。RB 名称节点若被覆盖成
            TT/SL(如转台/跨层), 所在位置仍有滚床可停, 此时 X1 应按实际设备类型、X2 恒为该
            位置的 RB。不传(或名称本就带设备后缀)时按名称判断。
    """
    carr = None
    for tok in node_id.split('_'):
        if re.match(rf'^\d*[A-Z]+\d+(?:{"|".join(DEV_SUFFIXES)})$', tok):
            carr = tok
            break
    if carr is None:
        return rb_index_of(node_id), rb_index_of(node_id)
    m = re.match(r'^(\d*[A-Z]+\d+)([A-Z]+)', carr)
    pos, suf = m.group(1), m.group(2)
    if suf == 'RB':
        if dtype == 'LT':                               # LT 工位(=同号RB, 升降台): X1=X2=LT (现场取 LT_LT, 弃 LT_RB)
            return f"{pos}LT", f"{pos}LT"
        if dtype in DEV_SUFFIXES and dtype != 'RB':     # 名称是 RB 但功能件类型被覆盖(TT/SL/TC...)
            return f"{pos}{dtype}", f"{pos}RB"          # X1=实际设备, X2=该位置 RB
        return f"{pos}RB", f"{pos}RB"
    if suf == 'LT' and node_id.rsplit('_', 1)[-1] == carr:
        # 纯LT存位: 节点名就以LT结尾(且无 高/中/低 状态后缀), X1=X2=LT (如 Line_4_1C205LT)
        return f"{pos}LT", f"{pos}LT"
    return f"{pos}{suf}", f"{pos}RB"


# 决策表 DEC 订阅: 每决策点铺决策表行 tag(DB符号直达, 无 .IL.SD. 层), 走 make_extra_resolver kind='dec'。
# 低频: 启动一次性订阅 + dataChange 变化即推(工人工改才动), 不按轮询; 作路径规划/推算用。
DEC_ROWS, DEC_COLS = 4, 12                 # 决策表 4页 × 12行
_DEC_MAX_TRIES = 10                        # 单格补齐重试上限(超过仍不回 → 认定无法回值, 放弃)
# 实测(2026-11-06 健康会话全字段后): EMOS 稳定回 Dest/Use/Filtr01..05(存在数据的点均 48/48/48);
# SB.S9999_Prior 回得**极不一致**(同会话有的点 48 有的 0/1/26), 且富表已不显示优先级列, 故不订(省负载)。
# demo 表 SB.Data1..5Match / SB.FullMatch 真线 EMOS 不回, 同样不订。
DEC_FIELDS = (['AS.I9999_Dest', 'SB.S9999_Use']
              + [f'AS.I9999_Filtr0{i}' for i in range(1, 6)])   # 结果道号 + 启用 + 5条件(VIN/CodeF/CodeB/车型/平台)


def build_dec_subscriptions(dec_points_by_line):
    """由 config 的 DEC_POINTS 展开决策表行 tag 作为订阅点(kind='dec', node=dec_id)。

    dec_points_by_line: {短线代号('Line_4'): [dec_dict]}。dec_dict 需含 id/db
    (db 如 'L3L4_1A_LT025DecVisu')。订阅 tag 形如
      <db>.Row[页,行].<AS|SB>.<字段>; plc = 该线代号。
    返回点表可直接并入 _resolve_points 的 pending 订阅集 —— 从而同时喂给
    WorkshopSource 订阅 + make_extra_resolver(整串 tag 命中 dec_id) + translate 解析行格。
    """
    out = []
    for pf, decs in (dec_points_by_line or {}).items():
        plc = plc_code(pf)
        for d in decs:
            db = (d.get('db') or '').strip().strip('.')
            if not db:
                continue
            dec_id = d.get('id') or f'{pf}_DEC'
            for p in range(1, DEC_ROWS + 1):
                for r in range(1, DEC_COLS + 1):
                    for fld in DEC_FIELDS:
                        # 决策表 DB 符号订阅必须带前导点(与 body tag 的 TAG_NS='.' 一致; 实测剥点回 WSS.Error 收不到)。.
                        out.append({'node': dec_id, 'plc': plc,
                                    'tag': f'.{db}.Row[{p},{r}].{fld}', 'kind': 'dec'})
    return out


def build_subscriptions(nodes, short_of=None, tag_fn=None, with_skid=True, with_target=True, dtype_of=None, mid_of=None, branch_of=None):
    """按命名约定为一组节点生成订阅点。返回 [{'node','plc','tag','kind'}, ...]。

    每个节点生成三条(可停滚床/LT/存位):
      kind='body'  (车身号 M1003_BodyID)
      kind='skid'  (雪橇号 I1030_RBDataSkidNo)
      kind='target'(车身目标地址 I1031_RBDataTarget) —— 该车下游目标, 随车辆卡展示
      台体分支默认 .IL.。kind 用于 translate_message 区分处理, node 解析共用(设备路径相同)。
    nodes     : 本线滚床节点ID列表(来自 Line_1/Line_2/Line_3/Line_4 拓扑)。可为纯RB, 也可含 LT/TT/SL/TC。
    short_of  : node->短线代号, 默认取节点前缀(Line_1/Line_2/Line_3/Line_4)。
    tag_fn    : node,short -> tag(返回车身号 tag); 若提供, 雪橇/目标按同路径 .I1030/_I1031 补。
    with_skid : 是否同时生成雪橇号订阅(默认 True)。
    with_target: 是否同时生成目标地址订阅(默认 True; 真机个别点缺失可关)。
    dtype_of  : node -> 功能件类型('TT'/'SL'/'LT'/'TC'), 用于按实际设备生成 X1。
    mid_of    : node -> 非标准中间段(如 14AS/15AS), 正常点位返回 None; 各点共用。
    branch_of : node -> 台体分支(.IL./.ILO.), 正常点位返回 None(取默认); 个别 .ILO. 由 deviceConfig 显式给。
    """
    short_of = short_of or line_of

    def _mk(n):
        sh = short_of(n)
        plc = plc_code(sh)
        dtype = dtype_of(n) if dtype_of else None
        mid = mid_of(n) if mid_of else None
        branch = branch_of(n) if branch_of else None
        if tag_fn:
            body = tag_fn(n, sh)
        else:
            x1, x2 = node_device_x1x2(n, dtype)
            body = default_tag(sh, x2, device=x1, mid=mid, branch=branch)
        x1, x2 = node_device_x1x2(n, dtype)
        out = [{'node': n, 'plc': plc, 'tag': body, 'kind': 'body'}]
        if with_skid:
            skid = skid_tag(sh, x2, device=x1, mid=mid, branch=branch)
            out.append({'node': n, 'plc': plc, 'tag': skid, 'kind': 'skid'})
        if with_target:
            tgt = target_tag(sh, x2, device=x1, mid=mid, branch=branch)
            out.append({'node': n, 'plc': plc, 'tag': tgt, 'kind': 'target'})
        return out
    pts = []
    for n in nodes:
        pts += _mk(n)
    return pts


def load_subscriptions(path):
    """从 JSON 文件读订阅点(权威来源, 真机设备 config 落地后用它)。

    结构: {"points":[{"node":"Line_1_1D325RB","plc":"Line_1","tag":"....","kind":"body"}], ...}
    """
    with open(path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    return cfg.get('points', [])


def load_device_config(path):
    """从"每条输送一个"的设备配置目录(或单文件)载入订阅点 → 与 load_subscriptions 同构的 points 列表。

    path 为目录时读其中所有 *.json(按文件名排序, Line_1/Line_2/Line_3…); 为文件时读单个。
    每份文件结构:
      {
        "plc": "L3L1",
        "rollers": [ {"node","device","type","mid"}, ... ],    # 自动生成 车身+雪橇 两条(device/type/mid 决定了 X1/X2/branch)
        "points":  [ {"node","tag","kind","plc"}, ... ]        # 额外订阅(位信号/状态等, tag 手写完整, 不推导)
      }
    返回 [{node, plc, tag, kind}, ...]: rollers 生成 kind='body'/'skid'; points 原样带上其 kind(默认 'pos')。
    """
    paths = []
    if os.path.isdir(path):
        paths = sorted(os.path.join(path, f) for f in os.listdir(path)
                       if f.lower().endswith('.json'))
    else:
        paths = [path]
    out = []
    for p in paths:
        with open(p, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        rollers = cfg.get('rollers') or []
        rm = {r.get('node'): r for r in rollers}
        nodes = [n for n in rm if n]
        if nodes:
            out.extend(build_subscriptions(
                nodes,
                dtype_of=(lambda n, rm=rm: rm[n].get('type') or None),
                mid_of=(lambda n, rm=rm: rm[n].get('mid') or None),
                branch_of=(lambda n, rm=rm: rm[n].get('branch') or None)))
        for x in (cfg.get('points') or []):
            out.append({'node': x.get('node'),
                        'plc': x.get('plc') or cfg.get('plc')
                               or plc_code(line_of(x.get('node') or '')),
                        'tag': x['tag'],
                        'kind': x.get('kind', 'pos')})
    return out


def load_points(path):
    """通用订阅点载入: 目录(每线一个 deviceConfig) / 单文件(flat points 或 per-line deviceConfig)。"""
    if os.path.isdir(path):
        return load_device_config(path)
    with open(path, 'r', encoding='utf-8') as f:
        d = json.load(f)
    if isinstance(d, dict) and 'points' in d:
        return d.get('points') or []
    return load_device_config(path)


def make_extra_resolver(points):
    """把订阅点表打成一个 tag->(kind, node) 解析函数, 只针对显式登记的附加点位。

    附加点位(位信号/TC位置信号等)不走内容兜底, 而用 config 里**手写整条 tag** 精确匹配,
    每个点位显式落到它的目标 node。匹配键 = 规范化整条 tag(含点位后缀, 如 .SB.S1069_PRev),
    所以共享同一设备前缀的多个信号(TC 的 R/F 位偏置各一条)能各自命中不同 node —— 这正是
    TC 位置分辨所必需(前缀子串无法分开 R/F)。返回 None 表示非附加点位(按 body/skid 兜底)。
    """
    pairs = []
    exact = {}
    for p in points:
        tag = (p.get('tag') or '').strip().lstrip('.')
        node = p.get('node')
        kind = p.get('kind', 'pos')
        if tag and node and kind not in ('body', 'skid'):
            pairs.append((tag, kind, node))
            exact[tag] = (kind, node)          # 完整订阅 tag → (kind,node), O(1) 精确查
    pairs.sort(key=lambda pr: len(pr[0]), reverse=True)

    def resolve(tag):
        t = (tag or '').strip().lstrip('.')
        # 快路径: 精确命中(回推 tag 与订阅 tag 逐字一致时)
        hit = exact.get(t)
        if hit:
            return hit
        # 快路径2: EMOS 回推带 plc 前缀(如 'L3L4.') → 剥掉首个点段后再精确查一次
        # (订阅 tag 无前导点, 回推 = '<PLC>.' + 订阅 tag; 绝大多数 DEC/位信号回帧即此形态)
        if '.' in t:
            seg, rest = t.split('.', 1)
            if seg.startswith(PLC_SYS_PREFIX) and seg[2:].isalnum():   # 严格校验 'L3*' 才是 plc 前缀
                hit = exact.get(rest)
                if hit:
                    return hit
        # 设备 body/skid/target 帧不属于附加点位: 直接交给下游按内容兜底, 不做线性扫(热点, O(n))
        if 'M1003_BodyID' in t or 'SkidNo' in t or 'RBDataTarget' in t:
            return None
        # 兜底: 前缀/规范化差异, 退化为子串匹配(仅少量非热点额外点位会走到)
        for k, kind, node in pairs:
            if k in t:
                return (kind, node)
        return None
    return resolve


# ---------------------------------------------------------------------------
# 报文 -> 统一事件　(字符串/字节帧通用)
# ---------------------------------------------------------------------------
def translate_message(frame, node_of_tag=None, extra_resolver=None):
    """把服务器一帧报文翻译成统一事件(可能 0 或多个)。

    frame          : bytes 或 str 的 JSON 报文(若是心跳/info 等, 返回空列表)。
    node_of_tag    : tag -> 本项目节点ID(车身/雪橇用); 缺省用 rb_index 匹配。
    extra_resolver : tag -> (kind, node), 显式登记的附加点位(位信号/TC位置); 命中则按其路由,
                     否则按 body/skid 内容兜底。参见 make_extra_resolver。
    返回事件列表:
      ('clear', node)                   车身号点位回推为空 → 释放
      ('place', node, vehicle_dict)     车身号点位回推有效值 → 占用
      ('skid', node, sled_no)           雪橇号点位回推 → 更新该车雪橇号
      (kind, node, value)               附加点位(如 kind='pos' 位信号) → 通用事件, node 由 config 显式给定
      ('print', text)                   日志/无法定位
    区分: 优先查 extra_resolver(共享设备前缀的 R/F 信号也能各自落到正确 node);
          否则按 tag 内容区分(含 'I1030_RBDataSkidNo'/'SkidNo' 视为雪橇号, 其余按车身号)。
    """
    if isinstance(frame, (bytes, bytearray)):
        frame = frame.decode('utf-8', errors='replace')
    try:
        msg = json.loads(frame)
    except Exception:
        return [('print', f"[ws] 非JSON帧: {str(frame)[:80]}")]

    tag = msg.get('tag')
    value = msg.get('value')
    if not tag:
        return []                                    # info / 心跳应答 / 其它

    # 附加点位(位信号/TC位置等): 走订阅时整条 tag 精确登记的 (kind, node), 不按内容兜底。
    # 节点身份由 config 显式给出, 即使同一设备前缀有 R/F 两个信号也可分到不同目标格。
    hit = extra_resolver(tag) if extra_resolver else None
    if hit:
        kind, node = hit
        if kind == 'dec':                              # 决策表行: 抽 Row[p,r].字段 供 consume 存 dec 段
            m = _RX_DEC_CELL.search(tag or '')
            p, r, grp, fld = ((int(m.group(1)), int(m.group(2)), m.group(3), m.group(4))
                              if m else (0, 0, '', ''))
            return [('dec', node, (p, r, f'{grp}.{fld}', str(value)))]
        return [(kind, node, str(value))]

    is_body = 'M1003_BodyID' in tag                  # 车身号点位
    is_skid = ('SkidNo' in tag) or ('I1030_RBDataSkidNo' in tag)  # 雪橇号点位
    if not is_body and not is_skid:
        return []                                    # 其它未登记点位, 忽略

    node = node_of_tag(tag) if node_of_tag else _rb_to_node(tag)
    if not node:
        return [('print', f"[ws] 无法定位节点: {tag}")]

    if is_skid:
        sled = '' if VehicleId.is_empty(value) else str(value).strip()
        return [('skid', node, sled)]                # 更新该车雪橇号

    if VehicleId.is_empty(value):
        return [('clear', node)]
    return [('place', node, VehicleId.to_vehicle(value))]


def _rb_to_node(tag):
    """无显式映射时, 用 tag 里的滚床编号兜底还原为节点ID(前缀缺省 F..)。"""
    m = rgx_rb.search(tag)
    return m.group(1) if m else None


rgx_rb = re.compile(r'_(\d+[A-Z]+\d+[A-Z]+)_')

# 决策表行 tag 定位: <...>DecVisu.Row[页,行].SB|AS|CB|AA.<字段>
_RX_DEC_CELL = re.compile(r'\.Row\[(\d+),(\d+)\]\.(AS|SB|CB|AA)\.(\w+)$')


# ---------------------------------------------------------------------------
# 实时源: 连 EMOS ws
# ---------------------------------------------------------------------------
class WorkshopSource(VehicleSource):
    """EMOS ws 实时源。events() 为拉式生成器, 与 consume() 直接对接。

    连接/重连/心跳都在内部线程处理; 断线自动重连并重订阅。
    连接失败时不阻塞, yield 一条 ('print', 原因) 后正常结束。
    """

    def __init__(self, url, points, heartbeat_ms=2000, reconnect_ms=5000,
                 max_reconnect=-1, out=None, poll_ms=1000):
        self.url = url if url.startswith('ws') else f"ws://{url}"
        self.points = points or []
        self.hb = heartbeat_ms / 1000.0
        self.retry = reconnect_ms / 1000.0
        self.max_retry = max_reconnect
        # 占用点周期重订: EMOS 只在值变化时推, 一辆车停住后只有"进站那一条" place,
        # 停留统计 (MAX(ts)-MIN(ts)) 恒为 0。对**当前占用**的 body 点每秒重发一次
        # advise(实测每次 advise 必重推当前值), 重现与历史库一致的 1s 级重复上报。
        # 0 = 关闭(纯变化推送, 省流量但停留恒 0)。
        self.poll_sec = max(0.0, (poll_ms or 0) / 1000.0)
        self._occ_pin = {}          # node -> 当前占用车的 pin_code(判断 place 是否重复上报)
        self._node_body = {}        # node -> body 点 advise 载荷(周期重订用)
        self._poll_ts = 0.0
        self._poll_rounds = 0
        self._n_place = 0
        self._node_by_tag = {(p.get('tag'),): p.get('node') for p in self.points}
        self._node_of_tag = None
        self._extra_resolver = None
        self._prefetch = []      # 订阅铺发期间排空到的应答帧, 主循环先消费(防大批量把 socket 堵死)
        # 决策表 DEC 存量补齐: EMPOS 大批量铺发时初始值回推**可能随机漏掉整块**(如某决策点整页不回),
        # 且静态值之后不再变、永不重推 → 必须对"没回过的格"定时重新 advise 直到铺全。
        # key = node|页|行|组.字段; value = (plc, tag)。
        self._dec_expect = {}
        for p in self.points:
            if p.get('kind') == 'dec':
                m = _RX_DEC_CELL.search(p.get('tag') or '')
                if m:
                    key = f"{p['node']}|{m.group(1)}|{m.group(2)}|{m.group(3)}.{m.group(4)}"
                    self._dec_expect[key] = (p.get('plc', ''), p.get('tag', ''))
        self._dec_seen = set()
        self._dec_tries = {}         # 每格补发次数(超 _DEC_MAX_TRIES 放弃)
        self._bf_done = False        # 本连接 DEC 补齐是否完成
        self._bf_rounds = 0
        self._bf_ts = 0.0
        self._connect_ts = 0.0
        # node -> 该停车点的 body/skid/target 订阅载荷: 车上位时重探一口气拿齐雪橇/目标, 与车身同步
        self._node_reprobe = {}
        for p in self.points:
            if p.get('node') and p.get('kind') in ('body', 'skid', 'target') and p.get('tag'):
                self._node_reprobe.setdefault(p['node'], []).append(
                    {'id': p.get('id', ''), 'type': 'advise',
                     'plc': p.get('plc', ''), 'tag': p.get('tag', '')})
        for p in self.points:                # 周期重订只发 body: 雪橇/目标不随停留变化
            if p.get('node') and p.get('kind') == 'body' and p.get('tag'):
                self._node_body[p['node']] = {'id': 'poll', 'type': 'advise',
                                              'plc': p.get('plc', ''), 'tag': p.get('tag', '')}

    # --- 订阅/心跳发送(在同一条连接上) ---
    def _subscribe_all(self, ws):
        # 附加点位(位信号/TC位置等, kind 非 body/skid)与 主体(车身/雪橇)分开、**延迟单独发一轮**。
        # 实测(2026-10-21): 一口气连发 461 条 body/skid 时, 末尾少数附加点的**初始值回推会被挤掉**;
        # 而附加点值多静止(如 TC 恒在某位), 之后不再变、永不重推。趁主批量静默后再独发,
        # 像 probe 一样可靠拿到当前值。主/附各自的 kind 解析顺序无关(resolver 按长度排序)。
        # 大批量(DEC 已扩到 11点×48行×14字段≈7400)必须**边发边排空接收缓冲**: 铺发结束前没人读 socket,
        # EMOS 应答塞满接收缓冲 → 服务端读不进我们的 advise → ws.send 卡到超时丢连接(实测 "连接失败 timed out")。
        extra = [p for p in self.points if p.get('kind') not in ('body', 'skid')]
        main = [p for p in self.points if p.get('kind') in ('body', 'skid')]

        def _chunked(batch, chunksize):
            for _i in range(0, len(batch), chunksize):
                for p in batch[_i:_i + chunksize]:
                    ws.send(json.dumps({'id': p.get('id', ''), 'type': 'advise',
                                        'plc': p.get('plc', ''), 'tag': p.get('tag', '')}))
                self._drain_while(ws)          # 排空已到的应答, 防缓冲溢出
                if _i + chunksize < len(batch):
                    time.sleep(0.05)

        _chunked(main, 300)
        if extra:
            time.sleep(1.5)                     # 让主批量推完, 给附加点一个静默窗口
            _drain_while = self._drain_while
            _step = 60                       # 初铺也小批(实测 400 连发随机丢块; 60+排空稳)
            for _i in range(0, len(extra), _step):
                for p in extra[_i:_i + _step]:
                    ws.send(json.dumps({'id': p.get('id', ''), 'type': 'advise',
                                        'plc': p.get('plc', ''), 'tag': p.get('tag', '')}))
                self._drain_while(ws)
                time.sleep(0.08)
        self._drain_while(ws)
        return len(self.points)

    def _drain_while(self, ws):
        """非阻塞排空当前可读帧, 暂存进 self._prefetch 供主循环先消费(不丢数据)。"""
        try:
            saved = ws.gettimeout()
        except Exception:
            saved = None
        try:
            ws.settimeout(0.05)
            cnt = 0
            while cnt < 4000:
                try:
                    self._prefetch.append(ws.recv())
                except Exception:
                    break
                cnt += 1
        finally:
            try:
                ws.settimeout(saved)
            except Exception:
                pass

    def _reprobe(self, ws, node):
        """车上位到 node: 重发该点的 body/skid/target advise, 触发服务器重推当前值,
        使车身/雪橇/目标三份数据尽量在同一时刻取齐(消除 skid/target 滞后于 body 的窗口)。"""
        for payload in self._node_reprobe.get(node, []) or []:
            try:
                ws.send(json.dumps(payload))
            except Exception:
                return

    def _maybe_poll(self, ws):
        """对当前占用点周期重发 body advise, 触发服务器重推同一车号(重复 place)。

        EMOS 是变化推送: 车停稳后不会再推, 轨迹库只有进站一条 → 停留 MAX-MIN 恒 0。
        重订只要 1s 一次即可复现历史库的重复上报节奏; 只订**有车**的点, 空位靠 clear
        变化推送即可。返回 True 表示本轮发过重订。"""
        if self.poll_sec <= 0:
            return False
        now = time.time()
        if now - self._poll_ts < self.poll_sec:
            return False
        self._poll_ts = now
        self._poll_rounds += 1
        if self._poll_rounds <= 2 or self._poll_rounds % 30 == 0:
            print(f"[ws] 重订第{self._poll_rounds}轮: 占用 {len(self._occ_pin)} 点, "
                  f"place 累计 {self._n_place}", flush=True)
        if not self._occ_pin:
            return False
        for node in list(self._occ_pin):
            payload = self._node_body.get(node)
            if not payload:
                continue
            try:
                ws.send(json.dumps(payload))
            except Exception:
                break
        return True

    def _heartbeat(self, ws, stop):
        while not stop.is_set():
            time.sleep(self.hb)
            try:
                ws.send(json.dumps({'type': 'info', 'info': 'alive'}))
            except Exception:
                return

    # --- 决策表存量补齐: 对还没回过值的格重新 advise, 直到铺全 ---
    # 实测 EMOS 大批量连发会随机丢块, 而单条/小批量重订必定重推当前值。
    # 数据是静态的(错过后永不重推), 所以补齐必须跑到"未回格清零"为止, 不能按时钟冻结;
    # 每个格设重试上限(_DEC_MAX_TRIES), 防止个别真·无法回值的格卡住死循环。
    def _maybe_backfill(self, ws):
        if self._bf_done:
            return True
        if time.time() - self._bf_ts < 0.2:          # 限速: 每 0.2s 一批, 防再大批量丢
            return self._bf_done
        pending = []
        for k, v in self._dec_expect.items():
            if k in self._dec_seen:
                continue
            if self._dec_tries.get(k, 0) >= _DEC_MAX_TRIES:   # 重试到上限仍不回 → 放弃(不再补)
                self._dec_seen.add(k)                          # 视作已处理, 让本轮能收敛结束
                continue
            pending.append((k, v))
        if not pending:
            self._bf_done = True
            return True
        n = 0
        for _k, (_plc, _tag) in pending:
            if n >= 25:                              # 小批量
                break
            try:
                ws.send(json.dumps({'id': 'bf', 'type': 'advise', 'plc': _plc, 'tag': _tag}))
                self._dec_tries[_k] = self._dec_tries.get(_k, 0) + 1
                n += 1
            except Exception:
                break
        self._bf_ts = time.time()
        self._bf_rounds += 1
        return self._bf_done

    # --- 生成器主循环 ---
    def events(self):
        if not _HAVE_WS:
            yield ('print', "[ws] 未安装 websocket-client, 无法实时连接; 用 ReplaySource 离线回放")
            return

        if not self._node_of_tag:
            # 优先按显式 node 映射(子串匹配); 否则退化为按滚床编号
            self._node_of_tag = (make_node_resolver(self.points)
                                 if any(p.get('node') for p in self.points)
                                 else None)
        if self._extra_resolver is None:
            self._extra_resolver = make_extra_resolver(self.points)

        attempts = 0
        while True:
            if self.max_retry >= 0 and attempts > self.max_retry:
                yield ('print', "[ws] 重连次数超限, 中止")
                return
            try:
                # proxy_type=None / http_proxy_host=None: 直连, 不经系统/环境代理(内网车间服务器)
                ws = websocket.create_connection(self.url, timeout=90,   # 大批量(~7900)铺发需更大余量
                                                 proxy_type=None, http_proxy_host=None, http_proxy_port=None)
                yield ('print', f"[ws] 已连接 {self.url}")
                attempts = 0
                # 每个连接重新开始 DEC 补齐: 上次连接可能中途断掉只铺了一部分
                self._connect_ts = time.time()
                self._dec_seen = set()
                self._dec_tries = {}
                self._bf_done = False
                self._bf_rounds = 0
                self._bf_ts = 0.0
                self._occ_pin = {}               # 初始 advise 洪峰会重新填满占用表
                self._poll_ts = time.time()

                nsub = self._subscribe_all(ws)
                yield ('print', f"[ws] 已订阅 {nsub} 个滚床点位")

                stop = threading.Event()
                hbth = threading.Thread(target=self._heartbeat, args=(ws, stop), daemon=True)
                hbth.start()

                try:
                    n_bad = 0
                    ws.settimeout(0.2)          # 读超时用于空闲推进 DEC 补齐(不判为断线)
                    while True:
                        try:
                            frame = self._prefetch.pop(0) if self._prefetch else ws.recv()
                        except Exception as _e:
                            # 读超时(空闲): 推进补齐, 不做断线判断。兼容不同 websocket 版本的异常类/文本。
                            if ('timeout' in type(_e).__name__.lower()) or ('timed out' in str(_e).lower()):
                                if not self._bf_done:
                                    self._maybe_backfill(ws)
                                self._maybe_poll(ws)
                                continue
                            raise
                        for ev in translate_message(frame, self._node_of_tag, self._extra_resolver):
                            if ev and ev[0] == 'place':
                                self._n_place += 1
                                pin = ev[2].get('pin_code') if len(ev) > 2 and isinstance(ev[2], dict) else None
                                if self._occ_pin.get(ev[1]) != pin:
                                    self._reprobe(ws, ev[1])  # 真·进站: 重探 body/skid/target 同步取齐
                                self._occ_pin[ev[1]] = pin
                            elif ev and ev[0] == 'clear':
                                self._occ_pin.pop(ev[1], None)
                            elif ev and ev[0] == 'dec':      # 决策表行: 记已见, 供补齐只补缺格
                                _n, (_p, _r, _fld, _v) = ev[1], ev[2]
                                self._dec_seen.add(f"{_n}|{_p}|{_r}|{_fld}")
                            yield ev
                        # 空闲或收帧后推进 DEC 存量补齐, 直到未回格清零
                        if not self._bf_done:
                            self._maybe_backfill(ws)
                        self._maybe_poll(ws)             # 占用点 1s 重订(停留统计用)
                        n_bad = 0
                except Exception as e:
                    yield ('print', f"[ws] 连接中断 ({e}), 重连中…")
                finally:
                    stop.set()
                    try:
                        ws.close()
                    except Exception:
                        pass
            except Exception as e:
                attempts += 1
                yield ('print', f"[ws] 连接失败 ({e}); {self.retry}s 后重试 ({attempts})")
                if self.max_retry >= 0 and attempts > self.max_retry:
                    yield ('print', "[ws] 重连次数超限, 中止")
                    return
                time.sleep(self.retry)


def make_node_resolver(points):
    """把订阅点表打成一个 tag->节点 解析函数。

    服务器回推 tag 可能带规范化前缀且逐台分支可能不同(实测: 订阅得到回推 `L3L1.L3L1_..1A020RB..`, 台体分支 IL/ILO 各台不同),
    所以不做整串精确匹配。节点身份只看"设备路径"(=tag 里第一个点之前、L3Lx后的那一段, 如 L3L1_1A_1A020RB_1A020RB),
    各点位设备路径互异; 只要它在回推 tag 里出现即归属对应节点(与前面 L3Lx. 前缀和后面 .IL[O].SD.. 分支都无关)。
    """
    pairs = sorted(
        ((p['tag'].lstrip('.').split('.')[0], p['node']) for p in points
         if p.get('tag') and p.get('node')),
        key=lambda pr: len(pr[0]), reverse=True)

    def resolve(tag):
        t = (tag or '').strip()
        for k, node in pairs:
            if k in t:
                return node
        return None
    return resolve


# ---------------------------------------------------------------------------
# 离线源: 回放录制报文, 走同一条翻译路径
# ---------------------------------------------------------------------------
class ReplaySource(VehicleSource):
    """把录制的 EMOS 报文(每行一个 JSON / jsonl)按原顺序回放成统一事件。

    replay(log_lines_or_file): 逐条喂给 translate_message, 与 WorkshopSource 共用翻译。
    用来在无法连通车间服务器时, 离线路测整条 place/clear -> 占用表 -> 快照 -> 可视化 链路。
    """

    def __init__(self, path=None, lines=None, node_of_tag=None, extra_resolver=None):
        self.path = path
        self.lines = list(lines or [])
        self.node_of_tag = node_of_tag
        self.extra_resolver = extra_resolver

    def events(self):
        if self.path:
            with open(self.path, 'r', encoding='utf-8') as f:
                lines = list(f)
        else:
            lines = self.lines
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            for ev in translate_message(ln, self.node_of_tag, self.extra_resolver):
                yield ev
        yield ('print', "[replay] 回放结束")


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--replay', help='回放 JSONL 片段并打印翻译后的统一事件(离线自测)')
    args, _ = ap.parse_known_args()
    if args.replay:
        from runtime_state import VehicleState, consume
        st = consume(ReplaySource(path=args.replay), VehicleState(), live=True)
        print("\n占用表:")
        from runtime_state import print_table
        print_table(st)
    else:
        print("import 模块。实时: WorkshopSource(url,points); 离线: ReplaySource。")