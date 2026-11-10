# -*- coding: utf-8 -*-
"""
轨迹统计 Web API — 只读查询 MySQL `conveyor_traj.vehicle_events`, 供前端问答复用。

启动(默认 127.0.0.1:8766, 与 8765 实时页面同机多端口, 不重启 run_runtime):
    python -X utf8 -u codes/traj_api.py --port 8766

派生表 vehicle_runs:
    --build-runs       增量更新(只扫新 place 事件, 秒级); 首次无水位时自动全量
    --build-runs-full  强制全量重建(staging + RENAME 原子换表, 数分钟)

端点:
    GET /api/health          连通性 / 数据时间范围
    GET /api/point_traffic   ?node_id=... | area=... &start_time=&end_time=&time_range=30m&window=300
                             过车量: 按 (pin_code, 时间窗) 去重, 绝不用裸 COUNT(*)
    GET /api/bottlenecks     ?start_time=&end_time=&hours=24&limit=5&min_visits=3
                             平均停留最久的节点 TopN
    GET /api/vehicle_history ?query=<PIN|雪橇号|车身号> &limit=10
                             单台车的节点链 + 各节点停留秒数

时间参数接受 epoch 秒 或 'YYYY-MM-DD[ HH:MM:SS]' / ISO 'T' 格式; 也可传
time_range=30m/1h/24h/7d(或 半小时/30分钟), 后端严格换算成 start=now-timerange 并下推到 SQL.
缺省: point_traffic 用最近 24 小时, bottlenecks 用最近 hours(默认24)小时。

去重口径:
- place 事件每 ~2s 重复上报同车同节点, 直接 COUNT(*) 会把一台车算成几十台。
- point_traffic: SELECT COUNT(*) FROM (SELECT pin_code, FLOOR(ts/window) GROUP BY pin_code, win)
  即每台车每个时间窗最多算一次; 同时返回 COUNT(DISTINCT pin_code) 作为参考。
- bottlenecks / vehicle_history: 按 pin 的事件时间序把「同一节点连续 place」压成一段,
  停留 = 该段 MAX(ts_epoch) − MIN(ts_epoch)(EMOS 2s 重复上报自然折叠, 不含站间交接空档);
  不用 clear(其 pin_code 为空)。堵点均值再剔除 <2s(瞬间路过) 与 >15min(卡死/数据断档) 的段。
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request  # noqa: E402

from traj_store import load_db_config  # noqa: E402
from common import LINE_CODES, EXIT_CODES, dev_of, line_of  # noqa: E402

_UPPER_PREFIXES = tuple(sorted([c.upper() for c in LINE_CODES + EXIT_CODES],
                                key=len, reverse=True))
_UPPER_PREFIX_RE = re.compile(r'^((?:LINE|EXIT)_\d+)_')


def _split_upper(nid):
    """大写节点ID -> (区域前缀, 设备段)。'LINE_1_1A045RB' -> ('LINE_1','1A045RB')。

    固定表之外的未配置线(LINE_6/EXIT_4 等)按正则兜底。
    """
    s = str(nid or '').upper()
    for p in _UPPER_PREFIXES:
        if s.startswith(p + '_'):
            return p, s[len(p) + 1:]
    m = _UPPER_PREFIX_RE.match(s)
    if m:
        return m.group(1), s[m.end():]
    if '_' in s:
        return tuple(s.split('_', 1))
    return '', s

try:
    import pymysql
except Exception:  # pragma: no cover
    pymysql = None

app = Flask(__name__)
app.json.ensure_ascii = False
app.json.sort_keys = False

DEFAULT_WINDOW = 300       # 5 分钟去重窗
MAX_WINDOW = 86400
DEFAULT_BOTTLENECK_HOURS = 24
DWELL_CAP = 3600           # 建表时打 suspect 标记: 单段 > 1h 视为数据断档
MIN_DWELL_SEC = 2          # 堵点统计剔除 <2s(两节点交接抖动/瞬间路过)
MAX_DWELL_SEC = 900        # 堵点统计剔除 >15min(车卡死/漏 clear 的数据错误)
MIN_RUN_SEC = MIN_DWELL_SEC  # 轨迹展示同样合并 <2s 的交接抖动段
HISTORY_LIMIT = 10
TRACK_ACTIVE_SEC = 1800    # 最后一次 place 在 30 分钟内 → 视为仍停在末节点, 用 now-last 算已停留

# 派生表: 把每车连续同节点 place 压成一段(enter/leave/dwell), 供堵点/过车量快速查询.
# --build-runs 默认增量更新(只扫 id > 水位的 place 事件); --build-runs-full 才全量重建.
RUNS_TABLE = 'vehicle_runs'
RUNS_HWM_KEY = 'vehicle_runs.last_processed_event_id'
BUILD_LOCK = 'conveyor_traj.build_runs'
META_TABLE = 'system_meta'
META_DDL = """
CREATE TABLE IF NOT EXISTS system_meta (
  k          VARCHAR(64) NOT NULL PRIMARY KEY,
  v          VARCHAR(255) NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""
RUNS_DDL = """
CREATE TABLE IF NOT EXISTS vehicle_runs (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  pin_code    VARCHAR(14) NOT NULL,
  node_id     VARCHAR(64) NOT NULL,
  enter_ts    INT UNSIGNED NOT NULL,
  leave_ts    INT UNSIGNED NULL,
  dwell_sec   INT UNSIGNED NOT NULL DEFAULT 0,
  is_last     TINYINT(1) NOT NULL DEFAULT 0,
  suspect_gap TINYINT(1) NOT NULL DEFAULT 0,
  KEY idx_node_enter (node_id, enter_ts),
  KEY idx_pin_enter  (pin_code, enter_ts),
  KEY idx_enter      (enter_ts),
  KEY idx_last_pin   (is_last, pin_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

_KNOWN_NODES = None       # 短名 → [全名...], 由 build_line 汇总一次(供短名解析, 避免全表扫)


def _known_nodes():
    """build_line 里所有节点 → {短名: [Line_1_xxx, Line_2_xxx...]}. 失败返回空 dict."""
    global _KNOWN_NODES
    if _KNOWN_NODES is None:
        m = {}
        try:
            import build_line as BL
            for pf in BL.available_lines():
                _, raw, _ = BL.build_line(pf)
                for nd in raw['G'].nodes():
                    m.setdefault(dev_of(nd), []).append(nd)
        except Exception as e:
            print('  [warn] build_line 节点表加载失败:', e)
        _KNOWN_NODES = m
    return _KNOWN_NODES


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


@app.errorhandler(ApiError)
def _on_api_error(e):
    body = {'ok': False, 'error': e.message}
    body.update(e.extra)
    return jsonify(body), e.status


@app.errorhandler(Exception)
def _on_error(e):
    app.logger.exception('api error')
    return jsonify({'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}), 500


@app.after_request
def _cors(resp):
    # 页面由 127.0.0.1:8765 提供, 本服务在 8766, 需要跨端口放行(仅本机使用).
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ---------- 连接与时间 ----------
def _connect():
    if pymysql is None:
        raise ApiError('未安装 pymysql', 500)
    cfg = load_db_config()
    return pymysql.connect(
        host=cfg.get('host'), port=int(cfg.get('port', 3306)),
        user=cfg.get('user'), password=cfg.get('password'),
        database=cfg.get('database'), charset='utf8mb4', autocommit=True,
        connect_timeout=5, read_timeout=30)


def _parse_ts(v):
    """None/'' → None; epoch 数字或常见日期字符串 → int epoch."""
    if v is None:
        return None
    v = str(v).strip()
    if v == '':
        return None
    try:
        return int(float(v))
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return int(time.mktime(time.strptime(v, fmt)))
        except ValueError:
            continue
    raise ApiError("时间格式无法解析: %r (用 epoch 秒或 'YYYY-MM-DD HH:MM:SS')" % v)


def _ts_obj(epoch):
    return datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M:%S')


MAX_TIME_RANGE_SEC = 31 * 86400


def _parse_time_range(v):
    """严格解析相对窗口 (30m / 1h / 24h / 7d / 半小时 / 30分钟) 为秒数.

    只接受明确的相对时长; 无法解析时抛 400, 绝不静默回退成 24 小时全表默认窗口.
    """
    s = str(v or '').strip().lower().replace(' ', '')
    if not s:
        return None
    s = re.sub(r'^(?:最近|过去|近|last)', '', s)
    if s in ('今天', '今日', '当天'):
        d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int(time.time() - d.timestamp()))
    if s in ('昨天', '昨日'):
        return 86400
    if s in ('一周', '一星期', '7天', '七天'):
        return 7 * 86400
    if s in ('半小时', '半个小时', '半个钟头', '0.5小时', '0.5h', '30m', '30min', '30分钟'):
        return 1800
    cn = {'半': 0.5, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
          '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    m = re.fullmatch(r'([半一二两三四五六七八九十]+)(?:个)?(小时|钟头|分钟|天)', s)
    if m:
        raw, unit = m.group(1), m.group(2)
        if raw == '半':
            num = 0.5
        elif len(raw) == 1:
            num = cn[raw]
        else:
            num = None
        if num is not None:
            sec = num * (86400 if unit == '天' else (60 if unit == '分钟' else 3600))
            if 1 <= sec <= MAX_TIME_RANGE_SEC:
                return int(round(sec))
    m = re.fullmatch(r'(\d+(?:\.\d+)?)(s|sec|秒|m|min|分钟|分|h|小时|钟头|d|天|日)', s)
    if not m:
        raise ApiError('time_range 无法解析: %r (用 30m/1h/24h/7d 或 半小时/30分钟)' % v, 400)
    mult = {'s': 1, 'sec': 1, '秒': 1,
            'm': 60, 'min': 60, '分钟': 60, '分': 60,
            'h': 3600, '小时': 3600, '钟头': 3600,
            'd': 86400, '天': 86400, '日': 86400}[m.group(2)]
    sec = float(m.group(1)) * mult
    if not (1 <= sec <= MAX_TIME_RANGE_SEC):
        raise ApiError('time_range 超出允许范围: %r (1秒~31天)' % v, 400)
    return int(round(sec))


def _window(start_v, end_v, default_hours, time_range=None):
    """返回 (start, end, start_iso, end_iso).

    优先级: 显式 start_time/end_time > time_range 相对窗口 > default_hours.
    time_range 一旦传入就严格解析, 解析失败直接 400, 不再退回全表默认窗口.
    """
    end = _parse_ts(end_v)
    start = _parse_ts(start_v)
    if end is None:
        end = int(time.time())
    span = _parse_time_range(time_range)
    if start is None:
        start = end - (span if span is not None else int(default_hours * 3600))
    if start > end:
        raise ApiError('start_time 不能晚于 end_time', 400,
                       start_time=_ts_obj(start), end_time=_ts_obj(end))
    return start, end, _ts_obj(start), _ts_obj(end)


def _latest_ts(cur):
    cur.execute("SELECT MAX(ts_epoch) FROM vehicle_events")
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _table_exists(cur, name):
    cur.execute("SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=%s LIMIT 1", (name,))
    return cur.fetchone() is not None


def _runs_ready(cur):
    """派生表存在且非空 → 走快速路径."""
    if not _table_exists(cur, RUNS_TABLE):
        return False
    cur.execute("SELECT 1 FROM `%s` LIMIT 1" % RUNS_TABLE)
    return cur.fetchone() is not None


def _meta_get(cur, key):
    cur.execute("SELECT v FROM `%s` WHERE k=%%s" % META_TABLE, (key,))
    row = cur.fetchone()
    return row[0] if row else None


def _meta_set(cur, key, value):
    cur.execute(
        "INSERT INTO `%s` (k,v) VALUES (%%s,%%s) "
        "ON DUPLICATE KEY UPDATE v=VALUES(v)" % META_TABLE,
        (key, str(value)))


def _try_build_lock(cur, wait=0):
    cur.execute("SELECT GET_LOCK(%s,%s)", (BUILD_LOCK, int(wait)))
    row = cur.fetchone()
    return bool(row and int(row[0] or 0) == 1)


def _ensure_runs_index(cur):
    """老表可能没有 idx_last_pin; 增量只捞 is_last=1 的未闭合行, 需要该索引。"""
    cur.execute(
        "SELECT 1 FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name=%s AND index_name='idx_last_pin' "
        "LIMIT 1", (RUNS_TABLE,))
    if not cur.fetchone():
        cur.execute("ALTER TABLE `%s` ADD KEY idx_last_pin (is_last, pin_code)" % RUNS_TABLE)


def _build_runs_full(verbose=True):
    """全量重建: 扫 raw place 到本次水位, staging + RENAME 原子换表后写入水位。"""
    if pymysql is None:
        raise RuntimeError('未安装 pymysql')
    cfg = load_db_config()
    kwargs = dict(host=cfg.get('host'), port=int(cfg.get('port', 3306)),
                  user=cfg.get('user'), password=cfg.get('password'),
                  database=cfg.get('database'), charset='utf8mb4',
                  autocommit=True, connect_timeout=5)
    rd = pymysql.connect(read_timeout=900, **kwargs)
    wr = pymysql.connect(**kwargs)
    stage = RUNS_TABLE + '_new'
    old = RUNS_TABLE + '_old'
    n = 0
    try:
        wc = wr.cursor()
        if not _try_build_lock(wc):
            raise RuntimeError('另一个 build_runs 正在运行, 本次全量重建取消')
        wc.execute(META_DDL)
        # 全量重建期间先清掉水位: 即使中途崩溃, 下次也会重新全量, 不会拿旧水位做增量。
        wc.execute("DELETE FROM `%s` WHERE k=%%s" % META_TABLE, (RUNS_HWM_KEY,))
        # 先钉住水位, 再只扫 id <= 水位: 扫描期间新写入的事件留给下一次增量, 避免重复。
        wc.execute("SELECT COALESCE(MAX(id),0) FROM vehicle_events")
        hwm = int(wc.fetchone()[0] or 0)
        wc.execute("DROP TABLE IF EXISTS `%s`" % stage)
        wc.execute(RUNS_DDL.replace(RUNS_TABLE, stage, 1))
        wc.execute("DROP TABLE IF EXISTS `%s`" % old)

        rc = rd.cursor(pymysql.cursors.SSCursor)
        rc.execute(
            "SELECT pin_code, node_id, ts_epoch FROM vehicle_events "
            "WHERE id<=%s AND ev='place' AND pin_code<>'' AND node_id<>'' "
            "ORDER BY pin_code, ts_epoch, id", (hwm,))

        ins_sql = ("INSERT INTO `%s` "
                   "(pin_code,node_id,enter_ts,leave_ts,dwell_sec,is_last,suspect_gap) "
                   "VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % stage)
        batch = []
        cur_pin = cur_node = None
        enter = last_seen = None
        scanned = 0

        def close_run(is_last):
            nonlocal n
            # 停留 = MAX(ts_epoch) - MIN(ts_epoch), 两者都取"本节点"的上报:
            # last_seen 是该节点最后一次 place, 不含到下一节点的交接空档(EMOS 2s 重复上报已折叠).
            eff = last_seen
            dwell = int(max(0, eff - enter))
            batch.append((cur_pin, cur_node, int(enter), int(eff),
                          dwell, 1 if is_last else 0,
                          1 if dwell > DWELL_CAP else 0))
            n += 1

        for pin, node, ts in rc:
            ts = int(ts)
            scanned += 1
            if verbose and scanned % 1000000 == 0:
                print('  [runs] 全量扫描 %d 行, 已生成 %d 段' % (scanned, n))
            if pin != cur_pin:
                if cur_pin is not None:          # 上一个 pin 的末段: 用最后上报近似离开
                    close_run(True)
                    if len(batch) >= 5000:
                        wc.executemany(ins_sql, batch); batch = []
                cur_pin, cur_node = pin, node
                enter = last_seen = ts
            elif node != cur_node:               # 换站: 闭合上一段
                close_run(False)
                if len(batch) >= 5000:
                    wc.executemany(ins_sql, batch); batch = []
                cur_node, enter, last_seen = node, ts, ts
            else:                                 # 同站重复上报: 只更新最后见到时间
                last_seen = ts
        if cur_pin is not None:
            close_run(True)
        if batch:
            wc.executemany(ins_sql, batch)

        # 原子换表: 期间读请求要么看旧表, 要么看新表, 不会看到空表
        if _table_exists(wc, RUNS_TABLE):
            wc.execute("RENAME TABLE `%s` TO `%s`, `%s` TO `%s`"
                       % (RUNS_TABLE, old, stage, RUNS_TABLE))
            wc.execute("DROP TABLE IF EXISTS `%s`" % old)
        else:
            wc.execute("RENAME TABLE `%s` TO `%s`" % (stage, RUNS_TABLE))
        _meta_set(wc, RUNS_HWM_KEY, hwm)
        wc.execute("SELECT COUNT(*), MAX(enter_ts) FROM `%s`" % RUNS_TABLE)
        cnt, mx = wc.fetchone()
        if verbose:
            print('  [runs] 全量完成: %d 段, 水位 %d, 最新进入 %s' % (
                int(cnt or 0), hwm, _ts_obj(mx) if mx else '-'))
        return int(cnt or 0)
    finally:
        rd.close()
        wr.close()


def build_runs(verbose=True, full=False):
    """增量更新 vehicle_runs，只处理 id > system_meta 水位的新 place 事件.

    首次没有水位(或派生表不存在)时自动做一次全量。增量会先把每车最后一个
    is_last=1 的未闭合 run 捞出来，与新事件连续处理，因此跨扫描的轨迹不会截断。
    """
    if full:
        return _build_runs_full(verbose)
    if pymysql is None:
        raise RuntimeError('未安装 pymysql')
    cfg = load_db_config()
    kwargs = dict(host=cfg.get('host'), port=int(cfg.get('port', 3306)),
                  user=cfg.get('user'), password=cfg.get('password'),
                  database=cfg.get('database'), charset='utf8mb4',
                  connect_timeout=5)
    # 先确认派生表与水位; 任一缺失都退回一次全量。
    chk = pymysql.connect(autocommit=True, **kwargs)
    try:
        cc = chk.cursor()
        cc.execute(META_DDL)
        if not _table_exists(cc, RUNS_TABLE) or not _runs_ready(cc):
            if verbose:
                print('  [runs] 未找到派生表, 先做一次全量初始化')
            return _build_runs_full(verbose)
        hwm_raw = _meta_get(cc, RUNS_HWM_KEY)
        if hwm_raw is None:
            if verbose:
                print('  [runs] 未找到事件水位, 先做一次全量初始化')
            return _build_runs_full(verbose)
        _ensure_runs_index(cc)
        last_id = int(hwm_raw)
    finally:
        chk.close()

    rd = pymysql.connect(read_timeout=300, autocommit=True, **kwargs)
    wr = pymysql.connect(autocommit=False, **kwargs)
    scanned = written = 0
    try:
        if not _try_build_lock(wr.cursor()):
            if verbose:
                print('  [runs] 另一个 build_runs 正在运行, 跳过本次增量')
            cur0 = rd.cursor()
            cur0.execute("SELECT COUNT(*) FROM `%s`" % RUNS_TABLE)
            return int(cur0.fetchone()[0] or 0)
        rc = rd.cursor()
        rc.execute("SELECT COALESCE(MAX(id),0) FROM vehicle_events")
        upper = int(rc.fetchone()[0] or 0)
        if upper <= last_id:
            if verbose:
                print('  [runs] 无新增事件 (水位 %d)' % last_id)
            wc = wr.cursor()
            wc.execute("SELECT COUNT(*) FROM `%s`" % RUNS_TABLE)
            return int(wc.fetchone()[0] or 0)

        # 每车最后一段 run 是 is_last=1; 它就是跨扫描需要续上的未闭合状态。
        rc2 = rd.cursor()
        rc2.execute(
            "SELECT id,pin_code,node_id,enter_ts,COALESCE(leave_ts,enter_ts),suspect_gap "
            "FROM `%s` WHERE is_last=1" % RUNS_TABLE)
        open_by_pin = {}
        for rid, pin, node, enter, last_seen, _sg in rc2.fetchall():
            st = {'row_id': int(rid), 'pin': pin, 'node': node,
                  'enter': int(enter), 'last_seen': int(last_seen or enter)}
            prev = open_by_pin.get(pin)
            if prev is None or st['last_seen'] > prev['last_seen']:
                open_by_pin[pin] = st

        updates, inserts = [], []

        def flush_run(run, is_last):
            nonlocal written
            enter = int(run['enter'])
            last_seen = max(enter, int(run['last_seen']))
            dwell = int(max(0, last_seen - enter))
            sg = 1 if dwell > DWELL_CAP else 0
            if run['row_id'] is not None:
                updates.append((run['node'], enter, last_seen, dwell,
                                1 if is_last else 0, sg, run['row_id']))
            else:
                inserts.append((run['pin'], run['node'], enter, last_seen, dwell,
                                1 if is_last else 0, sg))
            written += 1

        def new_run(pin, node, ts):
            return {'row_id': None, 'pin': pin, 'node': node,
                    'enter': ts, 'last_seen': ts}

        # 按主键 id 顺序扫新增事件: 强制 PRIMARY 范围扫描, 绝不因 ORDER BY pin/ts
        # 走到 idx_pin_ts 上从最早事件一直扫到水位。同一 pin 的 id 顺序即时间顺序,
        # 用 states 在内存里逐车接续上一次的未闭合 run。
        sc = rd.cursor(pymysql.cursors.SSCursor)
        sc.execute(
            "SELECT pin_code, node_id, ts_epoch FROM vehicle_events FORCE INDEX(PRIMARY) "
            "WHERE id>%s AND id<=%s AND ev='place' AND pin_code<>'' AND node_id<>'' "
            "ORDER BY id", (last_id, upper))
        states = {}
        for pin, node, ts in sc:
            ts = int(ts)
            scanned += 1
            if verbose and scanned % 500000 == 0:
                print('  [runs] 增量扫描 %d 行, 已处理 %d 段' % (scanned, written))
            run = states.get(pin)
            if run is None:
                run = open_by_pin.get(pin)     # 跨扫描续上该车最后一段
                if run is None:
                    states[pin] = new_run(pin, node, ts)
                    continue
                states[pin] = run
            if node == run['node']:
                if ts > run['last_seen']:
                    run['last_seen'] = ts
            else:
                flush_run(run, False)          # 换站: 闭合旧段
                states[pin] = new_run(pin, node, ts)
        for run in states.values():
            flush_run(run, True)               # 各车末段保持未闭合

        wc = wr.cursor()
        if updates:
            wc.executemany(
                "UPDATE `%s` SET node_id=%%s,enter_ts=%%s,leave_ts=%%s,"
                "dwell_sec=%%s,is_last=%%s,suspect_gap=%%s WHERE id=%%s" % RUNS_TABLE,
                updates)
        if inserts:
            wc.executemany(
                "INSERT INTO `%s` "
                "(pin_code,node_id,enter_ts,leave_ts,dwell_sec,is_last,suspect_gap) "
                "VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s)" % RUNS_TABLE,
                inserts)
        _meta_set(wc, RUNS_HWM_KEY, upper)
        wr.commit()
        wc.execute("SELECT COUNT(*), MAX(enter_ts) FROM `%s`" % RUNS_TABLE)
        cnt, mx = wc.fetchone()
        if verbose:
            print('  [runs] 增量完成: 扫描 %d 事件, 更新/新增 %d 段, 水位 %d -> %d, '
                  '表内 %d 段, 最新进入 %s' % (
                      scanned, written, last_id, upper, int(cnt or 0),
                      _ts_obj(mx) if mx else '-'))
        return int(cnt or 0)
    finally:
        rd.close()
        wr.close()


# ---------- 节点解析 ----------
def _resolve_node(cur, node_id, area=None):
    """
    LLM/用户常只说短名(1D235RB)或片段(045RB), 库里是 Line_1_1A045RB。
    精确命中优先; 否则用 build_line 的节点表做短名/后缀补全, 不扫原始表。
    area(Line_1 / Line_2,Line_4)只作消歧前缀: 有区域时优先该线节点, 但绝不把查询放大成整区域。
    多线重名时取字典序首个, 并返回全部候选供前端说明。
    """
    node_id = str(node_id or '').strip().upper()
    if not node_id:
        raise ApiError('缺少 node_id 或 area', 400)
    cur.execute("SELECT 1 FROM vehicle_events WHERE node_id=%s LIMIT 1", (node_id,))
    if cur.fetchone():
        return node_id, []
    # 解析 "LINE_1_045RB" / "045RB": 前一段是区域消歧前缀, 后一段是设备片段。
    prefixes, token = [], node_id
    _p, _t = _split_upper(node_id)
    if _p:
        prefixes = [_p]
        token = _t
    elif area:
        prefixes = [p for p in re.split(r'[,，、;；\s]+', str(area).upper()) if p]
    if not all(ch.isalnum() or ch == '_' for ch in token):
        raise ApiError('节点号只允许字母/数字/下划线: %r' % node_id, 400)
    if prefixes and not all(all(ch.isalnum() or ch == '_' for ch in p) for p in prefixes):
        raise ApiError('area 只允许字母/数字/下划线: %r' % area, 400)

    exact, fuzzy = [], []
    for short, fulls in _known_nodes().items():
        for full in fulls:
            fu = full.upper()
            if prefixes and not any(fu.startswith(p + '_') for p in prefixes):
                continue
            if short.upper() == token:
                exact.append(fu)
            elif len(token) >= 4 and short.upper().endswith(token):
                fuzzy.append(fu)          # 045RB → 1A045RB / 1B045RB
    cands = sorted(set(exact or fuzzy))
    if cands:
        ph = ','.join(['%s'] * len(cands))
        cur.execute("SELECT DISTINCT node_id FROM vehicle_events WHERE node_id IN (%s)" % ph,
                    tuple(cands))
        present = sorted(r[0] for r in cur.fetchall())
        hits = present or cands
        return hits[0], hits
    # 节点表里没有: 兜底按后缀查库; 有区域前缀时走 idx_node_ts 前缀范围。
    suffix = '%' + token
    if prefixes:
        clause = '(' + ' OR '.join(["node_id LIKE %s"] * len(prefixes)) + ')'
        cur.execute(
            "SELECT DISTINCT node_id FROM vehicle_events "
            "WHERE " + clause + " AND node_id LIKE %s ORDER BY node_id LIMIT 20",
            tuple(p + '\\_%' for p in prefixes) + (suffix,))
    else:
        cur.execute(
            "SELECT DISTINCT node_id FROM vehicle_events "
            "WHERE node_id LIKE %s ORDER BY node_id LIMIT 20", (suffix,))
    hits = [r[0] for r in cur.fetchall()]
    if not hits:
        raise ApiError('找不到节点 %s' % node_id, 404, node_id=node_id)
    return hits[0], hits


def _area_clause(area):
    """area=Line_1 / Exit_1 / "Line_2,Line_4" → 节点前缀 LIKE 'Line_1\\_%' (可走 idx_node_ts). 非法字符拒绝."""
    raw = str(area or '').strip().upper()
    if not raw:
        return None, None
    parts = [p for p in re.split(r'[,，、;；\s]+', raw) if p]
    for a in parts:
        if not all(ch.isalnum() or ch == '_' for ch in a):
            raise ApiError('area 只允许字母/数字/下划线: %r' % area, 400)
    if not parts:
        return None, None
    if len(parts) == 1:
        return "node_id LIKE %s", (parts[0] + '\\_%',)
    return '(' + ' OR '.join(["node_id LIKE %s"] * len(parts)) + ')', \
        tuple(a + '\\_%' for a in parts)


# ---------- 1) 过车量 ----------
@app.route('/api/point_traffic', methods=['GET', 'OPTIONS'])
def point_traffic():
    if request.method == 'OPTIONS':
        return ('', 204)
    node_id = request.args.get('node_id')
    area = request.args.get('area') or request.args.get('targetArea')
    try:
        win = int(float(request.args.get('window') or DEFAULT_WINDOW))
    except ValueError:
        raise ApiError('window 必须是秒数', 400)
    win = max(10, min(win, MAX_WINDOW))
    start, end, s_iso, e_iso = _window(request.args.get('start_time'),
                                       request.args.get('end_time'),
                                       24, request.args.get('time_range'))

    with _connect() as conn:
        cur = conn.cursor()
        candidates = []
        scope_sql, scope_args = '', ()
        if node_id:
            resolved, candidates = _resolve_node(cur, node_id, area)
            scope_sql, scope_args = "node_id=%s", (resolved,)
            scope = resolved
        elif area:
            scope_sql, scope_args = _area_clause(area)
            scope = str(area).strip().upper()
        else:
            scope_sql, scope_args = '1=1', ()      # 不给范围 = 全厂
            scope = '全厂'

        # 去重: 绝不使用裸 COUNT(*). 内层按 (pin_code, 时间窗) 分组, 外层数"每车每窗一次".
        # 优先查派生 runs 表(每次进站一行), 缺表时回退 raw place 事件.
        if _runs_ready(cur):
            source = RUNS_TABLE
            sql = (
                "SELECT COUNT(*) AS pass_count, COUNT(DISTINCT pin_code) AS unique_vehicles "
                "FROM (SELECT pin_code, FLOOR(enter_ts / %%s) AS win "
                "      FROM `%s` WHERE enter_ts>=%%s AND enter_ts<=%%s AND %s "
                "      GROUP BY pin_code, win) t" % (RUNS_TABLE, scope_sql))
            cur.execute(sql, (win, start, end) + tuple(scope_args))
        else:
            source = 'vehicle_events(raw fallback)'
            sql = (
                "SELECT COUNT(*) AS pass_count, COUNT(DISTINCT pin_code) AS unique_vehicles "
                "FROM (SELECT pin_code, FLOOR(ts_epoch / %s) AS win "
                "      FROM vehicle_events "
                "      WHERE ev='place' AND pin_code<>'' AND ts_epoch>=%s AND ts_epoch<=%s "
                "        AND " + scope_sql + " "
                "      GROUP BY pin_code, win) t")
            cur.execute(sql, (win, start, end) + tuple(scope_args))
        pass_count, unique_vehicles = cur.fetchone()

        # 按小时聚合(前端折线图): 内层仍是 (pin, 时间窗) 去重, 窗口归属到窗起点所在小时,
        # 因此各小时之和 == pass_count. 空小时补 0, 图表连续.
        if source == RUNS_TABLE:
            hour_sql = (
                "SELECT FLOOR(win * %%s / 3600) AS hr, COUNT(*) AS c, "
                "       COUNT(DISTINCT pin_code) AS u "
                "FROM (SELECT pin_code, FLOOR(enter_ts / %%s) AS win "
                "      FROM `%s` WHERE enter_ts>=%%s AND enter_ts<=%%s AND %s "
                "      GROUP BY pin_code, win) t "
                "GROUP BY hr ORDER BY hr" % (RUNS_TABLE, scope_sql))
            cur.execute(hour_sql, (win, win, start, end) + tuple(scope_args))
        else:
            hour_sql = (
                "SELECT FLOOR(win * %s / 3600) AS hr, COUNT(*) AS c, "
                "       COUNT(DISTINCT pin_code) AS u "
                "FROM (SELECT pin_code, FLOOR(ts_epoch / %s) AS win "
                "      FROM vehicle_events "
                "      WHERE ev='place' AND pin_code<>'' AND ts_epoch>=%s AND ts_epoch<=%s "
                "        AND " + scope_sql + " "
                "      GROUP BY pin_code, win) t "
                "GROUP BY hr ORDER BY hr")
            cur.execute(hour_sql, (win, win, start, end) + tuple(scope_args))
        hm = {int(h): (int(c), int(u)) for h, c, u in cur.fetchall()}
        h0, h1 = start // 3600, end // 3600
        by_hour = []
        for h in range(h0, h1 + 1):
            c, u = hm.get(h, (0, 0))
            by_hour.append({'ts': h * 3600, 'iso': _ts_obj(h * 3600),
                            'pass_count': c, 'unique_vehicles': u})
        latest = _latest_ts(cur)

    return jsonify({
        'ok': True,
        'scope': scope,
        'node_id': scope if node_id else None,
        'node_candidates': candidates,
        'start_time': start, 'end_time': end,
        'start_iso': s_iso, 'end_iso': e_iso,
        'range_sec': end - start,
        'time_range': request.args.get('time_range'),
        'window_sec': win,
        'pass_count': int(pass_count or 0),
        'unique_vehicles': int(unique_vehicles or 0),
        'bucket_sec': 3600,
        'by_hour': by_hour,
        'source': source,
        'method': 'COUNT(*) over GROUP BY pin_code, time-window (no bare COUNT(*))',
        'latest_ts': latest,
        'latest_iso': _ts_obj(latest) if latest else None,
    })


# ---------- 2) 堵点 ----------
@app.route('/api/bottlenecks', methods=['GET', 'OPTIONS'])
def bottlenecks():
    if request.method == 'OPTIONS':
        return ('', 204)
    hours = request.args.get('hours') or DEFAULT_BOTTLENECK_HOURS
    try:
        hours = float(hours)
    except ValueError:
        raise ApiError('hours 必须是数字', 400)
    try:
        limit = max(1, min(int(request.args.get('limit') or 5), 50))
        min_visits = max(1, int(request.args.get('min_visits') or 3))
        win = max(10, min(int(request.args.get('window') or DEFAULT_WINDOW), MAX_WINDOW))
    except ValueError:
        raise ApiError('limit/min_visits/window 必须是整数', 400)
    start, end, s_iso, e_iso = _window(request.args.get('start_time'),
                                       request.args.get('end_time'),
                                       hours, request.args.get('time_range'))
    # node_id 可选: 定向查某一个节点(单节点堵点详情), 不参与 TOP 排序.
    # area 可选: 只统计某条线的节点(used by 过车量趋势判断拥堵风险).
    node_id_arg = (request.args.get('node_id') or '').strip()
    area_arg = (request.args.get('area') or request.args.get('targetArea') or '').strip()

    # 优先查派生 runs 表: 每段已经是去重后的停留记录, 直接 AVG(dwell_sec).
    # 缺表时回退 raw: 先按 (pin,node,5min窗) 取 MIN(ts), 再压缩连续同节点, 最后一段排除.
    # /*SCOPE*/ 处按需注入 node_id 过滤(避免为不存在的参数拼 SQL).
    RAW_SQL = (
        "WITH d AS ("
        "  SELECT pin_code, node_id, FLOOR(ts_epoch/%s) AS w,"
        "         MIN(ts_epoch) AS ts0, MAX(ts_epoch) AS ts1"
        "  FROM vehicle_events"
        "  WHERE ev='place' AND pin_code<>'' AND node_id<>''"
        "    AND ts_epoch>=%s AND ts_epoch<=%s /*SCOPE*/"
        "  GROUP BY pin_code, node_id, w"
        "), r1 AS ("
        "  SELECT pin_code, node_id, ts0, ts1,"
        "         LAG(node_id) OVER (PARTITION BY pin_code ORDER BY ts0) AS prev_node"
        "  FROM d"
        "), g AS ("
        "  SELECT pin_code, node_id, ts0, ts1,"
        "         SUM(CASE WHEN prev_node IS NULL OR prev_node<>node_id THEN 1 ELSE 0 END)"
        "           OVER (PARTITION BY pin_code ORDER BY ts0 ROWS UNBOUNDED PRECEDING) AS grp"
        "  FROM r1"
        "), runs AS ("
        "  SELECT pin_code, node_id, MIN(ts0) AS t0, MAX(ts1) AS t1"
        "  FROM g GROUP BY pin_code, node_id, grp"
        ") "
        "SELECT node_id, COUNT(*) AS visits, "
        "       ROUND(AVG(t1-t0), 1) AS avg_dwell_sec, "
        "       ROUND(MIN(t1-t0), 1) AS min_dwell_sec, "
        "       ROUND(MAX(t1-t0), 1) AS max_dwell_sec "
        "FROM runs "
        "WHERE t1>t0 AND t1-t0>=%s AND t1-t0<=%s "
        "GROUP BY node_id HAVING COUNT(*)>=%s "
        "ORDER BY avg_dwell_sec DESC, visits DESC "
        "LIMIT %s")
    with _connect() as conn:
        cur = conn.cursor()
        resolved_node, candidates = None, []
        scope_sql, scope_fmt, scope_args = '', '', ()
        if node_id_arg:
            resolved_node, candidates = _resolve_node(cur, node_id_arg, area_arg)
            scope_sql = ' AND node_id=%s'
            scope_fmt = ' AND node_id=%%s'     # 整条 SQL 统一 %-格式化一次, 此处用 %%s
            scope_args = (resolved_node,)
        elif area_arg:
            clause, cargs = _area_clause(area_arg)
            scope_sql = ' AND ' + clause
            scope_fmt = ' AND ' + clause.replace('%s', '%%s')
            scope_args = cargs
        if _runs_ready(cur):
            source = RUNS_TABLE
            sql = ("SELECT node_id, COUNT(*) AS visits, "
                   "       ROUND(AVG(dwell_sec), 1) AS avg_dwell_sec, "
                   "       ROUND(MIN(dwell_sec), 1) AS min_dwell_sec, "
                   "       ROUND(MAX(dwell_sec), 1) AS max_dwell_sec "
                   "FROM `%s` "
                   "WHERE enter_ts>=%%s AND enter_ts<=%%s" + scope_fmt +
                   "  AND is_last=0 AND dwell_sec>=%%s AND dwell_sec<=%%s "
                   "GROUP BY node_id HAVING COUNT(*)>=%%s "
                   "ORDER BY avg_dwell_sec DESC, visits DESC LIMIT %%s")
            cur.execute(sql % (RUNS_TABLE,),
                        (start, end) + scope_args + (MIN_DWELL_SEC, MAX_DWELL_SEC,
                                                     min_visits, limit))
        else:
            source = 'vehicle_events(raw fallback)'
            cur.execute(RAW_SQL.replace('/*SCOPE*/', scope_sql),
                        (win, start, end) + scope_args +
                        (MIN_DWELL_SEC, MAX_DWELL_SEC, min_visits, limit))
        rows = cur.fetchall()
        if source == RUNS_TABLE:
            car_sql = ("SELECT COUNT(DISTINCT pin_code) FROM `%s` "
                       "WHERE enter_ts>=%%s AND enter_ts<=%%s" + scope_fmt)
            cur.execute(car_sql % (RUNS_TABLE,), (start, end) + scope_args)
        else:
            cur.execute(
                "SELECT COUNT(DISTINCT pin_code) FROM ("
                "  SELECT pin_code FROM vehicle_events"
                "  WHERE ev='place' AND pin_code<>'' AND node_id<>''"
                "    AND ts_epoch>=%s AND ts_epoch<=%s" + scope_sql +
                "  GROUP BY pin_code, node_id, FLOOR(ts_epoch/%s)) x",
                (start, end) + scope_args + (win,))
        cars = int(cur.fetchone()[0] or 0)
        # 节点定向详情: 另外给该节点的过车台次/去重车数(与 /api/point_traffic 同口径).
        pass_count = unique_vehicles = None
        if resolved_node:
            if source == RUNS_TABLE:
                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT pin_code) FROM ("
                    "  SELECT pin_code, FLOOR(enter_ts/%s) AS w"
                    "  FROM `" + RUNS_TABLE + "`"
                    "  WHERE enter_ts>=%s AND enter_ts<=%s AND node_id=%s"
                    "  GROUP BY pin_code, w) x",
                    (win, start, end, resolved_node))
            else:
                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT pin_code) FROM ("
                    "  SELECT pin_code, FLOOR(ts_epoch/%s) AS w"
                    "  FROM vehicle_events"
                    "  WHERE ev='place' AND pin_code<>'' AND node_id=%s"
                    "    AND ts_epoch>=%s AND ts_epoch<=%s"
                    "  GROUP BY pin_code, w) x",
                    (win, resolved_node, start, end))
            pass_count, unique_vehicles = (int(x or 0) for x in cur.fetchone())
        latest = _latest_ts(cur)

    top = []
    try:
        from traj_query import label
    except Exception:
        label = lambda n: n  # noqa: E731
    for node, visits, avg, mn, mx in rows:
        try:
            nice = label(node)
        except Exception:
            nice = node
        top.append({
            'node_id': node,
            'label': nice,
            'visits': int(visits),
            'avg_dwell_sec': float(avg or 0),
            'min_dwell_sec': float(mn or 0),
            'max_dwell_sec': float(mx or 0),
        })

    return jsonify({
        'ok': True,
        'start_time': start, 'end_time': end,
        'start_iso': s_iso, 'end_iso': e_iso,
        'range_sec': end - start,
        'time_range': request.args.get('time_range'),
        'hours': hours,
        'window_sec': win,
        'cars': cars,
        'min_dwell_sec': MIN_DWELL_SEC,
        'max_dwell_sec': MAX_DWELL_SEC,
        'min_visits': min_visits,
        'node_id': resolved_node,
        'node_candidates': candidates,
        'pass_count': pass_count,
        'unique_vehicles': unique_vehicles,
        'top': top,
        'source': source,
        'latest_ts': latest,
        'latest_iso': _ts_obj(latest) if latest else None,
        'method': ('derived vehicle_runs: dwell = MAX(ts) - MIN(ts) within a pin+node run; '
                   'runs <2s or >15min excluded'
                   if source == RUNS_TABLE else
                   'raw place runs: dwell = MAX(ts) - MIN(ts) within a pin+node run; '
                   'runs <2s or >15min excluded'),
    })


# ---------- 3) 单车历史 ----------
_CAR_COLS = "pin_code, body_no, sled_no, color, vehicle_type, platform, ts_epoch"


def _pick_car(cur, query, limit=50):
    """PIN/雪橇/车身号匹配, 取最近活动的一台车. 精确 PIN→精确雪橇→包含匹配, 逐级降级."""
    q = str(query or '').strip()
    if not q:
        raise ApiError('缺少 query (PIN / 雪橇号 / 车身号)', 400)
    rows = []
    # 1) 精确 PIN (idx_pin_ts) — 14 位数字最常见
    cur.execute("SELECT " + _CAR_COLS + " FROM vehicle_events "
                "WHERE pin_code=%s ORDER BY id DESC LIMIT 1", (q,))
    row = cur.fetchone()
    if row:
        rows = [row]
    else:
        # 2) 精确雪橇号 (idx_sled)
        cur.execute("SELECT " + _CAR_COLS + " FROM vehicle_events "
                    "WHERE sled_no=%s ORDER BY id DESC LIMIT 1", (q,))
        row = cur.fetchone()
        if row:
            rows = [row]
        else:
            # 3) 包含匹配(慢路径): 只取最近若干行, 交给 Python 去重
            like = '%' + q + '%'
            cur.execute("SELECT " + _CAR_COLS + " FROM vehicle_events "
                        "WHERE pin_code LIKE %s OR body_no LIKE %s OR sled_no LIKE %s "
                        "ORDER BY id DESC LIMIT %s", (like, like, like, limit))
            rows = cur.fetchall()

    cars, candidates = {}, []
    for r in rows:
        pin = r[0]
        if not pin:
            continue
        if pin not in cars:
            cars[pin] = {
                'pin_code': pin, 'body_no': r[1] or '', 'sled_no': r[2] or '',
                'color': r[3] or '', 'vehicle_type': r[4] or '',
                'platform': r[5] or '', 'last_ts': int(r[6]),
            }
        candidates.append(pin)
    if not cars:
        return None, []
    pin = max(cars, key=lambda p: cars[p]['last_ts'])
    uniq = list(dict.fromkeys(candidates))
    return cars[pin], uniq[:10]


def _clean_history(raw):
    """
    raw: [(node, enter, leave, dwell, is_last)]. 剔除 <MIN_RUN_SEC 的交接抖动段:
    - 抖动段夹在两段同节点之间 → 合并成一段;
    - 抖动段夹在不同节点之间 → 把抖动时间并入上一段(上一段离开=下一段进入).
    """
    clean = []
    for node, enter, leave, dwell, is_last in raw:
        enter, leave, dwell = int(enter), int(leave), int(dwell)
        if not is_last and dwell < MIN_RUN_SEC:
            continue
        if clean and clean[-1]['node_id'] == node:
            p = clean[-1]
            p['leave_ts'] = leave
            p['dwell_sec'] = int(max(0, leave - p['enter_ts']))
            p['is_last'] = bool(is_last)
            continue
        if clean and clean[-1]['leave_ts'] is not None:
            p = clean[-1]
            p['leave_ts'] = enter
            p['dwell_sec'] = int(max(0, enter - p['enter_ts']))
        clean.append({'node_id': node, 'enter_ts': enter, 'leave_ts': leave,
                      'dwell_sec': dwell, 'is_last': bool(is_last)})
    return clean


def _history_segments(cur, pin, now=None):
    """单车轨迹: 直接扫原始 place 事件, 不再依赖已闭合的 vehicle_runs.

    - 按 ts 升序取该 PIN 的全部 place, 连续同节点事件压成一组, 保留最早/最晚时间;
    - 每组停留 = 最晚 place - 最早 place(EMOS 2s 重复上报自然折叠);
    - 末节点若最后一次 place 在 TRACK_ACTIVE_SEC 内, 视为车仍停在末端,
      停留改用 now - 最后一次 place, 供前端说“已停留”。
    """
    cur.execute(
        "SELECT node_id, ts_epoch FROM vehicle_events "
        "WHERE pin_code=%s AND ev='place' AND node_id<>'' "
        "ORDER BY ts_epoch ASC, id ASC", (pin,))
    groups = []
    for node, ts in cur.fetchall():
        ts = int(ts)
        if groups and groups[-1]['node_id'] == node:
            groups[-1]['last_ts'] = ts
        else:
            groups.append({'node_id': node, 'enter_ts': ts, 'last_ts': ts})
    if not groups:
        return []
    now = int(now if now is not None else time.time())
    out = []
    for i, g in enumerate(groups):
        enter, last_seen = int(g['enter_ts']), int(g['last_ts'])
        is_last = (i == len(groups) - 1)
        ongoing = bool(is_last and now - last_seen <= TRACK_ACTIVE_SEC)
        if ongoing:
            # 当前仍在末节点: 已停留 = now - 该节点最早 place.
            # (EMOS 1~2s 重复 place 会不断刷新 last_ts, 用 last 会恒等于 0~2s)
            dwell = int(max(0, now - enter))
        else:
            dwell = int(max(0, last_seen - enter))   # 历史段: 最早~最晚 place
        out.append({
            'node_id': g['node_id'],
            'enter_ts': enter,
            'last_place_ts': last_seen,
            'leave_ts': None if is_last else int(groups[i + 1]['enter_ts']),
            'dwell_sec': dwell,
            'is_last': is_last,
            'ongoing': ongoing,
        })
    return out


@app.route('/api/vehicle_history', methods=['GET', 'OPTIONS'])
def vehicle_history():
    if request.method == 'OPTIONS':
        return ('', 204)
    query = request.args.get('query') or request.args.get('targetKeyword')
    try:
        limit = max(1, min(int(request.args.get('limit') or HISTORY_LIMIT), 200))
    except ValueError:
        raise ApiError('limit 必须是整数', 400)

    try:
        from traj_query import label
    except Exception:
        label = lambda n: n  # noqa: E731

    with _connect() as conn:
        cur = conn.cursor()
        car, candidates = _pick_car(cur, query, limit=50)
        if not car:
            return jsonify({'ok': True, 'query': query, 'found': False,
                            'segments': [], 'nodes': [], 'current': None,
                            'total_segments': 0, 'total_nodes': 0})
        now = int(time.time())
        segs = _history_segments(cur, car['pin_code'], now=now)
        latest = _latest_ts(cur)

    out = []
    for i, s in enumerate(segs[:limit]):
        out.append({
            'seq': i + 1,
            'node_id': s['node_id'],
            'label': label(s['node_id']),
            'enter_ts': s['enter_ts'],
            'enter_iso': _ts_obj(s['enter_ts']),
            'last_place_ts': s['last_place_ts'],
            'last_place_iso': _ts_obj(s['last_place_ts']),
            'leave_ts': s['leave_ts'],
            'leave_iso': _ts_obj(s['leave_ts']) if s['leave_ts'] is not None else None,
            'dwell_sec': s['dwell_sec'],
            'ongoing': s['ongoing'],
            'status': 'active' if s['ongoing'] else ('last_recorded' if s['is_last'] else 'past'),
            'suspect_gap': s['dwell_sec'] > DWELL_CAP,
        })

    last = segs[-1] if segs else None
    current = None
    if last:
        current = {
            'node_id': last['node_id'],
            'label': label(last['node_id']),
            'enter_ts': last['enter_ts'],
            'enter_iso': _ts_obj(last['enter_ts']),
            'last_place_ts': last['last_place_ts'],
            'last_place_iso': _ts_obj(last['last_place_ts']),
            'dwell_sec': last['dwell_sec'],
            'ongoing': last['ongoing'],
        }
    nodes = [s['node_id'] for s in segs]
    return jsonify({
        'ok': True,
        'query': query,
        'found': True,
        'car': car,
        'candidates': candidates,
        'segments': out,
        'nodes': nodes,
        'total_nodes': len(nodes),
        'current': current,
        'total_segments': len(segs),
        'returned_segments': len(out),
        'truncated': len(segs) > len(out),
        'now_ts': now,
        'latest_ts': latest,
        'latest_iso': _ts_obj(latest) if latest else None,
    })


# ---------- 健康检查 ----------
@app.route('/api/health', methods=['GET', 'OPTIONS'])
def health():
    if request.method == 'OPTIONS':
        return ('', 204)
    exact = request.args.get('exact') in ('1', 'true', 'yes')
    with _connect() as conn:
        cur = conn.cursor()
        # 精确 COUNT(*) 在 600 万行上会 20s+ 超时; 默认用 information_schema 估算 + 索引 MIN/MAX.
        cur.execute("SELECT table_rows FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_name='vehicle_events'")
        r = cur.fetchone()
        rows = int(r[0] or 0) if r else 0
        cur.execute("SELECT MIN(ts_epoch), MAX(ts_epoch) FROM vehicle_events")
        mn, mx = cur.fetchone()
        body = {
            'ok': True,
            'rows_approx': rows,
            'min_ts': int(mn) if mn else None,
            'max_ts': int(mx) if mx else None,
            'min_iso': _ts_obj(mn) if mn else None,
            'max_iso': _ts_obj(mx) if mx else None,
            'server_time': _ts_obj(time.time()),
        }
        if exact:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT pin_code), COUNT(DISTINCT node_id) "
                        "FROM vehicle_events")
            n, pins, nodes = cur.fetchone()
            body.update(rows=int(n), pins=int(pins or 0), nodes=int(nodes or 0))
    return jsonify(body)


def main():
    ap = argparse.ArgumentParser(description='conveyor_traj 轨迹统计只读 API')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--build-runs', action='store_true',
                    help='增量更新派生表 vehicle_runs 后退出(只扫新事件; 首次自动全量)')
    ap.add_argument('--build-runs-full', action='store_true',
                    help='强制全量重建 vehicle_runs 后退出(数分钟)')
    a = ap.parse_args()
    if a.build_runs or a.build_runs_full:
        if a.build_runs_full:
            print('全量重建 %s ...' % RUNS_TABLE)
            print('完成: %d 段' % build_runs(full=True))
        else:
            print('增量更新 %s ...' % RUNS_TABLE)
            print('完成: %d 段' % build_runs())
        return
    print('traj_api: http://%s:%d/api/health (DB %s)' % (
        a.host, a.port, load_db_config().get('database')))
    app.run(host=a.host, port=a.port, threaded=True, debug=a.debug, use_reloader=False)


if __name__ == '__main__':
    main()