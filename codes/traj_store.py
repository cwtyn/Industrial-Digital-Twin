# -*- coding: utf-8 -*-
"""
轨迹落盘 MySQL 持久化层 — 把 consume() 的 place/clear/skid/target 事件写入库。

设计要点:
- 独立于实时热循环: 主进程只往 queue put, 后台写线程攒批落库。DB 挂了绝不阻塞/中断 feed,
  异常捕获打印 warn, 缓冲保留重试。
- 表 `vehicle_events`(conveyor_traj 库): 每车 place/clear 一条轨迹事件, skid/target 回填
  其 place 行的字段(同一节点当前占位车). pin_code(body 前14位)唯一识别车.
- 连接配置优先读 config/db.json(host/port/user/password/database), 缺省走本机默认值；
  仓库内不内置任何真实凭据，密码请放 config/db.json(已 gitignore)或环境变量 CONVEYOR_DB_PASSWORD。

用法(run_runtime 接线):
    writer = TrajWriter(); writer.start()          # 起后台线程(ensure_schema 已建库表)
    writer.ensure_schema()                          # 幂等建库建表
    consume(src, state, on_event=writer.record)     # 每事件回调 → writer.record(kind,node,payload)
    writer.close()                                  # 结束: 停线程 + flush 缓冲

查询见 traj_query.py(命令行回溯).
"""
import json
import os
import queue
import threading
import time

try:
    import pymysql
except Exception:
    pymysql = None

DB_CONFIG_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'config', 'db.json'))

# 缺省连接. config/db.json 存在时以它为准(权威单源); 密码只从环境变量取, 不写死在代码里.
_DEFAULTS = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'root',
    'password': os.environ.get('CONVEYOR_DB_PASSWORD', ''),
    'database': 'conveyor_traj',
}

# 建表 DDL(IEEE utf8mb4; id 自增, pin_code 索引供回溯)
_DDL = """
CREATE TABLE IF NOT EXISTS vehicle_events (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  ts_epoch     INT UNSIGNED NOT NULL,
  body_no      VARCHAR(32)  NOT NULL,
  pin_code     VARCHAR(14)  NOT NULL,
  sled_no      VARCHAR(32)  NOT NULL DEFAULT '',
  color        VARCHAR(8)   NOT NULL DEFAULT '',
  vehicle_type VARCHAR(8)   NOT NULL DEFAULT '',
  platform     VARCHAR(8)   NOT NULL DEFAULT '',
  target       VARCHAR(16)  NOT NULL DEFAULT '',
  node_id      VARCHAR(64)  NOT NULL,
  ev           VARCHAR(8)   NOT NULL,
  KEY idx_pin    (pin_code, node_id),
  KEY idx_pin_ts (pin_code, ts_epoch),
  KEY idx_ts     (ts_epoch),
  KEY idx_node_ts(node_id, ts_epoch, ev, pin_code),      -- 覆盖索引: 节点统计免回表
  KEY idx_ev_ts_cover(ev, ts_epoch, pin_code, node_id),  -- 覆盖索引: 堵点/过车量全表扫描
  KEY idx_node_ev(node_id, ev),                          -- skid/target 回填: 取该节点最新 place(id)
  KEY idx_sled   (sled_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

_FLUSH_CAP = 200      # 缓冲满 200 条强制 flush
_FLUSH_INTERVAL = 0.5  # 后台线程每 0.5s flush 一次

_NODB = object()      # connect(db=_NODB) = 连"不带库"(仅建库用)


def load_db_config(path=None, defaults=None):
    """读 config/db.json → dict; 缺文件/缺键时用默认值。返回 {'host','port','user','password','database','_path'}."""
    cfg = dict(defaults or _DEFAULTS)
    p = path or DB_CONFIG_PATH
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                d = json.load(f)
            for k in ('host', 'port', 'user', 'password', 'database'):
                if d.get(k):
                    cfg[k] = d[k]
        except (OSError, ValueError) as e:
            print(f"  [warn] 读 DB 配置失败 {p}: {e}, 用默认连接")
    cfg['_path'] = p
    return cfg


class TrajWriter:
    """后台上写线程. record() 从任意线程安全入队, 内部攒批落库."""

    def __init__(self, config=None):
        self.cfg = load_db_config() if config is None else dict(config)
        self.q = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._conn = None
        self._n_written = 0        # 成功落库条数
        self._n_kept = 0           # 当前缓冲条数

    # ---- 建库建表(幂等) ----
    def connect(self, db=None):
        """db=None(省略)→连配置库; db=_NODB→连"不带库"(仅建库用); db='xxx'→连指定库."""
        if pymysql is None:
            raise RuntimeError("未安装 pymysql (pip install --user pymysql)")
        kwargs = dict(host=self.cfg.get('host'), port=int(self.cfg.get('port', 3306)),
                      user=self.cfg.get('user'), password=self.cfg.get('password'),
                      charset='utf8mb4', autocommit=True, connect_timeout=5)
        target = None if db is _NODB else (db or self.cfg.get('database'))
        if target:
            kwargs['database'] = target
        return pymysql.connect(**kwargs)

    def ensure_schema(self):
        """CREATE DATABASE + 建 vehicle_events 表(幂等). 失败仅打 warn, 不 raise(不卡启动)."""
        try:
            with self.connect(db=_NODB) as c:
                cur = c.cursor()
                cur.execute(
                    "CREATE DATABASE IF NOT EXISTS `%s` "
                    "DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                    % self.cfg.get('database'))
            with self.connect() as c:
                cur = c.cursor()
                cur.execute(_DDL)
            print(f"  [db] 就绪: {self.cfg.get('user')}@{self.cfg.get('host')}:{self.cfg.get('port')}"
                  f"/{self.cfg.get('database')} (vehicle_events)")
        except Exception as e:
            print(f"  [warn] 建库表失败: {e}")

    def _get_conn(self):
        if self._conn is None:
            self._conn = self.connect()
        return self._conn

    def _drop_conn(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    # ---- 入队(主线程调用, 安全) ----
    def record(self, kind, node_id, payload=None):
        """事件回调(来自 consume on_event). kind ∈ place/clear/skid/target."""
        ts = int(time.time())
        if kind == 'place':
            v = payload if isinstance(payload, dict) else {}
            self.q.put(('insert', {
                'ts_epoch': ts,
                'body_no': str(v.get('body_no') or ''),
                'pin_code': str(v.get('pin_code') or (v.get('body_no') or '')[:14]),
                'sled_no': str(v.get('sled_no') or ''),
                'color': str(v.get('color') or ''),
                'vehicle_type': str(v.get('vehicle_type') or ''),
                'platform': str(v.get('platform') or ''),
                'target': str(v.get('target') or v.get('extra', {}).get('target') or '') or '',
                'node_id': str(node_id or ''),
                'ev': 'place',
            }))
        elif kind == 'clear':
            self.q.put(('insert', {
                'ts_epoch': ts, 'body_no': '', 'pin_code': '', 'sled_no': '', 'color': '',
                'vehicle_type': '', 'platform': '', 'target': '', 'node_id': str(node_id or ''), 'ev': 'clear',
            }))
        elif kind == 'skid':                       # 回填该节点当前占位车的雪橇
            self.q.put(('update', ('sled_no', str(payload or ''), str(node_id or ''))))
        elif kind == 'target':                     # 回填该节点当前占位车的目标
            self.q.put(('update', ('target', str(payload or ''), str(node_id or ''))))

    def flush(self):
        """出队并批量落库(线程内部)."""
        buf = []
        while True:
            try:
                buf.append(self.q.get_nowait())
            except queue.Empty:
                break
        if not buf:
            return
        inserts = [r for kind, r in buf if kind == 'insert']
        updates = [r for kind, r in buf if kind == 'update']
        try:
            conn = self._get_conn()
            cur = conn.cursor()
            if inserts:
                sql = ("INSERT INTO vehicle_events "
                       "(ts_epoch,body_no,pin_code,sled_no,color,vehicle_type,platform,target,node_id,ev) "
                       "VALUES (%(ts_epoch)s,%(body_no)s,%(pin_code)s,%(sled_no)s,%(color)s,"
                       "%(vehicle_type)s,%(platform)s,%(target)s,%(node_id)s,%(ev)s)")
                cur.executemany(sql, inserts)
            for col, val, node in updates:
                # FORCE idx_node_ev: 默认优化器可能走 ev 索引全表扫 + 锁上万行(实测把写线程卡死);
                # (node_id, ev) 二级索引尾部隐含主键 id, 可倒序直接取最新一条 place。
                cur.execute(
                    "UPDATE vehicle_events FORCE INDEX(idx_node_ev) SET %s=%%s "
                    "WHERE node_id=%%s AND ev='place' ORDER BY id DESC LIMIT 1" % col, (val, node))
            self._n_written += len(buf)
        except Exception as e:
            # DB 异常: 保留缓冲, 断开连接下次重连重试; 绝不影响热循环
            print(f"  [warn] 轨迹落库失败({len(buf)}条暂存): {e}")
            self._drop_conn()
            for item in buf:
                self.q.put(item)
        else:
            self._n_kept = self.q.qsize()

    # ---- 生命周期 ----
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        n = 0
        while not self._stop.is_set():
            self.flush()
            n += 1
            if n % 60 == 0:                    # 每 ~30s 报一次落库/积压, 供现场看管线健康
                print(f"  [traj] written={self._n_written} queued={self.q.qsize()}", flush=True)
            self._stop.wait(_FLUSH_INTERVAL)
        self.flush()                     # 退出前最后一次清空

    def close(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=3)
        self._drop_conn()
        self._thread = None

    def stats(self):
        return {'written': self._n_written, 'buffered': self.q.qsize()}


def make_default_path(cfg):
    return cfg.get('database')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--init', action='store_true', help='只建库建表后退出')
    ap.add_argument('--count', action='store_true', help='统计 vehicle_events 行数')
    a = ap.parse_args()
    w = TrajWriter()
    if a.init:
        w.ensure_schema()
    if a.count:
        try:
            with w.connect() as c:
                cur = c.cursor()
                cur.execute("SELECT COUNT(*) FROM vehicle_events")
                print("vehicle_events 行数:", cur.fetchone()[0])
        except Exception as e:
            print("查询失败:", e)
    elif not a.init:
        ap.print_help()