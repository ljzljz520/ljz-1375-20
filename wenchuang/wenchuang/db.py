# -*- coding: utf-8 -*-
"""文创来源索引 - 数据模型

核心约束:
- 设计地 / 制造地 / 销售地 是不同字段, 全部挂在【批次】上
  (同名商品不同批次可能产地不同, 商品级不固化产地)
- 品牌故事(marketing_narrative)与实际制作信息分列, 前者不得覆盖后者
- 价格 = 币种 + 区间(上下限) + 渠道 + 生效时间; 无价格记录 = 无价, 绝不显示为 0/免费
- 证据关系 = 证据 -> 主张(批次字段), 允许多条争议证据并列
- 商品级快照(snapshot) 与 批次级版本(version) 分别留痕
"""
import sqlite3, json, time, os

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  brand_story TEXT DEFAULT '',          -- 营销/品牌故事, 仅叙事, 不是来源事实
  status TEXT NOT NULL DEFAULT 'on',    -- on / off(下架, 传播到其所有批次)
  merged_into INTEGER REFERENCES products(id), -- 商品合并目标
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  batch_no TEXT NOT NULL,
  design_location TEXT,                 -- 设计地
  manufacture_location TEXT,            -- 制造地(实际制作信息)
  culture_source TEXT,                  -- 文化出处
  sale_location TEXT,                   -- 销售地
  status TEXT NOT NULL DEFAULT 'on',    -- on / off
  off_reason TEXT DEFAULT '',
  current_version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  UNIQUE(product_id, batch_no)
);

CREATE TABLE IF NOT EXISTS batch_versions (
  -- 批次级版本: 每次来源字段变更产生一条不可变版本
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  version INTEGER NOT NULL,
  design_location TEXT, manufacture_location TEXT,
  culture_source TEXT, sale_location TEXT,
  editor_role TEXT NOT NULL,            -- 记录修改者角色(用于审计营销编辑越权)
  changed_at INTEGER NOT NULL,
  change_note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sales_channels (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  channel TEXT NOT NULL,                -- 渠道名称
  url TEXT,
  link_status TEXT DEFAULT 'unknown',  -- ok / fail / unknown / pending
  link_checked_at INTEGER,
  link_status_note TEXT DEFAULT '',    -- 探测仅代表访问状态, 不证明店铺/产地真实
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  channel_id INTEGER REFERENCES sales_channels(id),
  currency TEXT NOT NULL,               -- 币种: CNY / USD ...
  amount_min INTEGER,                   -- 最小单位(分), NULL = 缺价
  amount_max INTEGER,                   -- 区间上限, NULL
  effective_from INTEGER NOT NULL,      -- 生效时间
  effective_to INTEGER,                 -- NULL 表示长期
  created_at INTEGER NOT NULL
);
-- 缺价 = 没有适用价格记录(或 amount_min IS NULL), 前端显示"暂无报价", 不得渲染 0 / 免费

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  claim_field TEXT NOT NULL,   -- manufacture_location / design_location / culture_source / sale_location ...
  claim_value TEXT,
  evidence_type TEXT NOT NULL,          -- doc / link / image / registry ...
  ref TEXT NOT NULL,                    -- 证据引用(发布作业校验其可用性)
  stance TEXT NOT NULL DEFAULT 'support', -- support / dispute  (争议证据并列)
  status TEXT NOT NULL DEFAULT 'active', -- active / withdrawn (撤权)
  withdrawn_note TEXT DEFAULT '',
  submitted_by TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  evidence_id INTEGER NOT NULL REFERENCES evidence(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  result TEXT NOT NULL,                 -- upheld(支持) / rejected(驳回) / pending
  reviewer TEXT NOT NULL,
  note TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  product_id INTEGER REFERENCES products(id),
  batch_id INTEGER REFERENCES batches(id),
  kind TEXT NOT NULL DEFAULT 'image',
  ref TEXT NOT NULL,
  license_status TEXT NOT NULL DEFAULT 'active', -- active / withdrawn
  withdrawn_note TEXT DEFAULT '',
  section TEXT NOT NULL DEFAULT 'material', -- material(资料) / ad(广告)
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS product_snapshots (
  -- 商品级快照: 收藏时定格的当时公开信息
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL,          -- 冻结的公开数据(含当时媒体URL等)
  taken_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
  id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL,
  product_id INTEGER NOT NULL REFERENCES products(id),
  snapshot_id INTEGER NOT NULL REFERENCES product_snapshots(id),
  -- 失效状态: 收藏内容在当下是否仍然可得/有效
  display_state TEXT NOT NULL DEFAULT 'active', -- active / product_off / media_withdrawn / merged
  invalid_reason TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(user_id, product_id)
);

CREATE TABLE IF NOT EXISTS search_index (
  -- 索引可能晚到: 记录索引版本, 搜索结果以已提交索引为准, 缺失不阻塞写入
  product_id INTEGER PRIMARY KEY REFERENCES products(id),
  body TEXT NOT NULL,
  indexed_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  actor_role TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL, detail TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);
"""

def now():
    return int(time.time())

def connect(db_path=":memory:"):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()
