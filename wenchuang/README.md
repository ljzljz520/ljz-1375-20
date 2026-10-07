# 文创来源索引（文化站建设配套系统）

面向文化站的**文创商品来源索引**：管理页录入材料/制作地/文化出处/销售渠道，API 将
**商品、批次、证据关系**持久化，公开页提供地图、筛选与比较。系统只做信息来源管理，
**不含任何下单、支付、结算功能**。

零第三方依赖（Python 3.11 标准库 `http.server` + `sqlite3`）。

## 快速开始

```bash
# 1) 初始化演示数据（同名商品两批次，制造地分别为德化/佛山，B 批次缺价）
python3 -m wenchuang.seed wcindex.db
# 2) 启动服务（管理页 + 公开页 + API 同端口）
python3 -m wenchuang wcindex.db 8080
```

- 管理台：http://127.0.0.1:8080/admin.html （右上角切换角色）
- 公开页：http://127.0.0.1:8080/public.html
- 验收测试：`python3 tests/test_acceptance.py` → **9/9**

## 角色

| 角色 | 权限要点 |
|---|---|
| `editor` 资料编辑 | 录入商品/批次来源字段、渠道、价格、资料媒体、支持证据 |
| `marketing` 营销编辑 | 仅可改**品牌故事**、投放**广告区**素材；来源字段、价格、资料媒体一律 403 |
| `reviewer` 复核员 | 复核证据、撤证、撤媒体、商品下架 |
| `admin` 管理员 | 全部，含商品合并 |
| 匿名公开用户 | 浏览/搜索/比较/收藏 |

HTTP 头 `X-Role` 选角色，`X-User` 标识用户（测试用，生产应替换为真实鉴权中间件）。

## 数据模型（见 `wenchuang/db.py`）

- **商品 products**：品牌故事 `brand_story` 是营销叙事，与事实字段物理分列，永不覆盖。
- **批次 batches / batch_versions**：设计地、制造地、文化出处、销售地全部挂在批次上；
  同名商品不同批次可以有不同产地。来源字段每次修改生成一条**不可变批次版本**。
- **证据 evidence / reviews**：证据指向具体字段主张，`support`/`dispute` 并列；
  复核结论 `upheld`/`rejected`。争议成立 → 批次下架并传播到公开页与收藏。
- **渠道 sales_channels**：`link_status` 仅表示链接访问状态（ok/fail），
  备注固定声明“**不证明店铺/产地真实**”。
- **价格 prices**：币种 + 区间(min/max) + 渠道 + 生效起止。无生效记录或 min 为空 = **缺价**，
  公开页渲染“暂无报价”，绝不显示 0/免费。价格是追加式生效记录，便于与收藏快照隔离。
- **媒体 media**：`material` 资料区与 `ad` 广告区分离；撤权后公开页/快照中一律掩码，不重新暴露。
- **商品快照 product_snapshots + 收藏 favorites**：收藏定格当时公开信息，之后只更新
  `display_state`（active / product_off / media_withdrawn / merged），不改写冻结内容。
- **搜索索引 search_index**：索引可晚到——未入索引时搜不到，但不阻断发布与详情访问。

## 关键业务规则（`wenchuang/services.py`）

1. **营销隔离**：营销编辑改来源字段/价格/资料媒体均 403；品牌故事独立字段保存。
2. **发布作业**：同时校验①每个批次四项来源字段齐全 ②每个主张有 active 支持证据
   ③无未复核争议 ④至少一件可用资料区媒体 ⑤引用非空、未撤权。任一不过返回 422 阻断发布。
3. **下架传播**：商品下架 → 全部在架批次下架 → 公开列表消失 → 在架收藏标记失效。
4. **图片撤权**：媒体 withdrawn → 发布阻断 + 收藏 `media_withdrawn`，历史快照中的引用渲染为“已撤权·不再展示”。
5. **商品合并**：仅 admin；源商品批次/媒体转入目标，源商品 off+merged_into，
   旧收藏标记 `merged` 且保留原快照。
6. **并发**：写请求经进程级锁串行；收藏与同步使用 `BEGIN IMMEDIATE`；
   价格追加新生效行而非原地更新，二者互不污染（测试并发 10 组写无错误）。
7. **比较**：商品级品牌故事（叙事）与各批次当前版本（事实）分列对照，缺价照常“暂无报价”。

## API 一览（节选）

```
POST /api/products                     新建商品
POST /api/products/{id}/story          修改品牌故事（营销可改）
POST /api/products/{id}/batches        录入批次+四地+出处
POST /api/batches/{id}/sources         修改来源（生成新版本；营销 403）
POST /api/batches/{id}/channels        销售渠道
POST /api/channels/{id}/probe          链接探测(仅访问状态)
POST /api/batches/{id}/prices          价格: 币种/区间/渠道/生效时间
POST /api/batches/{id}/evidence        证据(support|dispute)
POST /api/evidence/{id}/review         复核(upheld→下架传播)
POST /api/evidence/{id}/withdraw       撤证
POST /api/products/{id}/media          媒体(material|ad)
POST /api/media/{id}/withdraw          图片撤权(传播)
POST /api/products/{id}/publish        发布校验
POST /api/products/{id}/merge          商品合并(仅admin)
POST /api/products/{id}/status         上下架
POST /api/products/{id}/reindex        重建搜索索引(允许晚到)
POST /api/products/{id}/favorites      收藏(生成商品级快照)
POST /api/favorites/{id}/sync          收藏同步(只刷失效态)
GET  /api/products?manufacture_location=&culture_source=&currency= 筛选
GET  /api/map                          制造/设计/销售三图层地点
GET  /api/compare?ids=1,2              商品比较
GET  /api/search?q=                    仅查已入索引
GET  /api/favorites                    我的收藏（含失效态与撤权掩码）
```

系统**不存在** `/api/orders`、`/api/pay`、`/api/checkout` 等交易端点（测试断言 404）。

## 页面分区

- 公开页顶部独立黄色虚线**广告区**，明确标注“广告 AD”，并声明不构成来源/产地证明；
  商品卡素材带“资料 MATERIAL”标记，两类内容不混排。
- 地图用红/蓝/黄三色图钉分别表示制造地、设计地、销售地，点击列出涉及批次。
- 收藏列表对失效项红条提示（下架/合并/撤权），撤权素材显示删除线占位且**不返回真实引用**。

## 目录

```
wenchuang/
  db.py         数据模型
  services.py   业务规则/权限/快照/传播
  api.py        HTTP API + 静态服务
  seed.py       演示数据
  __main__.py   入口
static/
  admin.html    管理台
  public.html   公开页(地图/筛选/比较/收藏)
tests/
  test_acceptance.py  9 条验收测试
```
