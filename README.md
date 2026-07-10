# flight-watch ✈️ 机票低价监控系统

每天定时抓取指定航线的最低机票价格，命中「低于目标价」或「单日大跌」时通过飞书机器人推送提醒，并用 GitHub Pages 展示价格趋势 Dashboard。**全程零服务器、零月费**——数据源、调度、存储、推送、可视化全部跑在免费额度内。

> **一句话架构**：`fast-flights`（Google Flights 逆向库，可选 SerpAPI 兜底）做数据源 → GitHub Actions 每日 cron 抓取 → JSONL 存进 Git 仓库当数据库 → 告警引擎判价 → 飞书自定义机器人推卡片 → GitHub Pages 静态 Dashboard。

本项目为调研报告《机票低价监控系统：调研报告与执行计划》里程碑 M1–M5 的落地实现。设计依据与风险登记见同目录调研报告。

---

## 目录结构

```
flight-watch/
├── config.yaml              # 唯一配置源：航线/日期/阈值/通知（改配置不改代码）
├── config.json              # config.yaml 的无依赖镜像，沙箱/无 PyYAML 时自动回退，需与 yaml 同步
├── requirements.txt         # 依赖（fast-flights / PyYAML / requests，全部 pin 版本）
├── .gitignore               # 注意：data/ 与 state/serpapi_usage.json 故意不忽略
│
├── src/                     # 核心代码
│   ├── main.py              # pipeline 编排：抓取→落盘→summary→告警→通知
│   ├── config.py            # 配置加载 + 日期解析（rolling/fixed/both）
│   ├── models.py            # FlightQuote 数据模型、时区/币种工具
│   ├── storage.py           # JSONL 落盘（按月分片、去重）+ build_summary
│   ├── fetchers/            # 数据源适配器（FetcherAdapter 抽象）
│   │   ├── fast_flights.py  #   主源：Google Flights（懒加载，缺库不崩）
│   │   ├── serpapi.py       #   交叉校验源：SerpAPI（有月额度硬上限）
│   │   └── ctrip_local.py   #   国内兜底插件位：本地携程爬虫（M3 PoC 失败才启用）
│   ├── alerts/              # 告警引擎：规则评估 + 去重/熔断 + 失败看门狗
│   └── notifiers/           # 通知器：dispatch(cfg, summary, alerts) + 飞书卡片
│
├── scripts/
│   ├── gen_routes_meta.py   # 生成 docs/data/routes_meta.json（目标价/标签）
│   └── gen_detail.py        # 生成 docs/data/detail/{route}.json（航司柱状图/数据源）
│
├── docs/                    # GitHub Pages 站点根（Settings→Pages 指向 /docs）
│   ├── index.html           # 总览页
│   ├── route.html           # 单航线明细页
│   ├── vendor/charts.js     # 图表库（ECharts 缺失时的 SVG 回退，见「已知限制」）
│   └── data/                # 前端 fetch 的 JSON（由上面脚本 + main.py 生成）
│       ├── summary.json     #   折线/对比/热力数据（dashboard.output）
│       ├── routes_meta.json
│       └── detail/*.json
│
├── data/                    # JSONL 数据库（Git-as-DB），按航线/月分片，故意入库
├── state/                   # 运行状态：failures.json / alert_sent.json / serpapi_usage.json
└── tests/                   # 单元测试（49 项）
```

---

## 部署步骤

> 前置：先完成调研报告 6.2 节「首日必须亲手验证的 7 项」（本 README「首日验证清单」一节有复述），**尤其第 3 项 Actions IP 能否访问 Google Flights——这是全方案 Go/No-Go 开关**，未过不要开工。

### ① 建 GitHub public repo 并 push

**首次提交前先清理 mock 数据**（集成验证跑 dry-run 会在本地产生示例数据，不应带入首个真实提交）：

```bash
cd flight-watch

# 1) 清空 JSONL 数据库（保留目录与 .gitkeep）——或直接删掉整个 mock 航线目录：
rm -rf data/pek-cdg data/sha-can data/sha-nrt      # 删掉 dry-run 造出的 mock 航线数据
# 若想保留空目录结构，改为清空即可： : > data/*/*.jsonl

# 2) 复位运行状态为空态：
echo '{}' > state/failures.json
echo '{}' > state/alert_sent.json
rm -f state/serpapi_usage.json                      # 若存在

# docs/data/ 下的 summary.json / routes_meta.json / detail/*.json 是【预览数据】，
# 请【保留】——首次 GitHub Pages 加载时用它避免空白页；第一次真实 workflow 跑完会自动覆盖。
```

然后创建 **public** 仓库（Git-as-DB 需要公开仓库才能免费无限用 Actions，且 Pages 免费）并推送：

```bash
git init && git add . && git commit -m "init: flight-watch"
git branch -M main
git remote add origin https://github.com/<your-user>/flight-watch.git
git push -u origin main
```

### ② Settings → Secrets and variables → Actions，添加以下 Secrets

| Secret | 必需性 | 说明 |
|--------|--------|------|
| `FEISHU_WEBHOOK` | **必需** | 飞书自定义机器人 webhook 地址（含 token）。不设置则只有抓取、无推送。 |
| `FEISHU_SECRET` | 可选 | 飞书机器人开启「签名校验」时填。不填代码走无签名分支。 |
| `SERPAPI_KEY` | 可选 | 启用 SerpAPI 交叉校验源时填。不填则跳过 serpapi 源，只用 fast-flights。 |

### ③ Settings → Pages，开启 GitHub Pages

Source 选 **Deploy from a branch**，Branch 选 `main` / 目录 `/docs`，保存。等几分钟后 Pages 地址形如 `https://<your-user>.github.io/flight-watch/`。

### ④ 手动触发一次 workflow 验证

Actions 页 → **daily-flight-watch** → **Run workflow**（`workflow_dispatch`）。检查：
- Run pipeline 步骤日志出现各航线抓取条数；
- Commit & push data 步骤把 `data/ state/ docs/data/` 变更提交回库；
- 手机飞书收到日报卡片（若配置了 webhook）。

### ⑤ 把 dashboard.url 改成真实 Pages 地址

编辑 `config.yaml`（及镜像 `config.json`）末尾：

```yaml
dashboard:
  url: "https://<your-user>.github.io/flight-watch/"   # 卡片「查看趋势图」按钮跳这里
```

commit & push。之后 cron 每天 UTC 00:23（北京 08:23）自动运行。

---

## 配置说明（config.yaml）

> **`config.yaml` 与 `config.json` 必须保持同步**：前者是主配置，后者是无 PyYAML 环境下的自动回退镜像。改了一个记得改另一个。

每条航线（`routes[]` 数组的一项）：

| 字段 | 含义 |
|------|------|
| `id` | 航线唯一标识，同时是 `data/<id>/` 目录名与 dashboard 键。 |
| `from` / `to` | 出发 / 到达机场三字码（如 `SHA` / `NRT`）。 |
| `dates.mode` | 日期窗口模式：`fixed`（固定日期）/ `rolling`（滚动未来 N 天）/ `both`（两者并集去重）。 |
| `dates.fixed_dates` | `fixed`/`both` 用，固定监控的出发日列表。 |
| `dates.depart_in_days` | `rolling`/`both` 用，监控未来多少天内每一天。 |
| `airlines.whitelist` | 只保留这些航司（空 = 不限）。 |
| `airlines.blacklist` | 排除这些航司（如 `["MM"]` 排除乐桃）。 |
| `target_price` | 目标价；今日最低 **低于**它 → 触发 urgent 立即推送。 |
| `drop_alert_pct` | 单日跌幅阈值（%）；今日最低较昨日跌超此比例 → 触发 urgent。 |
| `sources` | 数据源顺序，如 `[fast_flights, serpapi]`；前者失败降级到后者。国内线 PoC 失败改为 `[ctrip_local]`。 |
| `enabled` | 是否启用该航线（`sha-can` 默认 `false`，M3 PoC 通过后改 `true`）。 |

其他区块：`defaults`（全局币种/语言/请求间隔/重试退避，可被 route 覆盖）、`cross_check`（SerpAPI 交叉校验额度与偏差阈值）、`alerts`（日报开关、urgent 去重时长、每日 urgent 熔断上限）、`notifiers`（各渠道开关，飞书 webhook 从 Secrets 读）、`dashboard`（输出路径与 Pages 地址）。

---

## 首日必须亲手验证的 7 项清单

> 来自调研报告 6.2 节。**勾完才开工/上线。第 3 项是 Go/No-Go 开关。**

- [ ] 1. 本机 `fast-flights` 查 SHA→NRT，成功返回航班与价格。
- [ ] 2. 本机 `fast-flights` 查 SHA→CAN（国内），记录航班覆盖（有没有漏春秋/九元）与返回币种。
- [ ] 3. **临时 public repo 在 GitHub Actions 上跑通同一查询**——验证 Actions 美国机房 IP 能否访问 Google Flights。**这是全方案最大未证实假设、真正的 Go/No-Go 开关；失败即触发 Plan B。**
- [ ] 4. 三方价格对照：fast-flights 返回价 vs Google Flights 网页价 vs 携程实际可购价，记录偏差。
- [ ] 5. 注册 SerpAPI，确认当前免费额度实际数字，跑通一次 `google_flights` engine 查询。
- [ ] 6. 建飞书群 + 自定义机器人，curl 发一条带按钮的卡片到手机并收到。
- [ ] 7. 手机 + 电脑在国内网络（不挂梯子）打开任意 GitHub Pages 站点，实测可达性与加载速度。

**第 3 项失败（Actions IP 被 Google 拦）的备选**：依次为 ① self-hosted runner（本机 Mac 跑 Actions）；② 全量转本地 launchd 调度；③ SerpAPI 限量模式（只监控 2–3 条重点航线）。

---

## M3 国内航线 PoC 操作指引

国内线（`sha-can`）默认 `enabled: false`。转正前必须跑 PoC 验证 Google Flights 数据对国内航线是否够用。

**操作**：
1. 把 `config.yaml` 中 `sha-can` 的 `enabled` 改为 `true`（保持 `sources: [fast_flights]`）。
2. 让系统连续运行 **3 天**（或本地手动 `python -m src.main` 跑 3 天），每天记录 SHA→CAN × 5 个日期的抓取结果。
3. 每天人工打开携程 App/网站，对照同航线同日期的实际最低价。

**判定标准**：
- **航班覆盖率 ≥ 80%**（Google 返回的航班占携程可见航班的比例，重点看是否漏掉春秋、九元等 LCC）；
- **价格偏差 ≤ 15%**（Google 最低价与携程可购价的差）。

**通过** → 国内航线正式进 config，零额外成本。
**失败**（覆盖率 <80% 或偏差 >15%） → 启用 `ctrip_local` 插件位：把该航线 `sources` 改为 `[ctrip_local]`，参照 `Suysker/Ctrip-Crawler` 在 Leon 本机跑携程爬虫（launchd 定时），输出**同 schema 的 JSONL** git push 汇入同一仓库，Dashboard 与告警无感知。预算 +5 个工作日。

---

## 本地运行

```bash
pip install -r requirements.txt

# 端到端 dry-run：MockFetcher 造假数据，退避/间隔=0，不发网络请求，打印卡片 JSON
NOTIFY_DRY_RUN=1 python -m src.main --dry-run

# 只跑指定航线
python -m src.main --routes sha-nrt,pek-cdg

# 真实运行（需已 pip install fast-flights，且本机 IP 能访问 Google Flights）
python -m src.main

# 生成 dashboard 数据（真实运行后）
python scripts/gen_routes_meta.py
python scripts/gen_detail.py

# 单元测试（应 49 项全过）
python -m unittest discover tests -v
```

- **`NOTIFY_DRY_RUN=1`**：通知器只打印卡片 JSON、不真正发飞书。用于本地/CI 验证推送内容。
- **`--dry-run`**：数据源换成 MockFetcher（确定性假数据），退避与请求间隔归零。**注意 dry-run 会写 `data/`、`state/`、`docs/data/`——本地验证后记得按上面「首次提交前清理」复位。**

---

## 故障排查

**fast-flights 失效怎么办**（它是「随时可能死的依赖」，维护者已于 2025-12 公开征集接手人）：
- 代码对 fast-flights 做**懒加载**，库缺失不会让 pipeline 崩溃；某源失败会按 `sources` 顺序降级到下一个。
- 备选库：`fli` / `faster-flights`（同类 Google Flights 逆向库），可在 `src/fetchers/` 新增适配器替换，核心不动。
- 兜底：启用 `serpapi` 源（配 `SERPAPI_KEY`），或对国内线走 `ctrip_local`。

**「结果数断言告警」是什么意思**：抓取源返回航班数异常（如骤降到 0 或远低于历史）时，`state/failures.json` 记连续失败；连续 ≥2 天无数据触发 **urgent 系统告警**（失败看门狗），提示数据源可能已失效，需人工检查是不是 fast-flights 挂了或被风控。

**Dashboard 空白**：确认 `docs/data/summary.json` 已生成；本地预览需通过 HTTP 服务器打开（`python -m http.server` 后访问，而非 `file://`）。

**飞书没收到消息**：确认 `FEISHU_WEBHOOK` Secret 已设；若机器人开了签名校验，必须同时设 `FEISHU_SECRET`。

**cron 不触发 / 被冻结**：GitHub 会在仓库 60 天无提交后停用 schedule。`keepalive.yml`（每月 1 日）会写一次 commit 保活，并检查 daily 工作流新鲜度（>3 天没跑就飞书告警）。

---

## 已知限制

- **ECharts 用 SVG 回退**：为不依赖国内不稳定的 CDN，`docs/vendor/charts.js` 内置轻量 SVG 图表回退。如需完整 ECharts 交互体验，可手动把 `echarts.min.js` 放进 `docs/vendor/` 并在 HTML 引用（可选增强，不影响基本可视化）。
- **cron 延迟 10–30 分钟**：GitHub Actions 定时任务在高峰期常有延迟，08:23 的任务实际可能 08:35–08:55 才跑，属正常现象。
- **Google 价 ≠ 可购价**：fast-flights 取的是 Google Flights 展示价，与携程/航司实际可购价存在口径差（含税/不含税、舱位放量差异）。告警用于「发现值得去查的低价」，最终以实际下单页为准。
- **GitHub Pages 国内可达性**：Fastly CDN 不挂梯子加载可能慢。首日第 7 项实测，不行可把 `docs/` 同步发到 Cloudflare Pages（同样免费，国内可达性通常更好，且支持 private repo）。

---

*交付：M5 集成验证与交付。单元测试 49 项全过，dry-run 端到端（抓取→落盘→summary→告警→飞书卡片）通过。详见调研报告第 9 章「实施交付记录」。*
