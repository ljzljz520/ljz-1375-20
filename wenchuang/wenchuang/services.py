# -*- coding: utf-8 -*-
"""领域服务: 所有业务规则集中于此, API 层只做参数编排。"""
import json, time
from .db import now

# ---- 角色与权限 ---------------------------------------------------------
ROLES = {"admin", "editor", "marketing", "reviewer"}
# 来源字段: 营销编辑不得触碰(不能用品牌故事覆盖实际制作信息)
SOURCE_FIELDS = ("design_location", "manufacture_location",
                 "culture_source", "sale_location")

class ServiceError(Exception):
    def __init__(self, code, msg, http=400):
        super().__init__(msg); self.code = code; self.msg = msg; self.http = http

def _audit(conn, role, action, target, detail=""):
    conn.execute("INSERT INTO audit_log(actor_role,action,target,detail,created_at)"
                 " VALUES(?,?,?,?,?)", (role, action, target, detail, now()))

# ---- 商品 ---------------------------------------------------------------
def create_product(conn, role, name, brand_story=""):
    if role not in ("admin", "editor"):
        raise ServiceError("forbidden", "无权创建商品", 403)
    ts = now()
    cur = conn.execute("INSERT INTO products(name,brand_story,created_at,updated_at)"
                       " VALUES(?,?,?,?)", (name, brand_story, ts, ts))
    pid = cur.lastrowid
    _audit(conn, role, "product.create", str(pid), name)
    conn.commit()
    return get_product(conn, pid)

def update_brand_story(conn, role, pid, story):
    # 品牌故事允许营销编辑修改, 但与来源字段严格分列, 互不覆盖
    if role not in ("admin", "editor", "marketing"):
        raise ServiceError("forbidden", "无权修改", 403)
    conn.execute("UPDATE products SET brand_story=?, updated_at=? WHERE id=?",
                 (story, now(), pid))
    _audit(conn, role, "product.story", str(pid))
    conn.commit()
    return get_product(conn, pid)

def merge_product(conn, role, src_id, dst_id):
    """商品合并: src 的批次移入 dst; src 标记 merged; 收藏标记 merged 且不丢快照。"""
    if role != "admin":
        raise ServiceError("forbidden", "仅管理员可合并商品", 403)
    src = get_product(conn, src_id); dst = get_product(conn, dst_id)
    conn.execute("UPDATE batches SET product_id=? WHERE product_id=?", (dst_id, src_id))
    conn.execute("UPDATE media SET product_id=? WHERE product_id=?", (dst_id, src_id))
    conn.execute("UPDATE products SET status='off', merged_into=?, updated_at=? WHERE id=?",
                 (dst_id, now(), src_id))
    conn.execute("UPDATE favorites SET display_state='merged',"
                 " invalid_reason=? WHERE product_id=?",
                 (f"商品已合并至 #{dst_id}", src_id))
    _audit(conn, role, "product.merge", f"{src_id}->{dst_id}")
    conn.commit()

def set_product_status(conn, role, pid, status, reason=""):
    if role not in ("admin", "reviewer"):
        raise ServiceError("forbidden", "无权上下架", 403)
    # 下架传播: 商品下架 -> 全部批次下架 -> 公开页/收藏随之失效
    conn.execute("UPDATE products SET status=?, updated_at=? WHERE id=?", (status, now(), pid))
    if status == "off":
        conn.execute("UPDATE batches SET status='off', off_reason=? WHERE product_id=? AND status='on'",
                     (reason or "商品下架传播", pid))
        conn.execute("UPDATE favorites SET display_state='product_off', invalid_reason=?"
                     " WHERE product_id=? AND display_state='active'",
                     (reason or "商品已下架", pid))
    _audit(conn, role, f"product.{status}", str(pid), reason)
    conn.commit()

# ---- 批次 ---------------------------------------------------------------
def create_batch(conn, role, product_id, batch_no, fields):
    if role not in ("admin", "editor"):
        raise ServiceError("forbidden", "无权录入批次", 403)
    vals = {k: fields.get(k) for k in SOURCE_FIELDS}
    ts = now()
    try:
        cur = conn.execute(
            "INSERT INTO batches(product_id,batch_no,design_location,manufacture_location,"
            "culture_source,sale_location,created_at) VALUES(?,?,?,?,?,?,?)",
            (product_id, batch_no, vals["design_location"], vals["manufacture_location"],
             vals["culture_source"], vals["sale_location"], ts))
    except Exception as e:
        raise ServiceError("batch_conflict", f"批次号已存在: {batch_no}", 409)
    bid = cur.lastrowid
    conn.execute("INSERT INTO batch_versions(batch_id,version,design_location,"
                 "manufacture_location,culture_source,sale_location,editor_role,changed_at,change_note)"
                 " VALUES(?,1,?,?,?,?,?,?,?)",
                 (bid, vals["design_location"], vals["manufacture_location"],
                  vals["culture_source"], vals["sale_location"], role, ts, "录入"))
    _audit(conn, role, "batch.create", str(bid), batch_no)
    conn.commit()
    return bid

def update_batch_sources(conn, role, batch_id, fields, note=""):
    """来源字段修改: 营销编辑被拒; 每次修改生成批次级不可变版本。"""
    if role == "marketing":
        raise ServiceError("forbidden",
            "营销编辑不得修改设计地/制造地/文化出处/销售地等来源字段", 403)
    if role not in ("admin", "editor"):
        raise ServiceError("forbidden", "无权修改来源字段", 403)
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b: raise ServiceError("not_found", "批次不存在", 404)
    new = {k: fields.get(k, b[k]) for k in SOURCE_FIELDS}
    ver = b["current_version"] + 1; ts = now()
    conn.execute("UPDATE batches SET design_location=?,manufacture_location=?,"
                 "culture_source=?,sale_location=?,current_version=? WHERE id=?",
                 (new["design_location"], new["manufacture_location"],
                  new["culture_source"], new["sale_location"], ver, batch_id))
    conn.execute("INSERT INTO batch_versions(batch_id,version,design_location,"
                 "manufacture_location,culture_source,sale_location,editor_role,changed_at,change_note)"
                 " VALUES(?,?,?,?,?,?,?,?,?)",
                 (batch_id, ver, new["design_location"], new["manufacture_location"],
                  new["culture_source"], new["sale_location"], role, ts, note))
    _audit(conn, role, "batch.source_update", str(batch_id), f"v{ver} {note}")
    conn.commit()
    return ver

# ---- 渠道与链接探测 ------------------------------------------------------
def add_channel(conn, role, batch_id, channel, url):
    if role not in ("admin", "editor"):
        raise ServiceError("forbidden", "无权录入渠道", 403)
    cur = conn.execute("INSERT INTO sales_channels(batch_id,channel,url,created_at)"
                       " VALUES(?,?,?,?)", (batch_id, channel, url, now()))
    _audit(conn, role, "channel.add", str(cur.lastrowid), channel)
    conn.commit()
    return cur.lastrowid

def record_link_probe(conn, role, channel_id, status, note=""):
    """链接探测结论仅表示访问状态(ok/fail), 不证明店铺或产地真实性。"""
    if status not in ("ok", "fail", "unknown"):
        raise ServiceError("bad_status", "状态仅可为 ok/fail/unknown")
    conn.execute("UPDATE sales_channels SET link_status=?, link_checked_at=?,"
                 " link_status_note=? WHERE id=?",
                 (status, now(), note or "仅反映访问状态, 不证明店铺/产地真实", channel_id))
    _audit(conn, role, "link.probe", str(channel_id), status)
    conn.commit()

# ---- 价格: 币种 + 区间 + 渠道 + 生效时间 --------------------------------
def add_price(conn, role, batch_id, currency, amount_min, amount_max,
              effective_from, channel_id=None, effective_to=None):
    if role not in ("admin", "editor"):
        raise ServiceError("forbidden", "营销编辑无权改价", 403)
    if amount_min is not None and amount_max is not None and amount_max < amount_min:
        raise ServiceError("bad_range", "价格区间上限不得小于下限")
    cur = conn.execute(
        "INSERT INTO prices(batch_id,channel_id,currency,amount_min,amount_max,"
        "effective_from,effective_to,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (batch_id, channel_id, currency, amount_min, amount_max,
         effective_from, effective_to, now()))
    _audit(conn, role, "price.add", str(cur.lastrowid), f"{currency}")
    conn.commit()
    return cur.lastrowid

def current_price(conn, batch_id, at=None, channel_id=None):
    """返回当前生效价格; 无记录或缺价返回 None -> 页面显示'暂无报价', 绝不显示免费/0。"""
    at = at or now()
    q = ("SELECT * FROM prices WHERE batch_id=? AND effective_from<=? "
         "AND (effective_to IS NULL OR effective_to>=?)")
    args = [batch_id, at, at]
    if channel_id is not None:
        q += " AND (channel_id=? OR channel_id IS NULL)"; args.append(channel_id)
    q += " ORDER BY effective_from DESC, id DESC"
    rows = conn.execute(q, args).fetchall()
    if not rows: return None
    r = rows[0]
    if r["amount_min"] is None: return None   # 显式缺价
    return dict(r)

# ---- 证据: 并列 / 复核 / 下架传播 ---------------------------------------
def add_evidence(conn, role, batch_id, claim_field, claim_value, etype, ref,
                 stance="support"):
    if claim_field not in SOURCE_FIELDS:
        raise ServiceError("bad_field", "证据须指向具体来源字段")
    if stance not in ("support", "dispute"):
        raise ServiceError("bad_stance", "stance 仅支持 support/dispute")
    if role not in ("admin", "editor", "reviewer"):
        raise ServiceError("forbidden", "无权提交证据", 403)
    cur = conn.execute(
        "INSERT INTO evidence(batch_id,claim_field,claim_value,evidence_type,ref,"
        "stance,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (batch_id, claim_field, claim_value, etype, ref, stance, role, now()))
    _audit(conn, role, f"evidence.{stance}", str(cur.lastrowid), claim_field)
    conn.commit()
    return cur.lastrowid

def review_evidence(conn, role, evidence_id, result, note=""):
    """复核争议证据: 成立(upheld)则批次下架并传播到公开页与收藏。"""
    if role not in ("admin", "reviewer"):
        raise ServiceError("forbidden", "无权复核", 403)
    e = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if not e: raise ServiceError("not_found", "证据不存在", 404)
    conn.execute("INSERT INTO reviews(evidence_id,batch_id,result,reviewer,note,created_at)"
                 " VALUES(?,?,?,?,?,?)",
                 (evidence_id, e["batch_id"], result, role, note, now()))
    if result == "upheld" and e["stance"] == "dispute":
        conn.execute("UPDATE batches SET status='off', off_reason=? WHERE id=?",
                     (f"争议证据 #{evidence_id} 经复核成立: {note}", e["batch_id"]))
        conn.execute("UPDATE favorites SET display_state='product_off', invalid_reason=?"
                     " WHERE product_id=(SELECT product_id FROM batches WHERE id=?)"
                     " AND display_state='active'",
                     ("相关批次来源争议成立, 已下架", e["batch_id"]))
    _audit(conn, role, "evidence.review", str(evidence_id), result)
    conn.commit()

def withdraw_evidence(conn, role, evidence_id, note):
    if role not in ("admin", "reviewer"):
        raise ServiceError("forbidden", "无权撤证", 403)
    conn.execute("UPDATE evidence SET status='withdrawn', withdrawn_note=? WHERE id=?",
                 (note, evidence_id))
    _audit(conn, role, "evidence.withdraw", str(evidence_id), note)
    conn.commit()

# ---- 媒体与撤权 ---------------------------------------------------------
def add_media(conn, role, product_id, ref, section="material", batch_id=None):
    if section not in ("material", "ad"):
        raise ServiceError("bad_section", "section 仅支持 material/ad")
    # 广告素材可由营销编辑录入; 资料素材需资料/管理员
    if section == "ad" and role not in ("admin", "marketing"):
        raise ServiceError("forbidden", "仅营销/管理员可投放广告", 403)
    if section == "material" and role not in ("admin", "editor"):
        raise ServiceError("forbidden", "无权上传资料媒体", 403)
    cur = conn.execute(
        "INSERT INTO media(product_id,batch_id,ref,section,created_at)"
        " VALUES(?,?,?,?,?)", (product_id, batch_id, ref, section, now()))
    _audit(conn, role, "media.add", str(cur.lastrowid), section)
    conn.commit()
    return cur.lastrowid

def withdraw_media(conn, role, media_id, note):
    if role not in ("admin", "editor", "reviewer"):
        raise ServiceError("forbidden", "无权撤下媒体", 403)
    row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not row: raise ServiceError("not_found", "媒体不存在", 404)
    conn.execute("UPDATE media SET license_status='withdrawn', withdrawn_note=? WHERE id=?",
                 (note, media_id))
    # 撤权传播: 含该素材的收藏标记失效; 快照内的引用在渲染时必须掩码, 永不重新暴露
    conn.execute("UPDATE favorites SET display_state='media_withdrawn', invalid_reason=?"
                 "  WHERE product_id=? AND display_state IN ('active','product_off')",
                 (f"素材 #{media_id} 已撤权: {note}", row["product_id"]))
    _audit(conn, role, "media.withdraw", str(media_id), note)
    conn.commit()

# ---- 商品级快照 & 收藏 ---------------------------------------------------
def public_snapshot_payload(conn, product_id):
    """构造当时公开信息。已撤媒体一律不出现(收藏后也不会重新暴露)。"""
    p = get_product(conn, product_id)
    batches = []
    for b in conn.execute("SELECT * FROM batches WHERE product_id=? ORDER BY id",
                         (product_id,)).fetchall():
        chans = [dict(c) for c in conn.execute(
            "SELECT id,channel,url,link_status,link_status_note FROM sales_channels WHERE batch_id=?",
            (b["id"],))]
        evs = [dict(e) for e in conn.execute(
            "SELECT id,claim_field,claim_value,evidence_type,ref,stance FROM evidence"
            " WHERE batch_id=? AND status='active'", (b["id"],))]
        batches.append({"batch": dict(b), "channels": chans, "evidence": evs,
                        "price": current_price(conn, b["id"])})
    media = [dict(m) for m in conn.execute(
        "SELECT id,ref,section FROM media WHERE product_id=? AND license_status='active'",
        (product_id,))]
    return {"product": {k: p[k] for k in p.keys()}, "batches": batches, "media": media}

def take_snapshot(conn, product_id, commit=True):
    payload = public_snapshot_payload(conn, product_id)
    cur = conn.execute("INSERT INTO product_snapshots(product_id,snapshot_json,taken_at)"
                       " VALUES(?,?,?)", (product_id, json.dumps(payload, ensure_ascii=False), now()))
    if commit: conn.commit()
    return cur.lastrowid

def add_favorite(conn, user_id, product_id):
    """收藏定格当时公开信息(商品级快照), 之后不就地改写。"""
    conn.execute("BEGIN IMMEDIATE")
    p = get_product(conn, product_id)
    if p["status"] != "on":
        raise ServiceError("product_off", "商品当前未公开发布", 409)
    existed = conn.execute("SELECT id FROM favorites WHERE user_id=? AND product_id=?",
                           (user_id, product_id)).fetchone()
    if existed: raise ServiceError("dup", "已收藏", 409)
    sid = take_snapshot(conn, product_id, commit=False)
    cur = conn.execute("INSERT INTO favorites(user_id,product_id,snapshot_id,created_at)"
                       " VALUES(?,?,?,?)", (user_id, product_id, sid, now()))
    conn.execute("COMMIT")
    return cur.lastrowid

def sync_favorite(conn, user_id, product_id):
    """收藏同步: 只刷新失效标记, 不改写冻结快照; 已撤素材在渲染层掩码。
    与价格更新并发时以 BEGIN IMMEDIATE 串行化, 价格是追加生效记录, 不污染快照。"""
    conn.execute("BEGIN IMMEDIATE")
    fav = conn.execute("SELECT * FROM favorites WHERE user_id=? AND product_id=?",
                       (user_id, product_id)).fetchone()
    if not fav:
        conn.execute("COMMIT"); raise ServiceError("not_found", "收藏不存在", 404)
    p = get_product(conn, product_id)
    state, reason = "active", ""
    if p["merged_into"]:
        state, reason = "merged", f"商品已合并至 #{p['merged_into']}"
    elif p["status"] == "off":
        state, reason = "product_off", "商品已下架"
    else:
        n = conn.execute("SELECT COUNT(*) c FROM media WHERE product_id=?"
                         " AND license_status='withdrawn'", (product_id,)).fetchone()["c"]
        if n and fav["display_state"] == "media_withdrawn":
            state, reason = "media_withdrawn", "含已撤权素材"
        elif n:
            state, reason = "media_withdrawn", "含已撤权素材"
    conn.execute("UPDATE favorites SET display_state=?, invalid_reason=? WHERE id=?",
                 (state, reason, fav["id"]))
    conn.execute("COMMIT")
    return render_favorite(conn, fav["id"])

def render_favorite(conn, fav_id):
    """渲染收藏: 保留当时公开信息; 失效时显示状态并掩码已撤素材引用。"""
    fav = conn.execute("SELECT * FROM favorites WHERE id=?", (fav_id,)).fetchone()
    snap = conn.execute("SELECT * FROM product_snapshots WHERE id=?",
                        (fav["snapshot_id"],)).fetchone()
    data = json.loads(snap["snapshot_json"])
    withdrawn = {r["id"] for r in conn.execute(
        "SELECT id FROM media WHERE license_status='withdrawn'").fetchall()}
    masked = []
    for m in data.get("media", []):
        if m["id"] in withdrawn:
            masked.append({"id": m["id"], "section": m["section"],
                           "ref": None, "removed": True,
                           "note": "该素材已撤权, 不再展示"})   # 永不重新暴露
        else:
            masked.append(m)
    data["media"] = masked
    data["favorite"] = {"state": fav["display_state"], "reason": fav["invalid_reason"],
                        "taken_at": snap["taken_at"]}
    return data

# ---- 搜索索引(允许晚到) --------------------------------------------------
def reindex_product(conn, product_id):
    p = get_product(conn, product_id)
    words = [p["name"], p["brand_story"]]
    for b in conn.execute("SELECT * FROM batches WHERE product_id=?", (product_id,)):
        words += [b["culture_source"] or "", b["manufacture_location"] or "",
                  b["design_location"] or "", b["sale_location"] or ""]
    conn.execute("INSERT INTO search_index(product_id,body,indexed_at) VALUES(?,?,?)"
                 " ON CONFLICT(product_id) DO UPDATE SET body=excluded.body,"
                 " indexed_at=excluded.indexed_at",
                 (product_id, " ".join(w for w in words if w), now()))
    conn.commit()

def search(conn, keyword):
    """仅检索已入索引商品: 索引晚到时新商品暂搜不到, 但不影响其发布/详情。"""
    like = f"%{keyword}%"
    rows = conn.execute("SELECT product_id FROM search_index WHERE body LIKE ?", (like,)).fetchall()
    out = []
    for r in rows:
        p = get_product(conn, r["product_id"])
        if p["status"] == "on":
            out.append(public_listing(conn, r["product_id"]))
    return out

# ---- 发布作业: 校验引用来源 + 可用媒体; 不含任何交易结算 -----------------
def publish_product(conn, role, product_id):
    if role not in ("admin", "editor"):
        raise ServiceError("forbidden", "无权发布", 403)
    problems = []
    batches = conn.execute("SELECT * FROM batches WHERE product_id=?", (product_id,)).fetchall()
    if not batches:
        problems.append("至少需要一个批次")
    for b in batches:
        for f in ("design_location", "manufacture_location", "culture_source", "sale_location"):
            if not b[f]:
                problems.append(f"批次 {b['batch_no']} 缺少字段 {f}")
        # 每个来源主张至少有一条 active 的支持证据, 引用必须存在
        for f in ("design_location", "manufacture_location", "culture_source", "sale_location"):
            ev = conn.execute("SELECT COUNT(*) c FROM evidence WHERE batch_id=?"
                " AND claim_field=? AND status='active' AND stance='support'",
                (b["id"], f)).fetchone()["c"]
            if not ev: problems.append(f"批次 {b['batch_no']} 的 {f} 缺少支持证据")
        # 争议未复核 -> 阻断发布
        d = conn.execute("SELECT COUNT(*) c FROM evidence WHERE batch_id=?"
            " AND stance='dispute' AND status='active' AND id NOT IN"
            " (SELECT evidence_id FROM reviews WHERE result='rejected')",
            (b["id"],)).fetchone()["c"]
        if d: problems.append(f"批次 {b['batch_no']} 存在未复核的争议证据")
    media = conn.execute("SELECT * FROM media WHERE product_id=?", (product_id,)).fetchall()
    if not any(m["section"] == "material" and m["license_status"] == "active" for m in media):
        problems.append("缺少可用的资料区媒体")
    for m in media:
        if m["license_status"] != "active":
            problems.append(f"媒体 #{m['id']} 已撤权, 不可发布")
        if not str(m["ref"]).strip():
            problems.append(f"媒体 #{m['id']} 引用为空")
    if problems:
        raise ServiceError("publish_blocked", "；".join(problems), 422)
    conn.execute("UPDATE products SET status='on', updated_at=? WHERE id=?", (now(), product_id))
    _audit(conn, role, "product.publish", str(product_id))
    conn.commit()

# ---- 读取 ---------------------------------------------------------------
def get_product(conn, pid):
    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not r: raise ServiceError("not_found", "商品不存在", 404)
    return r

def public_listing(conn, pid):
    p = get_product(conn, pid)
    batches = []
    for b in conn.execute("SELECT * FROM batches WHERE product_id=? AND status='on' ORDER BY id",
                         (pid,)):
        price = current_price(conn, b["id"])
        batches.append({
            "batch_id": b["id"], "batch_no": b["batch_no"],
            "design_location": b["design_location"],
            "manufacture_location": b["manufacture_location"],
            "culture_source": b["culture_source"],
            "sale_location": b["sale_location"],
            "version": b["current_version"],
            "price": None if price is None else {
                "currency": price["currency"],
                "min": price["amount_min"], "max": price["amount_max"],
                "channel_id": price["channel_id"],
                "effective_from": price["effective_from"],
                "display": "暂无报价" if price is None else None,
            },
        })
    return {"product_id": p["id"], "name": p["name"], "brand_story": p["brand_story"],
            "merged_into": p["merged_into"], "batches": batches}

def list_public(conn, filters=None):
    """公开页列表 + 筛选(文化出处/制造地/销售地/币种), 下架/合并商品不出现。"""
    filters = filters or {}
    q = ("SELECT DISTINCT p.id FROM products p JOIN batches b ON b.product_id=p.id "
         "WHERE p.status='on' AND b.status='on'")
    args = []
    if filters.get("culture_source"):
        q += " AND b.culture_source=?"; args.append(filters["culture_source"])
    if filters.get("manufacture_location"):
        q += " AND b.manufacture_location=?"; args.append(filters["manufacture_location"])
    if filters.get("sale_location"):
        q += " AND b.sale_location=?"; args.append(filters["sale_location"])
    if filters.get("design_location"):
        q += " AND b.design_location=?"; args.append(filters["design_location"])
    rows = conn.execute(q, args).fetchall()
    items = [public_listing(conn, r["id"]) for r in rows]
    if filters.get("currency"):
        cur = filters["currency"]
        items = [it for it in items if any(
            bt["price"] and bt["price"]["currency"] == cur for bt in it["batches"])]
    return items

def compare(conn, product_ids):
    """商品比较: 商品级快照信息 + 各批次当前版本; 下架批次保留可见但标注。"""
    return [public_listing(conn, pid) for pid in product_ids]
