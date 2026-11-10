# -*- coding: utf-8 -*-
"""
把校对好的合并 deviceConfig(逐 PLC 分组)拆成"每条输送一个文件"的新结构。

新结构(去掉冗余的 tag_body/tag_skid, 只存推导参数, 加载时由 build_subscriptions 重拼):
  config/deviceConfig/<线>.json
  {
    "plc": "L3L1",
    "rollers": [ {"node","device","type","mid"}, ... ],   # node 推导 车身+雪橇
    "points":  [ {"node","tag","kind"}, ... ]              # 额外点位(位信号等, tag 手写)
  }

用法:
  python codes/export_device_config.py [--src samples/deviceConfig_校对.json] [--out ../config/deviceConfig]
幂等: 每次执行会按 src 重新生成 rollers; 手写的 points 会保留合并(见 _EXTRA_POINTS)。
"""
import argparse, json, os, re

_HERE = os.path.dirname(os.path.abspath(__file__))
_LINE_PLC_RE = re.compile(r'^L(\d+)$')
_EXIT_PLC_RE = re.compile(r'^X(\d+)$')


def _short_of(plc):
    """L3L1 -> Line_1(剥掉前导 'L3' 并按 Line_n 反查); 未知代号保持原样。"""
    p = str(plc or '')
    code = p[2:] if p.startswith('L3') else p
    m = _LINE_PLC_RE.match(code)
    if m:
        return 'Line_' + m.group(1)
    m = _EXIT_PLC_RE.match(code)
    if m:
        return 'Exit_' + m.group(1)
    return code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=os.path.join(_HERE, 'samples', 'deviceConfig_校对.json'))
    ap.add_argument('--out', default=os.path.normpath(os.path.join(_HERE, '..', 'config', 'deviceConfig')))
    a = ap.parse_args()

    with open(a.src, 'r', encoding='utf-8') as f:
        src = json.load(f)

    os.makedirs(a.out, exist_ok=True)
    note = ("滚床订阅配置(每条输送一个文件)。\n"
            " rollers: 每条只存 node/device/type/mid, 加载时自动生成 车身(M1003_BodyID)+雪橇(I1030_RBDataSkidNo) 两条, .IL. 分支;\n"
            "          device=X1 设备体(RB/LT/TT/SL/TC/HE), type 用于 X1 覆盖(LT/TT/SL/TC), mid=非标准中间段(如14AS/15AS).\n"
            " points:  额外点位订阅, tag 写完整(如位信号 .SB. 分支、非母本点位), kind 任取(如 pos), node=所属节点;\n"
            "          新增类型(如 TC 位置)只需在此加一条即可.")
    for plc, entries in src.items():
        if plc in ('server', '_note') or not isinstance(entries, list):
            continue
        short = _short_of(plc)
        rollers = [{'node': e['node'], 'device': e.get('device'),
                    'type': e.get('type'), 'mid': e.get('mid')} for e in entries]
        path = os.path.join(a.out, f'{short}.json')
        # 幂等: 保留已有文件里手写的 points(加载后不覆盖), rollers 一律以 src 为准重新生成
        extras = []
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                prev = json.load(f)
            extras = prev.get('points') or []
        cfg = {'plc': plc, '_comment': note, 'rollers': rollers, 'points': extras}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"  {short}: {len(rollers)} 台, {len(extras)} 额外点 → {path}")
    print("完成。写入目录:", a.out)


if __name__ == '__main__':
    main()