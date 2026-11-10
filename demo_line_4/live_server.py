# -*- coding: utf-8 -*-
"""
Line_4 demo · 现场实时状态 Web 可视化（决策表 DEMO 综合展示）

职责:
  1) 复用 layout_editor/tags.py(权威)推导 Line_4 全部点位的订阅 tag;
  2) 连车间 EMOS websocket, 全量预订阅:
       - 每台可停设备的 车身(M1003_BodyID)/雪橇(I1030_RBDataSkidNo)/目标(I1031_RBDataTarget)
       - 决策表 DB(L3L4_1A_LT025DecVisu) 4页×12行 决策逻辑字段
  3) 缓存最新 dataChange, 经本地 HTTP 提供:
       GET /       -> index.html(前端渲染)
       GET /spec   -> Line_4 布局(坐标/元素/决策 dest_map)
       GET /data   -> 实时缓存 {spec_line, dev, dec}

用法:
  python demo_line_4/live_server.py [--spec test/Line_4.json] [--port 8787]
依赖: websocket 客户端库(tags.py 运行需 platform; 订阅需 websocket)。
"""
import sys, os, json, time, threading, argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LAYOU = os.path.normpath(os.path.join(HERE, '..', 'layout_editor'))
if LAYOU not in sys.path:
    sys.path.insert(0, LAYOU)          # 供 import tags(唯一裁判,不重写推导)
import tags                                 # noqa: E402

WS_URL = os.environ.get('EMOS_WS_URL', 'ws://127.0.0.1/ws/emosweb')
PLC = 'L3L4'

DEC_FIELDS = (['AS.I9999_Filtr0%d' % i for i in range(1, 6)]
              + ['AS.I9999_Dest', 'SB.S9999_Use', 'SB.S9999_FullMatch', 'SB.S9999_Prior']
              + ['SB.S9999_Data%dMatch' % i for i in range(1, 6)])
# 完整 DB 字段名 -> 前端短名(前端 ORDER/PLATE 用短名)
SNAME = {'AS.I9999_Dest': 'Dest', 'SB.S9999_Use': 'Use',
         'SB.S9999_FullMatch': 'FullMatch', 'SB.S9999_Prior': 'Prior'}
SNAME.update({'AS.I9999_Filtr0%d' % i: 'Filtr0%d' % i for i in range(1, 6)})
SNAME.update({'SB.S9999_Data%dMatch' % i: 'Data%dMatch' % i for i in range(1, 6)})


def load_spec(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_device_subs(spec):
    """→ {node: {'plc','body','skid','tgt','nosub'}}（tag 串 default 出自 tags.build 权威推导；
    跨线设备用 RB_OVERRIDE_TAG 覆盖为真实线 L3L1; NOSUB 节点不订阅, 前端标"无实时源"）"""
    _, _, rows = tags.build(spec)
    return {r['node']: {'plc': PLC, 'body': r['body'], 'skid': r['skid'], 'tgt': r['tgt']}
            for r in rows}   # 权威 tag 全出自 tags.build; 三台过渡(LT)由 spec kind 正确推导


def find_dec(spec):
    """→ (dec_element_dict|None, [ (db, "."+db+".Row[p,r].field") ... ])"""
    dec = next((e for e in (spec.get('elements') or []) if e.get('kind') == 'DEC'), None)
    if not dec:
        return None, []
    a = dec.get('attrs') or {}
    db = (a.get('db') or '').strip() or '%s_%s' % (tags.plc_code(spec.get('line', '')), 'xxxLT000DecVisu')
    subs = ['.%s.Row[%d,%d].%s' % (db, p, r, f) for p in range(1, 5) for r in range(1, 13) for f in DEC_FIELDS]
    return {'id': dec['id'], 'db': db, 'dest_map': a.get('dest_map') or []}, subs


class EmosFeed:
    """订阅线程: 建立单连接, 分批 advise, 缓存 dataChange -> state."""

    def __init__(self, spec):
        self.state = {'spec_line': spec.get('line', ''), 'dev': {}, 'dec': {}}
        self.devs = build_device_subs(spec)
        for node, s in self.devs.items():
            self.state['dev'][node] = {'body': None, 'skid': None, 'tgt': None}
            if s.get('nosub'):
                self.state['dev'][node]['nosub'] = True
        self.dec_meta, dec_subs = find_dec(spec)
        self.dec_subs = dec_subs
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def _sub(self, ws, plc, tag, pid):
        ws.send(json.dumps({'id': pid, 'type': 'advise', 'plc': plc, 'tag': tag}))

    def run(self):
        import websocket
        conn_ok = False
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(WS_URL, timeout=30,
                                                 proxy_type=None, http_proxy_host=None,
                                                 http_proxy_port=None)
                print('[feed] connected', WS_URL, flush=True)
                conn_ok = True
                # 分批订阅, 规避一次性消息过多
                batch, i = [], 0
                for node, s in self.devs.items():
                    if s.get('nosub'):
                        continue                      # 无数据源设备不订阅
                    for k in ('body', 'skid', 'tgt'):
                        batch.append((s['plc'], s[k], 'd:%s:%s' % (node, k)))
                if self.dec_meta:
                    for t in self.dec_subs:
                        batch.append((PLC, t, 'c:%s' % t))
                B = 40
                for j in range(0, len(batch), B):
                    for plc, t, pid in batch[j:j + B]:
                        self._sub(ws, plc, t, pid)
                    time.sleep(0.15)
                print('[feed] subscribed %d tags' % len(batch), flush=True)
                hb = 0.0
                ws.settimeout(1.0)
                while not self._stop.is_set():
                    t0 = time.time()
                    try:
                        fr = ws.recv()
                    except Exception:
                        fr = None
                    if isinstance(fr, (bytes, bytearray)):
                        fr = fr.decode('utf-8', 'replace')
                    if fr:
                        try:
                            m = json.loads(fr)
                        except Exception:
                            m = None
                        if isinstance(m, dict) and m.get('type') == 'dataChange':
                            self._update(m.get('tag'), m.get('value'))
                    if time.time() - hb > 2:
                        try:
                            ws.send(json.dumps({'type': 'info', 'info': 'alive'}))
                        except Exception:
                            pass
                        hb = t0
                    if t0 - self._st() > 0:
                        pass
            except Exception as e:
                print('[feed] error:', repr(e), flush=True)
                conn_ok = False
            if self._stop.is_set():
                break
            print('[feed] 断开, 5s 后重连…', flush=True)
            time.sleep(5)
        try:
            ws and ws.close()
        except Exception:
            pass

    @staticmethod
    def _st():
        return 0.0

    def _update(self, raw_tag, value):
        if not raw_tag:
            return
        tag = raw_tag.lstrip('.')
        # dataChange 的 tag 带 plc 前缀(如 "L3L4."/"L3L1."), 剥掉使与订阅串一致
        if not tag.startswith('.'):
            first, _, rest = tag.partition('.')
            if rest and first.startswith('L3'):
                tag = rest
        # 设备: dataChange 的 tag 形如 L3L4.<L3L4_1A_1A025LT_1A025LT.IL.SD>.M1003_BodyID
        point = tag.rsplit('.', 1)[-1]
        kind = {'M1003_BodyID': 'body', 'I1030_RBDataSkidNo': 'skid',
                'I1031_RBDataTarget': 'tgt'}.get(point)
        if kind:
            for node, s in self.devs.items():
                if s[kind] and s[kind].lstrip('.') == tag:
                    with self.lock:
                        self.state['dev'][node][kind] = value
                    return
            return
        # 决策表: 形如 L3L4.L3L4_1A_LT025DecVisu.Row[页,行].AS.I9999_Dest
        if '.Row[' in tag:
            try:
                pr = tag.split('.Row[')[1].split(']')[0]     # "页,行"
                fld = tag.split('.Row[')[1].split(']')[1].lstrip('.')  # AS.I9999_Dest …
            except Exception:
                return
            with self.lock:
                self.state['dec'].setdefault(pr, {})[SNAME.get(fld, fld)] = value

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))


dummy_feed_ws = None  # noqa


class Handler(BaseHTTPRequestHandler):
    feed = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
        elif self.path == '/spec':
            with open(self.server.spec_path, 'rb') as f:
                self._send(200, f.read(), 'application/json; charset=utf-8')
        elif self.path == '/data':
            data = self.feed.snapshot()
            data['dec_meta'] = self.feed.dec_meta
            payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self._send(200, payload, 'application/json; charset=utf-8')
        else:
            self._send(404, b'not found', 'text/plain')


def main(argv):
    ap = argparse.ArgumentParser(description='Line_4 现场实时决策演示')
    ap.add_argument('--spec', default=os.path.join(LAYOU, 'test', 'Line_4.json'))
    ap.add_argument('--port', type=int, default=8787)
    a = ap.parse_args(argv)

    spec = load_spec(a.spec)
    feed = EmosFeed(spec)
    t = threading.Thread(target=feed.run, daemon=True)
    t.start()

    Handler.feed = feed
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    srv.spec_path = a.spec
    srv.feed = feed
    line = spec.get('line', 'Line_4')
    print('=' * 56)
    print(' Line_4 实时决策演示 · line=%s · %d 可停设备, DEC=%s'
          % (line, len(feed.devs), feed.dec_meta['id'] if feed.dec_meta else '无'))
    print(' 打开: http://127.0.0.1:%d/   (Ctrl+C 结束)' % a.port)
    print('=' * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        feed._stop.set()
        print('\n[bye]')


if __name__ == '__main__':
    main(sys.argv[1:])