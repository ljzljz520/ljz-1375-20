# -*- coding: utf-8 -*-
"""文创来源索引 HTTP API (标准库实现, 无第三方依赖)。
鉴权: X-Role 请求头(admin/editor/marketing/reviewer), X-User 表示用户。
注意: 系统不包含任何交易/结算接口。"""
import json, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from . import services as S
from .db import connect, init_db, now

JSON = {"Content-Type": "application/json; charset=utf-8"}

class App:
    def __init__(self, db_path=":memory:"):
        self.lock = threading.Lock()  # 串行化写事务(价格更新 vs 收藏同步)
        self.conn = connect(db_path)
        init_db(self.conn)

def make_handler(app, static_dir=None):
    conn = app.conn
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            if not n: return {}
            try: return json.loads(self.rfile.read(n))
            except Exception: raise S.ServiceError("bad_json", "请求体不是合法 JSON")
        def _role(self):
            # 公开页用户可能是匿名; 具体操作由 services 自行做角色白名单校验
            return self.headers.get("X-Role", "anon")
        def _user(self):
            return self.headers.get("X-User", "anonymous")


        def _serve_static(self, rel):
            import os
            if static_dir is None:
                return self._send(404, {"error": "no_static"})
            rel = rel.lstrip("/")
            if rel in ("", "public", "public.html"): rel = "public.html"
            if rel == "admin": rel = "admin.html"
            path = os.path.normpath(os.path.join(static_dir, rel))
            if not path.startswith(os.path.abspath(static_dir)) or not os.path.isfile(path):
                return self._send(404, {"error": "not_found"})
            ctype = "text/html; charset=utf-8" if path.endswith(".html") else                     "application/javascript; charset=utf-8" if path.endswith(".js") else                     "text/css; charset=utf-8"
            data = open(path, "rb").read()
            self.send_response(200); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                p = u.path
                if p == "/api/products":
                    return self._send(200, {"items": S.list_public(conn, q)})
                m = re.fullmatch(r"/api/products/(\d+)", p)
                if m:
                    lid = S.public_listing(conn, int(m.group(1)))
                    payload = S.public_snapshot_payload(conn, int(m.group(1)))
                    lid["_detail"] = payload
                    return self._send(200, lid)
                m = re.fullmatch(r"/api/products/(\d+)/evidence", p)
                if m:
                    rows = conn.execute("SELECT * FROM evidence WHERE batch_id IN"
                        " (SELECT id FROM batches WHERE product_id=?) ORDER BY id",
                        (int(m.group(1)),)).fetchall()
                    return self._send(200, {"evidence": [dict(r) for r in rows]})
                m = re.fullmatch(r"/api/batches/(\d+)/versions", p)
                if m:
                    rows = conn.execute("SELECT * FROM batch_versions WHERE batch_id=? ORDER BY version",
                                        (int(m.group(1)),)).fetchall()
                    return self._send(200, {"versions": [dict(r) for r in rows]})
                if p == "/api/map":
                    # 地图: 制造地/设计地/销售地分别返回, 供前端三个图层
                    items = S.list_public(conn, q)
                    make, design, sale = set(), set(), set()
                    for it in items:
                        for b in it["batches"]:
                            if b["manufacture_location"]: make.add(b["manufacture_location"])
                            if b["design_location"]: design.add(b["design_location"])
                            if b["sale_location"]: sale.add(b["sale_location"])
                    return self._send(200, {"manufacture": sorted(make),
                                            "design": sorted(design), "sale": sorted(sale)})
                if p == "/api/compare":
                    ids = [int(x) for x in q.get("ids", "").split(",") if x]
                    return self._send(200, {"items": S.compare(conn, ids)})
                if p == "/api/search":
                    return self._send(200, {"items": S.search(conn, q.get("q", ""))})
                m = re.fullmatch(r"/api/favorites/(\d+)", p)
                if m:
                    return self._send(200, S.render_favorite(conn, int(m.group(1))))
                if p == "/api/favorites":
                    rows = conn.execute("SELECT id FROM favorites WHERE user_id=?",
                                        (self._user(),)).fetchall()
                    return self._send(200, {"items": [S.render_favorite(conn, r["id"]) for r in rows]})
                self._serve_static(p)
            except S.ServiceError as e:
                return self._send(e.http, {"error": e.code, "message": e.msg})

        def do_POST(self):
            try:
                with app.lock:
                    body = self._body(); p = urlparse(self.path).path; role = self._role()
                    if p == "/api/products":
                        return self._send(201, dict(S.create_product(conn, role,
                            body["name"], body.get("brand_story", ""))))
                    m = re.fullmatch(r"/api/products/(\d+)/story", p)
                    if m:
                        return self._send(200, dict(S.update_brand_story(conn, role,
                            int(m.group(1)), body["brand_story"])))
                    m = re.fullmatch(r"/api/products/(\d+)/batches", p)
                    if m:
                        bid = S.create_batch(conn, role, int(m.group(1)),
                            body["batch_no"], body)
                        return self._send(201, {"batch_id": bid})
                    m = re.fullmatch(r"/api/products/(\d+)/merge", p)
                    if m:
                        S.merge_product(conn, role, int(m.group(1)), int(body["into"]))
                        return self._send(200, {"merged": True})
                    m = re.fullmatch(r"/api/products/(\d+)/publish", p)
                    if m:
                        S.publish_product(conn, role, int(m.group(1)))
                        return self._send(200, {"published": True})
                    m = re.fullmatch(r"/api/products/(\d+)/status", p)
                    if m:
                        S.set_product_status(conn, role, int(m.group(1)),
                            body["status"], body.get("reason", ""))
                        return self._send(200, {"status": body["status"]})
                    m = re.fullmatch(r"/api/products/(\d+)/reindex", p)
                    if m:
                        S.reindex_product(conn, int(m.group(1)))
                        return self._send(200, {"reindexed": True})
                    m = re.fullmatch(r"/api/batches/(\d+)/sources", p)
                    if m:
                        v = S.update_batch_sources(conn, role, int(m.group(1)), body,
                                                   body.get("note", ""))
                        return self._send(200, {"version": v})
                    m = re.fullmatch(r"/api/batches/(\d+)/channels", p)
                    if m:
                        cid = S.add_channel(conn, role, int(m.group(1)),
                                            body["channel"], body.get("url"))
                        return self._send(201, {"channel_id": cid})
                    m = re.fullmatch(r"/api/channels/(\d+)/probe", p)
                    if m:
                        S.record_link_probe(conn, role, int(m.group(1)),
                                            body["status"], body.get("note", ""))
                        return self._send(200, {"probed": True})
                    m = re.fullmatch(r"/api/batches/(\d+)/prices", p)
                    if m:
                        pid = S.add_price(conn, role, int(m.group(1)),
                            body["currency"], body.get("amount_min"), body.get("amount_max"),
                            body["effective_from"], body.get("channel_id"),
                            body.get("effective_to"))
                        return self._send(201, {"price_id": pid})
                    m = re.fullmatch(r"/api/batches/(\d+)/evidence", p)
                    if m:
                        eid = S.add_evidence(conn, role, int(m.group(1)),
                            body["claim_field"], body.get("claim_value"),
                            body["evidence_type"], body["ref"], body.get("stance", "support"))
                        return self._send(201, {"evidence_id": eid})
                    m = re.fullmatch(r"/api/evidence/(\d+)/review", p)
                    if m:
                        S.review_evidence(conn, role, int(m.group(1)),
                                          body["result"], body.get("note", ""))
                        return self._send(200, {"reviewed": body["result"]})
                    m = re.fullmatch(r"/api/evidence/(\d+)/withdraw", p)
                    if m:
                        S.withdraw_evidence(conn, role, int(m.group(1)), body["note"])
                        return self._send(200, {"withdrawn": True})
                    m = re.fullmatch(r"/api/products/(\d+)/media", p)
                    if m:
                        mid = S.add_media(conn, role, int(m.group(1)),
                            body["ref"], body.get("section", "material"), body.get("batch_id"))
                        return self._send(201, {"media_id": mid})
                    m = re.fullmatch(r"/api/media/(\d+)/withdraw", p)
                    if m:
                        S.withdraw_media(conn, role, int(m.group(1)), body["note"])
                        return self._send(200, {"withdrawn": True})
                    m = re.fullmatch(r"/api/products/(\d+)/favorites", p)
                    if m:
                        fid = S.add_favorite(conn, self._user(), int(m.group(1)))
                        return self._send(201, {"favorite_id": fid})
                    m = re.fullmatch(r"/api/favorites/(\d+)/sync", p)
                    if m:
                        return self._send(200, S.sync_favorite(conn, self._user(),
                            int(body["product_id"])))
                return self._send(404, {"error": "not_found"})
            except S.ServiceError as e:
                try: self.conn_rollback()
                except Exception: pass
                return self._send(e.http, {"error": e.code, "message": e.msg})
        def conn_rollback(self):
            conn.execute("ROLLBACK")
    return H

def run(db_path="wcindex.db", port=8080, static_dir=None):
    if static_dir is None:
        import os
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    app = App(db_path)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app, static_dir))
    print(f"文创来源索引 API: http://127.0.0.1:{port}")
    httpd.serve_forever()

if __name__ == "__main__":
    run()
