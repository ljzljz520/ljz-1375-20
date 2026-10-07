# -*- coding: utf-8 -*-
"""演示用种子数据: 同名商品多批次不同产地 + 争议/撤权/缺价等边界。"""
import time
from .db import connect, init_db
from . import services as S

def seed(conn):
    ts = int(time.time())
    p = S.create_product(conn, "admin", "唐仕女立牌",
        "以盛唐仕女意象讲述品牌故事(仅叙事, 不构成产地依据)")
    pid = p["id"]
    b = S.create_batch(conn, "editor", pid, "2026-A", {
        "design_location": "西安", "manufacture_location": "泉州德化",
        "culture_source": "簪花仕女图", "sale_location": "杭州西湖店"})
    bid = b
    for f, ref in [("design_location","doc/design-xa.pdf"),
                   ("manufacture_location","doc/make-dehua.pdf"),
                   ("culture_source","doc/source-zanhua.pdf"),
                   ("sale_location","doc/sale-xihu.pdf")]:
        S.add_evidence(conn, "editor", bid, f, None, "doc", ref, "support")
    cid = S.add_channel(conn, "editor", bid, "西湖文创店", "https://shop.example/tang-a")
    S.record_link_probe(conn, "editor", cid, "ok")
    S.add_price(conn, "editor", bid, "CNY", 5900, 7900, ts - 86400 * 30)
    S.add_media(conn, "admin", pid, "media/tang-a.jpg", "material", bid)

    b2 = S.create_batch(conn, "editor", pid, "2026-B", {
        "design_location": "成都", "manufacture_location": "佛山",
        "culture_source": "簪花仕女图", "sale_location": "成都春熙店"})
    for f, ref in [("design_location","doc/design-cd.pdf"),
                   ("manufacture_location","doc/make-foshan.pdf"),
                   ("culture_source","doc/source-zanhua2.pdf"),
                   ("sale_location","doc/sale-chunxi.pdf")]:
        S.add_evidence(conn, "editor", b2, f, None, "doc", ref, "support")
    S.add_media(conn, "admin", pid, "media/tang-b.jpg", "material", b2)
    # 该批次缺价 -> 公开页显示"暂无报价"

    S.add_media(conn, "marketing", pid, "ad/banner-spring.jpg", "ad")
    S.publish_product(conn, "admin", pid)
    S.reindex_product(conn, pid)
    return pid

if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "wcindex.db"
    conn = connect(path); init_db(conn)
    pid = seed(conn)
    print(f"seed done: product {pid}, db={path}")
