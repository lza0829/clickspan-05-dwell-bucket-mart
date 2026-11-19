# 点跨（Clickspan）

点击流装载 → 按访客空闲超时切会话 → 脏事件隔离 → 会话证明互证。

**不是** 运单轨迹时间线（途迹）、变更投影流（changecast）、CDC Sink、券核销钉扎、日结封账、双边对齐、改价价签、证据包放行。  
去皮后是：**事件装载 + 会话窗物化 + 闸/隔离 + 证明**。

## 启动

```powershell
# 后端
cd backend
pip install -r requirements.txt
$env:DATA_DIR = "$PWD\data"
$env:FIXTURES_DIR = "$PWD\..\fixtures"
uvicorn app.main:app --host 127.0.0.1 --port 10900

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5900 |
| 接口 | http://127.0.0.1:10900 |

## 停留分桶物化

桶名不是可写字段：它只能由会话 `started_at`/`ended_at` 算出的停留秒（`ended_at` 缺时用末击点）
按可配置桶界映射后物化。

- 落库形态：独立事实表 `session_dwell_facts`，按会话关联；桶界按版本存 `dwell_bucket_bounds`，
  现行版本在 settings `dwell_bucket_version`。
- 同成败：起止写入与桶名重算由库层触发器在**同一事务**内完成——起止变了桶名必变，
  直连 SQL 改起止也会同事务重算；反之桶名无法脱离停留秒被单独 UPDATE（触发器拒绝）。
- 手改桶名入口（`PATCH /api/sessions/{id}` 带 `dwell_bucket`/`bucket`/`dwell_sec`）整笔失败，
  返回 `DWELL_BUCKET_MANUAL_EDIT` 及原因，不留下半笔写入。
- 核证：`GET /api/dwell/facts`（会话、停留秒、桶界版本、桶名逐条复算，`verified` 字段）、
  `GET /api/dwell/bounds`；只读。改桶界走 `PUT /api/dwell/bounds`，生成新版本并整批重物化。

## UI

**泳道织机**：顶栏品牌 + 顶栏菜单（织机 / 收流 / 搁置 / 核证 / 切窗）+ 横向会话串珠。  
不用底栏导航、不用三栏测绘板、不用左轨编号、不用流程 ribbon。
