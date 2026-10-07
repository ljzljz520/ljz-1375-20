# -*- coding: utf-8 -*-
"""验收测试: 直接走 HTTP API, 覆盖题目全部硬性规则。"""
import json, os, sys, time, threading, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wenchuang.api import App, make_handler
from http.server import ThreadingHTTPServer

PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"

def req(path, method="GET", body=None, role=None, user="u1"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE+path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    if role: r.add_header("X-Role", role)
    r.add_header("X-User", user)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def setup_product_full(role="editor"):
    """造一个字段/证据/媒体齐全的可发布商品, 返回 (pid, bid, media_id)。"""
    _, p = req("/api/products", "POST", {"name": "唐仕女立牌",
              "brand_story": "灵感源自盛唐气象"}, role)
    pid = p["id"]
    _, b = req(f"/api/products/{pid}/batches", "POST", {
        "batch_no": "2026-A", "design_location": "西安",
        "manufacture_location": "泉州德化", "culture_source": "簪花仕女图",
        "sale_location": "杭州西湖店"}, role)
    bid = b["batch_id"]
    for f in ("design_location", "manufacture_location", "culture_source", "sale_location"):
        req(f"/api/batches/{bid}/evidence", "POST",
            {"claim_field": f, "evidence_type": "doc", "ref": f"doc-{f}-1",
             "stance": "support"}, role)
    _, m = req(f"/api/products/{pid}/media", "POST",
        {"ref": "media/tang-1.png", "section": "material"}, role)
    return pid, bid, m["media_id"]

def build_publish(pid):
    s, j = req(f"/api/products/{pid}/publish", "POST", {}, "editor")
    assert s == 200, j
    return s, j

# ---- 场景 1: 营销编辑试图修改来源字段, 必须被拒绝; 品牌故事可改但不覆盖事实 ----
def test_marketing_cannot_touch_sources():
    pid, bid, _ = setup_product_full()
    s, j = req(f"/api/batches/{bid}/sources", "POST",
        {"manufacture_location": "马尔代夫", "note": "营销想改产地讲故事"}, "marketing")
    assert s == 403 and "来源字段" in j["message"], j
    # 品牌故事允许营销编辑修改
    s, j = req(f"/api/products/{pid}/story", "POST",
        {"brand_story": "全新大唐叙事"}, "marketing")
    assert s == 200
    # 实际制造地未被动过
    _, b = req(f"/api/batches/{bid}/versions")
    assert len(b["versions"]) == 1  # 只有录入版本, 无非法来源修改
    # 广告素材营销可投, 资料素材不可
    s, _ = req(f"/api/products/{pid}/media", "POST",
        {"ref": "ad/banner.png", "section": "ad"}, "marketing")
    assert s == 201
    s, _ = req(f"/api/products/{pid}/media", "POST",
        {"ref": "mat/x.png", "section": "material"}, "marketing")
    assert s == 403

# ---- 场景 2: 价格更新与收藏同步冲突(并发), 收藏快照不被改价污染 ----
def test_price_update_vs_favorite_sync():
    pid, bid, _ = setup_product_full(); build_publish(pid)
    req(f"/api/batches/{bid}/prices", "POST",
        {"currency": "CNY", "amount_min": 5900, "amount_max": 7900,
         "effective_from": int(time.time())-10}, "editor")
    s, fav = req(f"/api/products/{pid}/favorites", "POST", None, "anonymous", "fan1")
    assert s == 201
    fid = fav["favorite_id"]
    # 之后价格变化(新生效记录), 并同时触发收藏同步; 两者并发
    errors = []
    def change_price():
        try:
            for _ in range(5):
                req(f"/api/batches/{bid}/prices", "POST",
                    {"currency": "CNY", "amount_min": 6900, "amount_max": 6900,
                     "effective_from": int(time.time())}, "editor")
        except Exception as e: errors.append(e)
    def sync():
        try:
            for _ in range(5):
                req(f"/api/favorites/{fid}/sync", "POST", {"product_id": pid}, "anonymous", "fan1")
        except Exception as e: errors.append(e)
    import threading
    t1, t2 = threading.Thread(target=change_price), threading.Thread(target=sync)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors
    # 收藏冻结快照里的价格字段保持为收藏当时内容(渲染层不回填新价)
    _, view = req(f"/api/favorites/{fid}", user="fan1")
    assert view["favorite"]["state"] == "active"
    assert view["batches"][0]["price"]["amount_min"] == 5900
    # 缺价规则: 新批次不给价, 公开页必须显示"暂无报价"而非免费
    _, b2 = req(f"/api/products/{pid}/batches", "POST",
        {"batch_no": "2026-B", "design_location": "西安", "manufacture_location": "佛山",
         "culture_source": "簪花仕女图", "sale_location": "上海店"}, "editor")
    for f in ("design_location","manufacture_location","culture_source","sale_location"):
        req(f"/api/batches/{b2['batch_id']}/evidence","POST",
            {"claim_field": f, "evidence_type":"doc","ref":f"d-{f}"}, "editor")
    _, listing = req(f"/api/products/{pid}")
    nb = next(x for x in listing["batches"] if x["batch_no"] == "2026-B")
    assert nb["price"] is None

# ---- 场景 3: 图片撤权 -> 下架传播 + 收藏失效 + 快照不重新暴露 ----
def test_media_withdraw_and_favorite_invalid():
    pid, bid, mid = setup_product_full(); build_publish(pid)
    s, fav = req(f"/api/products/{pid}/favorites", "POST", None, "anonymous", "fan2")
    fid = fav["favorite_id"]
    s, _ = req(f"/api/media/{mid}/withdraw", "POST",
        {"note": "版权到期"}, "reviewer")
    assert s == 200
    s, view = req(f"/api/favorites/{fid}/sync", "POST", {"product_id": pid}, "anonymous", "fan2")
    assert view["favorite"]["state"] == "media_withdrawn", view
    # 快照中的该素材必须被掩码, ref 不得再出现
    m = next(x for x in view["media"] if x["id"] == mid)
    assert m["removed"] is True and m["ref"] is None
    # 发布被撤权素材阻断
    s, j = req(f"/api/products/{pid}/publish", "POST", {}, "editor")
    assert s == 422 and "撤权" in j["message"]

# ---- 场景 4: 商品合并 ----
def test_product_merge():
    p1, b1, _ = setup_product_full()
    # 第二个商品
    _, p2 = req("/api/products", "POST", {"name": "唐仕女立牌 精装版"}, "editor")
    _, b2 = req(f"/api/products/{p2['id']}/batches", "POST",
        {"batch_no": "X1", "design_location": "西安", "manufacture_location": "景德镇",
         "culture_source": "簪花仕女图", "sale_location": "北京店"}, "editor")
    for f in ("design_location","manufacture_location","culture_source","sale_location"):
        req(f"/api/batches/{b2['batch_id']}/evidence","POST",
            {"claim_field":f,"evidence_type":"doc","ref":f"dx-{f}"},"editor")
    req(f"/api/products/{p2['id']}/media","POST",
        {"ref":"media/x1.png","section":"material"},"editor")
    build_publish(p1); build_publish(p2["id"])
    req(f"/api/products/{p1}/favorites", "POST", None, "anonymous", "fan3")
    # 非 admin 不能合并
    s, _ = req(f"/api/products/{p1}/merge", "POST", {"into": p2["id"]}, "editor")
    assert s == 403
    s, _ = req(f"/api/products/{p1}/merge", "POST", {"into": p2["id"]}, "admin")
    assert s == 200
    # 源商品不再公开, 批次已转移
    s, j = req(f"/api/products")
    ids = [x["product_id"] for x in j["items"]]
    assert p1 not in ids
    dst = next(x for x in j["items"] if x["product_id"] == p2["id"])
    nos = [b["batch_no"] for b in dst["batches"]]
    assert "2026-A" in nos and "X1" in nos
    # 旧收藏标记 merged 且快照仍在
    _, favs = req("/api/favorites", user="fan3")
    assert favs["items"][0]["favorite"]["state"] == "merged"
    assert favs["items"][0]["product"]["name"] == "唐仕女立牌"

# ---- 场景 5: 搜索索引晚到 ----
def test_search_index_late():
    pid, bid, _ = setup_product_full(); build_publish(pid)
    # 未建索引: 列表/详情可见, 搜索不可见
    _, j = req("/api/search?q="+urllib.parse.quote("唐仕女"))
    assert all(x["product_id"] != pid for x in j["items"])
    _, detail = req(f"/api/products/{pid}")
    assert detail["name"] == "唐仕女立牌"
    # 索引到达后可搜
    req(f"/api/products/{pid}/reindex", "POST", {}, "editor")
    _, j = req("/api/search?q="+urllib.parse.quote("唐仕女"))
    assert any(x["product_id"] == pid for x in j["items"])

# ---- 争议证据并列、复核成立后批次下架并传播 ----
def test_dispute_review_takedown():
    pid, bid, _ = setup_product_full(); build_publish(pid)
    _, e = req(f"/api/batches/{bid}/evidence", "POST",
        {"claim_field": "manufacture_location", "claim_value": "实际为义乌代工",
         "evidence_type": "doc", "ref": "doc-dispute-1", "stance": "dispute"}, "editor")
    # 发布被未复核争议阻断
    s, j = req(f"/api/products/{pid}/publish", "POST", {}, "editor")
    assert s == 422
    # 支持与争议证据并列可见
    _, evs = req(f"/api/products/{pid}/evidence")
    stances = {x["stance"] for x in evs["evidence"]}
    assert {"support", "dispute"} <= stances
    # 复核驳回 -> 可发布
    req(f"/api/evidence/{e['evidence_id']}/review", "POST",
        {"result": "rejected", "note": "证据不足"}, "reviewer")
    s, _ = req(f"/api/products/{pid}/publish", "POST", {}, "editor")
    assert s == 200
    # 再来一条争议并复核成立 -> 批次下架传播
    _, e2 = req(f"/api/batches/{bid}/evidence", "POST",
        {"claim_field": "culture_source", "claim_value": "出处造假",
         "evidence_type": "registry", "ref": "reg-2", "stance": "dispute"}, "editor")
    req(f"/api/evidence/{e2['evidence_id']}/review", "POST",
        {"result": "upheld", "note": "核实造假"}, "reviewer")
    _, j = req("/api/products")
    assert all(not any(b["batch_no"]=="2026-A" for b in x["batches"]) for x in j["items"])

# ---- 同名商品不同批次产地不同 + 三地不同字段 + 筛选/比较/地图 ----
def test_multi_batch_locations_and_filter_map():
    pid, bid, _ = setup_product_full()  # 德化批次
    _, b2 = req(f"/api/products/{pid}/batches", "POST",
        {"batch_no": "2026-B", "design_location": "成都", "manufacture_location": "佛山",
         "culture_source": "簪花仕女图", "sale_location": "成都春熙店"}, "editor")
    for f in ("design_location","manufacture_location","culture_source","sale_location"):
        req(f"/api/batches/{b2['batch_id']}/evidence","POST",
            {"claim_field":f,"evidence_type":"doc","ref":f"d2-{f}"},"editor")
    build_publish(pid)
    _, f1 = req("/api/products?manufacture_location=" + urllib.parse.quote("佛山"))
    assert any(x["product_id"]==pid for x in f1["items"])
    _, f2 = req("/api/products?manufacture_location=" + urllib.parse.quote("泉州德化"))
    assert any(x["product_id"]==pid for x in f2["items"])
    _, mp = req("/api/map")
    assert "佛山" in mp["manufacture"] and "成都" in mp["design"] and "成都春熙店" in mp["sale"]
    _, cmp = req(f"/api/compare?ids={pid}")
    assert len(cmp["items"][0]["batches"]) == 2

# ---- 链接探测仅表示访问状态 ----
def test_link_probe_is_status_only():
    pid, bid, _ = setup_product_full()
    _, c = req(f"/api/batches/{bid}/channels", "POST",
        {"channel": "某网店", "url": "https://shop.example/x"}, "editor")
    req(f"/api/channels/{c['channel_id']}/probe", "POST",
        {"status": "ok"}, "editor")
    # 注意措辞: 不证明店铺/产地真实
    import sqlite3
    # 通过证据页间接确认渠道数据
    _, detail = req(f"/api/products/{pid}")
    ch = detail["_detail"]["batches"][0]["channels"][0]
    assert ch["link_status"] == "ok"
    assert "不证明" in ch["link_status_note"]

# ---- 系统不含交易/结算能力 ----
def test_no_transaction_endpoints():
    for path, method in [("/api/orders","POST"), ("/api/pay","POST"),
                         ("/api/checkout","POST"), ("/api/orders","GET"),
                         ("/api/payment","POST")]:
        s, _ = req(path, method, {"amount": 1}, "admin")
        assert s == 404, (path, s)

def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t(); print(f"  PASS {t.__name__}"); passed += 1
    print(f"\n{passed}/{len(tests)} 验收测试通过")

if __name__ == "__main__":
    app = App(":memory:")
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(app))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        run_all()
    finally:
        httpd.shutdown()
