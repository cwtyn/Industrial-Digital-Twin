# -*- coding: utf-8 -*-
"""
vehicle_events 轨迹完整性体检（只读，不写库）

用法:
    python -X utf8 codes/traj_health_check.py
    python -X utf8 codes/traj_health_check.py --hours 24 --md docs/traj_health.md

口径:
- 车辆 = pin_code；place 事件才算轨迹；同一节点重复 place 压成链段。
- 今天首次出现 = MIN(ts_epoch) >= 今天 00:00。
- 数据恢复点 = 今天最早 place；恢复点 5 分钟内首见的车，其首节点大概率不是真实进厂起点。
- 节点数 = COUNT(DISTINCT node_id)；链段数 = 连续同节点压缩后的段数。
- “漏数据”是启发式判断：末条 place 已超过 15 分钟、跨度又长、且 place 最大空档 >= 10 分钟。
"""
import argparse
import json
import os
import time
from datetime import datetime

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
DB_CFG = os.path.join(HERE, '..', 'config', 'db.json')
RECENT_SEC = 900          # 末条 place 15 分钟内 → 仍在途/刚进站
GAP_SUSPECT_SEC = 600     # 相邻 place 空档 >=10min → 疑似漏数据
RESUME_WINDOW_SEC = 300   # 落库恢复点 5 分钟内的首见，视作“恢复时已在厂内/中段”


def iso(ts):
    return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S') if ts else '-'


def fmt_dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return '%d秒' % sec
    if sec < 3600:
        return '%d分%02d秒' % (sec // 60, sec % 60)
    return '%d小时%02d分' % (sec // 3600, (sec % 3600) // 60)


def connect():
    cfg = json.load(open(DB_CFG, encoding='utf-8'))
    cfg = {k.lower(): v for k, v in cfg.items()}
    return pymysql.connect(host=cfg.get('host', '127.0.0.1'), port=int(cfg.get('port', 3306)),
                           user=cfg['user'], password=cfg['password'], database=cfg['database'],
                           charset='utf8mb4', autocommit=True,
                           cursorclass=pymysql.cursors.DictCursor)


def chain_stats(cur, pin):
    """该 PIN 的完整 place 链: 连续同节点压缩、相邻 place 空档。"""
    cur.execute("""
        SELECT node_id, ts_epoch FROM vehicle_events FORCE INDEX (idx_pin_ts)
        WHERE pin_code=%s AND ev='place' AND node_id<>''
        ORDER BY ts_epoch ASC, id ASC
    """, (pin,))
    rows = cur.fetchall()
    if not rows:
        return None
    groups, gaps = [], []
    prev_ts = None
    for r in rows:
        node, ts = r['node_id'], int(r['ts_epoch'])
        if prev_ts is not None:
            gaps.append((ts - prev_ts, prev_ts, ts))
        if groups and groups[-1]['node_id'] == node:
            groups[-1]['last_ts'] = ts
        else:
            groups.append({'node_id': node, 'enter_ts': ts, 'last_ts': ts})
        prev_ts = ts
    max_gap = max(gaps, key=lambda x: x[0]) if gaps else (0, None, None)
    return {'rows': len(rows), 'groups': groups, 'gaps': gaps, 'max_gap': max_gap,
            'first': int(rows[0]['ts_epoch']), 'last': int(rows[-1]['ts_epoch'])}


def classify(car, st, now, resume_ts):
    """返回 (归类键, 细节)；归类键用于聚合，细节保留时长等证据。"""
    first, last = st['first'], st['last']
    span, max_gap, since_last = last - first, st['max_gap'][0], now - last
    at_resume = first <= resume_ts + RESUME_WINDOW_SEC
    if at_resume:
        if since_last <= RECENT_SEC:
            return ('恢复点回填快照', '首条在恢复点 5 分钟内，不能作为真实进厂起点')
        return ('恢复点回填期首见后静默', '末条距今 %s，需对照 EMOS 日志/现场' % fmt_dur(since_last))
    if since_last <= RECENT_SEC:
        return ('刚刚进站/仍在途', '末条 place 距今 %s' % fmt_dur(since_last))
    if max_gap >= GAP_SUSPECT_SEC and span >= RECENT_SEC:
        return ('中间漏数据', '跨度 %s，最大 place 空档 %s' % (fmt_dur(span), fmt_dur(max_gap)))
    if span <= RECENT_SEC:
        return ('短轨迹已结束', '首末仅 %s，需核查是否只覆盖局部' % fmt_dur(span))
    return ('低节点+长跨度', '末条距今 %s，疑似漏数据/离开订阅区' % fmt_dur(since_last))


def markdown_report(active, entries, resume_cars, short_rows, top5, rate, path):
    now = rate['now']
    lines = []
    lines.append('# vehicle_events 轨迹完整性体检')
    lines.append('')
    lines.append('生成时间：%s  ' % iso(now))
    lines.append('数据库：`vehicle_events`（只读）  ')
    lines.append('今天最早 place（落库恢复点）：%s' % iso(rate['resume_ts']))
    lines.append('')
    lines.append('## 摘要')
    lines.append('')
    lines.append('- 今天有 place 的车辆：**%d 台**；place 事件行：**%d 条**。' %
                 (len(active), sum(int(x['place_rows']) for x in active)))
    lines.append('- 今天首次出现（DB 中第一条 place 在今天）：**%d 台**。' % len(entries))
    lines.append('- 在这些车中，%d 台在恢复点 5 分钟内首见，属于“恢复时已在厂内/中段”，'
                 '不能当作今天真实进厂轨迹。' % len(resume_cars))
    lines.append('- 节点数 < 5 的车辆：**%d 台**。' % len(short_rows))
    lines.append('- 实际写入速率：最近 300 秒平均 **%.1f 行/秒**；'
                 '按表行数增量采样 **%.1f 行/秒**（place/clear 均为 INSERT；skid/target 另走 UPDATE）。'
                 % (rate['event_rate_300'], rate['insert_rate']))
    startup = rate.get('startup') or {}
    p_first15 = sum((startup.get(m) or {}).get('place', 0) for m in range(15))
    p_next15 = sum((startup.get(m) or {}).get('place', 0) for m in range(15, 30))
    lines.append('- 启动回填：恢复点后 0–15 分钟 place **%d 条（%.2f 行/秒）**；'
                 '15–30 分钟 **%d 条（%.2f 行/秒）**；之后稳态 **%.1f 行/秒**。'
                 '恢复点后 5 分钟内首见的短轨迹，主因是 EMOS 初始回填不完整，不能直接判为中间漏数据。'
                 % (p_first15, p_first15 / 900.0, p_next15, p_next15 / 900.0,
                    rate['event_rate_300']))
    lines.append('')
    lines.append('## 节点数 Top 5（最完整轨迹）')
    lines.append('')
    lines.append('| # | PIN | 雪橇 | 车型 | 唯一节点 | place 行 | 首条 | 末条 | 跨度 |')
    lines.append('|---|-----|------|------|----------|----------|------|------|------|')
    for i, c in enumerate(top5, 1):
        lines.append('| %d | %s | %s | %s | %d | %d | %s | %s | %s |' %
                     (i, c['pin_code'], c['sled_no'], c['vehicle_type'], c['node_cnt'],
                      c['place_rows'], iso(c['first_all']), iso(c['last_all']),
                      fmt_dur(c['last_all'] - c['first_all'])))
    lines.append('')
    lines.append('## 节点数 < 5 的车辆')
    lines.append('')
    by_cnt = {}
    by_verdict = {}
    for r in short_rows:
        by_cnt[r['node_cnt']] = by_cnt.get(r['node_cnt'], 0) + 1
        by_verdict[r['verdict_key']] = by_verdict.get(r['verdict_key'], 0) + 1
    lines.append('节点数分布：' + '，'.join('%d 节点 = %d 台' % (k, by_cnt[k])
                                         for k in sorted(by_cnt)) + '。')
    lines.append('')
    lines.append('判定分布：')
    for k, v in sorted(by_verdict.items(), key=lambda x: -x[1]):
        lines.append('- **%d 台**：%s' % (v, k))
    lines.append('')
    lines.append('| PIN | 雪橇 | 车型 | 唯一节点 | 链段 | place行 | 首条 | 末条 | 跨度 | 末条距今 | 最大空档 | 判断 |')
    lines.append('|-----|------|------|----------|------|---------|------|------|------|----------|----------|------|')
    for r in short_rows:
        lines.append('| %s | %s | %s | %d | %d | %d | %s | %s | %s | %s | %s | %s |' %
                     (r['pin_code'], r['sled_no'], r['vehicle_type'], r['node_cnt'],
                      r['chain_groups'], r['place_rows'], iso(r['first']), iso(r['last']),
                      fmt_dur(r['span']), fmt_dur(r['since_last']), fmt_dur(r['max_gap']),
                      r['verdict']))
    lines.append('')
    lines.append('## 启动回填曲线（恢复点后 40 分钟，逐分钟）')
    lines.append('')
    lines.append('| 分钟 | 时间 | place | clear |')
    lines.append('|------|------|-------|-------|')
    for m in range(40):
        d = startup.get(m) or {}
        lines.append('| +%d | %s | %d | %d |' %
                     (m, datetime.fromtimestamp(rate['resume_ts'] + m * 60).strftime('%H:%M'),
                      d.get('place', 0), d.get('clear', 0)))
    lines.append('')
    lines.append('## 写入速率')
    lines.append('')
    lines.append('- 最近 10 秒：%d 行，%.1f 行/秒' % (rate['last10'], rate['event_rate_10']))
    lines.append('- 最近 60 秒：%d 行，%.1f 行/秒' % (rate['last60'], rate['event_rate_60']))
    lines.append('- 最近 300 秒：%d 行，%.1f 行/秒' % (rate['last300'], rate['event_rate_300']))
    lines.append('- 最近 120 秒逐秒行数：最小 %d，中位 %d，最大 %d，平均 %.1f；'
                 '有事件的秒数 %d/120。' %
                 (rate['ps_min'], rate['ps_med'], rate['ps_max'], rate['ps_avg'], rate['ps_secs']))
    lines.append('- 最近 120 秒每 10 秒：' + '，'.join(rate['buckets']) + '。')
    lines.append('- `vehicle_events` 行数在 %d 秒内 %d → %d，实际 INSERT **%.1f 行/秒**'
                 '（`MAX(id)` %d → %d）。' %
                 (rate['sample_sec'], rate['n0'], rate['n1'], rate['insert_rate'],
                  rate['id0'], rate['id1']))
    lines.append('- 最近 60 秒事件类型：' + '，'.join(rate['evs']) + '。')
    lines.append('')
    lines.append('## 判定口径')
    lines.append('')
    lines.append('- `place` 事件按 `ts_epoch, id` 升序；同一 PIN 连续同节点压成链段。')
    lines.append('- 末条 place 距今 ≤15 分钟 → 仍在途/刚进站；')
    lines.append('- 首条在落库恢复点 5 分钟内 → 恢复时已在厂内/中段，不能当作真实进厂起点；')
    lines.append('- 末条已超过 15 分钟、跨度也超过 15 分钟，且 place 最大空档 ≥10 分钟 → 疑似中间漏数据。')
    lines.append('- 任何“漏数据”都是基于现有 place 空档的启发式判断，最终需对照 EMOS 订阅日志/现场确认。')
    lines.append('')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    ap = argparse.ArgumentParser(description='vehicle_events 轨迹完整性体检（只读）')
    ap.add_argument('--hours', type=float, default=0,
                    help='统计窗口小时数；0=今天 00:00 起（默认）')
    ap.add_argument('--md', default='',
                    help='输出 Markdown 报告路径；缺省 docs/traj_health_YYYYMMDD_HHMM.md')
    ap.add_argument('--sample-sec', type=int, default=20,
                    help='MAX(id) 增量采样秒数（默认 20）')
    args = ap.parse_args()

    now = int(time.time())
    today0 = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    win_start = today0 if args.hours <= 0 else now - int(args.hours * 3600)
    md_path = args.md or os.path.join(
        HERE, '..', 'docs', 'traj_health_%s.md' % datetime.fromtimestamp(now).strftime('%Y%m%d_%H%M'))

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT MIN(ts_epoch) mn, MAX(ts_epoch) mx, COUNT(*) n FROM vehicle_events")
    meta = cur.fetchone()
    latest = int(meta['mx'])
    cur.execute("SELECT MIN(ts_epoch) mn FROM vehicle_events "
                "WHERE ev='place' AND ts_epoch>=%s AND node_id<>''", (win_start,))
    resume_ts = int(cur.fetchone()['mn'])
    print('DB rows=%d  latest=%s' % (meta['n'], iso(latest)))
    print('window=%s ~ %s  resume(min place in window)=%s' %
          (iso(win_start), iso(now), iso(resume_ts)))

    cur.execute("""
        SELECT pin_code, MAX(sled_no) sled_no, MAX(vehicle_type) vehicle_type,
               MAX(platform) platform, COUNT(*) place_rows,
               COUNT(DISTINCT node_id) node_cnt,
               MIN(ts_epoch) first_today, MAX(ts_epoch) last_today
        FROM vehicle_events FORCE INDEX (idx_ev_ts_cover)
        WHERE ev='place' AND ts_epoch>=%s AND ts_epoch<=%s
          AND pin_code<>'' AND node_id<>''
        GROUP BY pin_code
    """, (win_start, now))
    active = cur.fetchall()
    # 只需要判断“今天之前有没有 place”：一次扫窗口前的索引区间（约 10s），
    # 比逐车查全时段首末（400+ 次查询，数分钟）快一个量级。
    cur.execute("""
        SELECT DISTINCT pin_code FROM vehicle_events FORCE INDEX (idx_ev_ts_cover)
        WHERE ev='place' AND ts_epoch<%s AND node_id<>''
    """, (win_start,))
    carry_pins = {r['pin_code'] for r in cur.fetchall()}
    # 对今天首见的车，first_all/last_all/place_all 就等于窗口内首末/行数，无需再查全表。
    for c in active:
        c['first_all'] = c['first_today']
        c['last_all'] = c['last_today']
        c['place_all'] = c['place_rows']
    entries = [c for c in active if c['pin_code'] not in carry_pins]
    resume_cars = [c for c in entries if c['first_all'] <= resume_ts + RESUME_WINDOW_SEC]

    print('active pins=%d  entries(first today)=%d  at-resume=%d' %
          (len(active), len(entries), len(resume_cars)))

    top5 = sorted(entries, key=lambda x: (-int(x['node_cnt']), int(x['first_all'])))[:5]
    short = [c for c in entries if int(c['node_cnt']) < 5]
    short_rows = []
    for c in short:
        st = chain_stats(cur, c['pin_code'])
        if not st:
            continue
        row = dict(c)
        row.update({'first': st['first'], 'last': st['last'], 'span': st['last'] - st['first'],
                    'since_last': now - st['last'], 'max_gap': st['max_gap'][0],
                    'chain_groups': len(st['groups'])})
        row['verdict_key'], row['verdict_detail'] = classify(c, st, now, resume_ts)
        row['verdict'] = row['verdict_key'] + ('：' + row['verdict_detail'] if row['verdict_detail'] else '')
        short_rows.append(row)
    short_rows.sort(key=lambda x: (int(x['node_cnt']), -int(x['last'])))
    print('short(<5)=%d' % len(short_rows))

    # ---- 写入速率 ----
    # 用行数增量而不是 MAX(id): 自增 id 在批量写入/事务提交间可能短时不动，
    # 行数增量才是真实落库速率(place/clear 都 INSERT，skid/target 另走 UPDATE)。
    cur.execute("SELECT MAX(id) max_id, COUNT(*) n, MAX(ts_epoch) max_ts FROM vehicle_events")
    s0 = cur.fetchone()
    time.sleep(max(1, args.sample_sec))
    cur.execute("SELECT MAX(id) max_id, COUNT(*) n, MAX(ts_epoch) max_ts FROM vehicle_events")
    s1 = cur.fetchone()
    insert_rate = (int(s1['n']) - int(s0['n'])) / float(max(1, args.sample_sec))
    # 速率窗口从采样结束的“此刻”起算：前面查短轨迹/采样都可能花 1~2 分钟，
    # 否则 last10/60/300 会把实际窗口拉长，速率虚高。
    rate_now = int(time.time())

    def ev_count(sec):
        cur.execute("SELECT COUNT(*) c FROM vehicle_events WHERE ts_epoch>=%s", (rate_now - sec,))
        return int(cur.fetchone()['c'])
    last10, last60, last300 = ev_count(10), ev_count(60), ev_count(300)
    cur.execute("""
        SELECT ts_epoch, COUNT(*) c FROM vehicle_events
        WHERE ts_epoch>=%s GROUP BY ts_epoch ORDER BY ts_epoch
    """, (rate_now - 120,))
    per_sec = [int(r['c']) for r in cur.fetchall()]
    vals = sorted(per_sec)
    cur.execute("""
        SELECT FLOOR(ts_epoch/10)*10 b, COUNT(*) c FROM vehicle_events
        WHERE ts_epoch>=%s GROUP BY b ORDER BY b
    """, (rate_now - 120,))
    buckets = ['%s=%d(%.1f/s)' % (datetime.fromtimestamp(int(r['b'])).strftime('%H:%M:%S'),
                                   r['c'], r['c'] / 10.0) for r in cur.fetchall()]
    cur.execute("""
        SELECT ev, COUNT(*) c FROM vehicle_events
        WHERE ts_epoch>=%s GROUP BY ev ORDER BY c DESC
    """, (rate_now - 60,))
    evs = ['%s=%d' % (r['ev'], r['c']) for r in cur.fetchall()]

    # 恢复点后 40 分钟逐分钟事件量: 用来区分“EMOS 初始回填不完整”与“真实漏数据”。
    cur.execute("""
        SELECT FLOOR((ts_epoch-%s)/60) m, ev, COUNT(*) c
        FROM vehicle_events
        WHERE ts_epoch>=%s AND ts_epoch<%s
        GROUP BY m, ev ORDER BY m
    """, (resume_ts, resume_ts, min(resume_ts + 2400, now + 60)))
    startup = {}
    for r in cur.fetchall():
        m = int(r['m'])
        if m < 0 or m >= 40:
            continue
        startup.setdefault(m, {})[r['ev']] = int(r['c'])
    for m in range(40):
        startup.setdefault(m, {})

    rate = {
        'now': rate_now, 'resume_ts': resume_ts,
        'insert_rate': insert_rate, 'sample_sec': args.sample_sec,
        'id0': int(s0['max_id']), 'id1': int(s1['max_id']),
        'n0': int(s0['n']), 'n1': int(s1['n']),
        'last10': last10, 'last60': last60, 'last300': last300,
        'event_rate_10': last10 / 10.0, 'event_rate_60': last60 / 60.0,
        'event_rate_300': last300 / 300.0,
        'ps_min': min(per_sec) if per_sec else 0,
        'ps_med': vals[len(vals) // 2] if vals else 0,
        'ps_max': max(per_sec) if per_sec else 0,
        'ps_avg': (sum(per_sec) / len(per_sec)) if per_sec else 0.0,
        'ps_secs': len(per_sec), 'buckets': buckets, 'evs': evs,
        'startup': startup,
    }

    markdown_report(active, entries, resume_cars, short_rows, top5, rate, md_path)

    print('\n== Top 5 ==')
    for i, c in enumerate(top5, 1):
        print('%d. PIN %s sled %s nodes=%d rows=%d %s~%s' %
              (i, c['pin_code'], c['sled_no'], c['node_cnt'], c['place_rows'],
               iso(c['first_all']), iso(c['last_all'])))
    print('\n== short verdict counts ==')
    vc = {}
    for r in short_rows:
        vc[r['verdict_key']] = vc.get(r['verdict_key'], 0) + 1
    for k, v in sorted(vc.items(), key=lambda x: -x[1]):
        print('  %3d  %s' % (v, k))
    print('\n== write rate ==')
    print('  last10=%.1f/s last60=%.1f/s last300=%.1f/s row-insert=%.1f/s'
          % (rate['event_rate_10'], rate['event_rate_60'], rate['event_rate_300'],
             rate['insert_rate']))
    print('  per-sec min/med/max/avg = %d/%d/%d/%.1f' %
          (rate['ps_min'], rate['ps_med'], rate['ps_max'], rate['ps_avg']))
    print('\nmarkdown -> %s' % md_path)
    conn.close()


if __name__ == '__main__':
    main()