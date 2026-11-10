# -*- coding: utf-8 -*-
"""
EMOS 订阅覆盖体检（只读，不写库）——判断“图上的可停节点是否都有订阅、订阅点今天是否真的在报”。

用法:
    python -X utf8 codes/sub_coverage_audit.py
    python -X utf8 codes/sub_coverage_audit.py --md docs/sub_coverage_YYYYMMDD_HHMM.md

口径:
- 可停节点 = 建图结果中 type 属于 RB/TT/SL/LT 的节点（CP/WT/LT_high/low 是机构或对位节点，不订阅）。
- 订阅点 = config/deviceConfig/F2x.json 的 rollers[].node。
- “图上未订阅”分两类：
    * transition：config 里标了 TRANSITION_RB / 过渡带，故意不订阅（车经过但不停，轨迹会跳点）。
    * non-transition：非过渡却漏订，属于真实覆盖缺口。
- “订阅但今天无 place”：可能是今天没车经过，也可能是 tag 映射错；需对照邻近节点/EMOS 日志。
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_line

DB_CFG = os.path.join(HERE, '..', 'config', 'db.json')
DEV_CFG_DIR = os.path.join(HERE, '..', 'config', 'deviceConfig')
STOPPABLE_TYPES = ('RB', 'TT', 'SL', 'LT')


def iso(ts):
    return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S') if ts else '-'


def load_subscriptions():
    subs = {}
    for fp in sorted(glob.glob(os.path.join(DEV_CFG_DIR, '*.json'))):
        line = os.path.basename(fp)[:-5]
        try:
            j = json.load(open(fp, encoding='utf-8'))
        except Exception as e:
            print('[warn] 读取 %s 失败: %s' % (fp, e))
            continue
        nodes = {r['node'] for r in (j.get('rollers') or []) if r.get('node')}
        subs[line] = nodes
    return subs


def connect():
    cfg = json.load(open(DB_CFG, encoding='utf-8'))
    cfg = {k.lower(): v for k, v in cfg.items()}
    return pymysql.connect(host=cfg.get('host', '127.0.0.1'), port=int(cfg.get('port', 3306)),
                           user=cfg['user'], password=cfg['password'], database=cfg['database'],
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)


def transition_nodes(line, cfg, r):
    """config 明确标注的过渡辊床 -> 节点名集合。"""
    d = {k.lower(): v for k, v in vars(cfg).items() if not k.startswith('_')}
    out = set()
    for dev in (d.get('transition_rb') or []):
        try:
            out.add(r['rb'](dev))
        except Exception:
            pass
    return out


def audit(now, today0, md_path):
    subs = load_subscriptions()
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT MIN(ts_epoch) mn FROM vehicle_events "
                "WHERE ev='place' AND ts_epoch>=%s AND node_id<>''", (today0,))
    resume = cur.fetchone()
    resume = int(resume['mn']) if resume and resume['mn'] else None
    cur.execute("SELECT node_id, COUNT(*) c, COUNT(DISTINCT pin_code) pins "
                "FROM vehicle_events WHERE ev='place' AND node_id<>'' AND ts_epoch>=%s "
                "GROUP BY node_id", (today0,))
    seen = {r['node_id']: (int(r['c']), int(r['pins'])) for r in cur.fetchall()}
    conn.close()

    lines, gaps, zeros = [], [], []
    for line in build_line.available_lines():
        try:
            cfg, r, _entries = build_line.build_line(line)
        except Exception as e:
            print('[warn] %s 建图失败: %s' % (line, e))
            continue
        G = r['G']
        stoppable = [n for n, d in G.nodes(data=True) if d.get('type') in STOPPABLE_TYPES]
        sub = subs.get(line, set())
        trans = transition_nodes(line, cfg, r)
        missing = [n for n in stoppable if n not in sub]
        extra = [n for n in sub if n not in G]
        no_event = [n for n in sub if n not in seen]
        missing_trans = [n for n in missing if n in trans]
        missing_real = [n for n in missing if n not in trans]
        gaps.extend((line, n) for n in missing_real)
        zeros.extend((line, n) for n in no_event)
        lines.append(dict(line=line, stoppable=len(stoppable), subscribed=len(sub),
                          missing=missing, missing_trans=missing_trans, missing_real=missing_real,
                          extra=extra, no_event=no_event))

    # ---- Markdown ----
    out = []
    out.append('# EMOS 订阅覆盖体检')
    out.append('')
    out.append('生成时间：%s  ' % iso(now))
    out.append('统计窗口：%s ~ %s  ' % (iso(today0), iso(now)))
    out.append('今天最早 place（落库恢复点）：%s' % (iso(resume) if resume else '-'))
    out.append('')
    out.append('## 摘要')
    out.append('')
    out.append('- 图上可停节点（RB/TT/SL/LT）与 deviceConfig 订阅点的差异见下表。')
    out.append('- **图上未订阅（非过渡）共 %d 个**：这是真实覆盖缺口。' % len(gaps))
    out.append('- 图上未订阅（过渡 RB）共 %d 个：车会经过但不停，轨迹必然跳点，属设计口径。'
               % sum(len(x['missing_trans']) for x in lines))
    out.append('- 订阅今天无 place 的点共 %d 个：可能今天没车经过，也可能 tag 映射错，需对照邻近节点。'
               % len(zeros))
    out.append('')
    out.append('## 分线明细')
    out.append('')
    out.append('| 线 | 可停节点 | 订阅点 | 图上未订阅(非过渡) | 图上未订阅(过渡) | 订阅不在图 | 订阅今天无 place |')
    out.append('|----|----------|--------|---------------------|------------------|------------|------------------|')
    for x in lines:
        out.append('| %s | %d | %d | %d | %d | %d | %d |' %
                   (x['line'], x['stoppable'], x['subscribed'], len(x['missing_real']),
                    len(x['missing_trans']), len(x['extra']), len(x['no_event'])))
    out.append('')

    def dump(title, items):
        out.append('## ' + title)
        out.append('')
        if not items:
            out.append('（无）')
        else:
            for line, n in sorted(items):
                out.append('- `%s`' % n)
        out.append('')

    dump('图上未订阅（非过渡，真实缺口）', gaps)
    trans_all = [(x['line'], n) for x in lines for n in x['missing_trans']]
    dump('图上未订阅（过渡 RB，设计上不订）', trans_all)
    dump('订阅点今天无 place', zeros)
    extra_all = [(x['line'], n) for x in lines for n in x['extra']]
    dump('订阅点不在建图结果', extra_all)

    out.append('## 判定口径')
    out.append('')
    out.append('- `TRANSITION_RB`（或配置中等价的过渡带）是“只过不停”的辊床，deviceConfig 不订阅它是已知口径；'
               '轨迹会从上一个可停点直接跳到下一个可停点，不能算漏数据。')
    out.append('- CP 横移链内部站位 / WT 对位点是机构节点，不入订阅；车辆经 CP 换位时同样会跳点。')
    out.append('- 非过渡节点若未订阅，或订阅点长期 0 事件且邻近节点有车，才是需要补订阅/查 tag 的覆盖缺口。')
    out.append('')
    path = md_path or os.path.join(HERE, '..', 'docs',
                                   'sub_coverage_%s.md' % datetime.fromtimestamp(now).strftime('%Y%m%d_%H%M'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    print('\n'.join(out))
    print('\nmarkdown -> %s' % path)
    return path


def main():
    ap = argparse.ArgumentParser(description='EMOS 订阅覆盖体检（只读）')
    ap.add_argument('--md', default='', help='输出 Markdown 路径')
    args = ap.parse_args()
    now = int(datetime.now().timestamp())
    today0 = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    audit(now, today0, args.md)


if __name__ == '__main__':
    main()