#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""布局编辑器·阶段C tag 订阅点生成（自包含，不依赖 E: codes）。

用法:
    python tags.py [Line_5.json]         # 默认取 spec.json；从一线 spec 推导订阅点
    python tags.py Line_5.json Line_6.json  # 多线各出一个配置文件

输入: 线路规格 spec JSON（editor.html 导出，schema 见 spec_schema.md）。
      元素的 attrs/元素 tag 可覆盖推导（阶段B 输入）：
        tag.x1 / tag.x2  显式写死 X1/X2 串（覆盖按 kind 自动推导）
        tag.branch       '.IL.'/'IL' / '.ILO.'/'ILO'（台体分支，默认 IL）
        tag.mid          段号后额外插的非标准段(如 14AS)，正常缺省
      可停设备才有点位：RB/WT/LT/TT/SL/HE/TC；CP(机构)/EXT/ENT(接口标记) 不推。
输出: <线>.json —— deviceConfig 片段（镜像 export_device_config.py 结构），
      rollers 只存推导参数, 加载时由订阅方重拼 车身+雪橇；target 等额外点进 points。
      <线>_tags.txt —— 平铺的 车身/雪橇/目标 三条完整 tag 便于人工核对。
"""
import json, sys, os, re

try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass

PLC_SYS_PREFIX = 'L3'
TAG_NS = '.'
TAG_BRANCH = 'IL'
# 可停/可读 tag 的设备；CP/EXT/ENT 不产生订阅点
TAG_KINDS = ('RB', 'WT', 'LT', 'TT', 'SL', 'HE', 'TC')
# 功能件类型 -> 其"实际停靠设备"后缀(X1)。RB/WT/LT 恒自我；TT/SL/HE/TC 设备体
POINT_BODY = 'M1003_BodyID'
POINT_SKID = 'I1030_RBDataSkidNo'
POINT_TGT  = 'I1031_RBDataTarget'


def load_spec(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _seg(dev):
    """1A010RB -> 1A; 1D325RB -> 1D。段号=首个(数字*字母+)数字。"""
    m = re.match(r'(\d*[A-Z]+)\d+', dev or '')
    return m.group(1) if m else '1D'


def line_of_id(full_id):
    """Line_5_1A040TC -> Line_5; 兼容无前缀设备号。"""
    m = re.match(r'^(Line_\d+|Exit_\d+)(?:_|$)', str(full_id or ''))
    return m.group(1) if m else str(full_id or '').split('_')[0]


def dev_tokens(full_id):
    """Line_5_1A040TC.1 -> ('1A', '1A040', 'TC')。剥线前缀与 .位置号。"""
    rid = full_id.split('.')[0]
    tail = rid.split('_')[-1]                 # 1A040TC
    m = re.match(r'(\d*[A-Z]+\d+)([A-Z]+)', tail)
    seg = _seg(tail)
    pos = m.group(1) if m else tail
    suf = m.group(2) if m else ''
    return seg, pos, suf


def x1x2_of(el):
    """按 kind 推导 X1(实际停靠设备)/X2(该位置 RB)；element.tag 显式值优先。"""
    tag = el.get('tag') or {}
    rid = el['id']
    seg, pos, suf = dev_tokens(rid)
    kind = el['kind']
    if tag.get('x1'):
        x1, x2 = str(tag['x1']), str(tag.get('x2') or tag['x1'])
    elif kind == 'RB':
        x1 = x2 = f"{pos}RB"
    elif kind == 'LT':
        x1 = x2 = f"{pos}LT"                      # 现场取 LT_LT, 弃 LT_RB
    elif kind == 'WT':
        x1 = x2 = f"{pos}WT"                      # 独立存位台
    else:                                         # TT/SL/HE/TC：设备体 X1, 该位 RB X2
        x1 = f"{pos}{kind}"; x2 = f"{pos}RB"
    branch = str(tag.get('branch') or TAG_BRANCH).lstrip('.')
    if tag.get('branch'):
        branch = tag['branch'].lstrip('.').rstrip('.') or TAG_BRANCH
    mid = tag.get('mid')
    return x1, x2, branch, mid, seg


_LINE_SHORT_RE = re.compile(r'^Line_(\d+)$')
_EXIT_SHORT_RE = re.compile(r'^Exit_(\d+)$')


def _short_plc(short):
    """泛化线名 -> PLC 短线代号。Line_3 -> L3, Exit_2 -> X2; 其它原样返回。"""
    s = str(short or '')
    m = _LINE_SHORT_RE.match(s)
    if m:
        return 'L' + m.group(1)
    m = _EXIT_SHORT_RE.match(s)
    if m:
        return 'X' + m.group(1)
    return s


def plc_code(short):
    s = str(short or '')
    if s.startswith(PLC_SYS_PREFIX):
        return s
    return PLC_SYS_PREFIX + _short_plc(s)


def tag_path(short, rb_index, point, device=None, branch=None, mid=None, device_area=None):
    """.L3Lx_<段>[_<mid>]_<X1>_<X2>.IL.SD.<point>"""
    x1 = device or rb_index
    seg = device_area or _seg(x1)
    mid_part = f"_{mid}" if mid else ""
    branch = (branch or TAG_BRANCH)
    return f"{TAG_NS}{plc_code(short)}_{seg}{mid_part}_{x1}_{rb_index}.{branch}.SD.{point}"


def derive(el):
    """→ dict(node, device, type, mid, branch, body, skid, tgt)。不可停设备返回 None。"""
    if el['kind'] not in TAG_KINDS:
        return None
    if el.get('attrs', {}).get('park') is False:
        return None   # 过渡滚床/不可停(park:false)：只输送不停车，不订阅点位
    x1, x2, branch, mid, seg = x1x2_of(el)
    short = line_of_id(el['id'])
    body = tag_path(short, x2, POINT_BODY, device=x1, branch=branch, mid=mid, device_area=seg)
    skid = tag_path(short, x2, POINT_SKID, device=x1, branch=branch, mid=mid, device_area=seg)
    tgt  = tag_path(short, x2, POINT_TGT,  device=x1, branch=branch, mid=mid, device_area=seg)
    return {'node': el['id'], 'device': x1, 'device_rb': x2, 'type': None, 'mid': mid,
            'branch': branch, 'body': body, 'skid': skid, 'tgt': tgt}


def build(spec):
    """→ (rollers, points[], all_rows[])。"""
    det = [derive(e) for e in (spec.get('elements') or [])]
    rows = [d for d in det if d]
    rollers = [{'node': d['node'], 'device': d['device'], 'device_rb': d['device_rb'],
                'type': d['type'], 'mid': d['mid'], 'branch': d['branch']} for d in rows]
    # 额外点位：把 target 也进 points(随车目标地址)（body/skid 由 rollers 加载时拼）
    points = [{'node': d['node'], 'tag': d['tgt'], 'kind': 'target'} for d in rows]
    return rollers, points, rows


def emit(spec, outdir):
    line = spec.get('line', '')
    rollers, points, rows = build(spec)
    cfg = {'plc': plc_code(line), '_comment':
           '布局编辑器 tags.py 生成。rollers: 推导参数, 加载时重拼 车身(M1003_BodyID)+雪橇(I1030_RBDataSkidNo); '
           'device=X1 设备体, device_rb=X2 该位滚床, branch=台体分支(IL/ILO); points: target 目标地址等手写/派生点。',
           'rollers': rollers, 'points': points}
    out = os.path.join(outdir, line + '.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    txt = os.path.join(outdir, line + '_tags.txt')
    with open(txt, 'w', encoding='utf-8') as f:
        f.write(f'#{line} tag 订阅点\n')
        for d in rows:
            f.write(f"{d['node']:<22} X1={d['device']:<8} X2={d['device_rb']:<8}\n")
            f.write(f"    body   {d['body']}\n")
            f.write(f"    skid   {d['skid']}\n")
            f.write(f"    target {d['tgt']}\n")
    return cfg, out, txt, rows


def main(argv):
    try: sys.stdout.reconfigure(encoding='utf-8')
    except Exception: pass
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, 'deviceConfig')
    os.makedirs(outdir, exist_ok=True)
    paths = argv or ([os.path.join(here, 'spec.json')]
                     if os.path.exists(os.path.join(here, 'spec.json'))
                     else [os.path.join(here, 'samples', 'sample_spec.json')])
    for p in paths:
        if not os.path.exists(p):
            print(f'  跳过缺文件: {p}'); continue
        spec = load_spec(p)
        cfg, out, txt, rows = emit(spec, outdir)
        ln = spec.get('line', '?')
        print(f'[{ln}] {len(rows)} 台可停设备 → {out}')
        for d in rows:
            print(f'  {d["node"]:<22} {d["body"]}')
            print(f'  {"":<22} {d["skid"]}')
        print(f'  明细(含 target) → {txt}')


if __name__ == '__main__':
    main(sys.argv[1:])