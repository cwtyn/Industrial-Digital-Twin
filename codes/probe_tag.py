# -*- coding: utf-8 -*-
"""
单连接探测任意 EMOS 点位(验证 tag 是否存在、是否回值)。

用法:
  python codes/probe_tag.py --tag ".L3L1_1E_15AS_1E390SL_1E390SL.SB.S1012_PDown" --seconds 10
  python codes/probe_tag.py --plc L3L2 --tag ".L3L2_..." --tag ".L3L2_..."   # 一条连接探多个

说明:
- 必须复用同一条连接(服务器对快速重连有频率限制, 逐条新连接会误报 WSS.Error)。
- 服务器回 `WSS.Error` 只表示"该整串 tag 不存在"(设备号/分支/点位名任一写错), 不是认证问题。
"""
import argparse, json, os, threading, time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ws-url', default=os.environ.get('EMOS_WS_URL', '127.0.0.1/ws/emosweb'),
                    help='EMOS ws 地址(可用环境变量 EMOS_WS_URL)')
    ap.add_argument('--plc', default='L3L1', help='订阅报文的 plc 字段, 如 L3L1')
    ap.add_argument('--tag', action='append', required=True, help='点位 tag, 可重复')
    ap.add_argument('--seconds', type=float, default=10, help='探测时长')
    a = ap.parse_args()

    import websocket
    url = a.ws_url if a.ws_url.startswith('ws') else 'ws://' + a.ws_url
    ws = websocket.create_connection(url, timeout=30,
                                     proxy_type=None, http_proxy_host=None, http_proxy_port=None)
    print(f'[connected] {url}', flush=True)

    for i, t in enumerate(a.tag):
        pid = f'p{i + 1}'
        ws.send(json.dumps({'id': pid, 'type': 'advise', 'plc': a.plc, 'tag': t}))
        print(f'[sub] {pid} plc={a.plc} tag={t}', flush=True)

    stop = threading.Event()

    def hb():
        while not stop.is_set():
            time.sleep(2)
            try:
                ws.send(json.dumps({'type': 'info', 'info': 'alive'}))
            except Exception:
                return

    threading.Thread(target=hb, daemon=True).start()
    ws.settimeout(0.5)
    deadline = time.time() + a.seconds
    try:
        while time.time() < deadline:
            try:
                fr = ws.recv()
            except Exception:
                continue                      # 读超时: 继续等
            if isinstance(fr, (bytes, bytearray)):
                fr = fr.decode('utf-8', 'replace')
            try:
                m = json.loads(fr)
            except Exception:
                print('[raw]', fr, flush=True)
                continue
            typ = m.get('type')
            if typ == 'dataChange':
                print(f"[dataChange] tag={m.get('tag')} value={m.get('value')!r} ts={m.get('ts')}",
                      flush=True)
            else:
                print(f'[{typ}]', json.dumps(m, ensure_ascii=False), flush=True)
    finally:
        stop.set()
        try:
            ws.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
