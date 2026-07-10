# Flight Watch Dashboard（GitHub Pages 静态前端）

纯前端、零构建的机票价格监控看板。数据由每日工作流生成后提交到本目录，Pages 直接托管。

## 开启 GitHub Pages

1. 打开仓库 **Settings → Pages**。
2. **Build and deployment → Source** 选择 **Deploy from a branch**。
3. **Branch** 选 `main`（或你的默认分支），文件夹选 **`/docs`**，点击 **Save**。
4. 约 1 分钟后，站点地址为 `https://<用户名>.github.io/<仓库名>/`，首页即 `docs/index.html`。

> Pages 以 `docs/` 为**站点根目录**。因此前端只能访问 `docs/` 内的文件；
> 仓库根的 `data/*.jsonl` 明细在站点外，**不可** 被前端 fetch。所需明细已由
> `scripts/gen_detail.py` 预聚合进 `docs/data/detail/{route}.json`。

## 目录结构与数据流

| 文件 | 由谁生成 | 用途 |
|---|---|---|
| `docs/data/summary.json` | `src/storage.py` 的 `build_summary`（`python -m src.main`） | 每航线 / 每出发日 的价格序列、最新价、历史低价 |
| `docs/data/routes_meta.json` | `scripts/gen_routes_meta.py` | 航线展示元数据：from/to、目标价、币种 |
| `docs/data/detail/{route}.json` | `scripts/gen_detail.py` | 航司最低价明细 + 数据源列表（柱状图 + 数据源筛选用） |
| `docs/index.html` | 手写 | 航线卡片网格：最低价、环比箭头、距目标差、sparkline |
| `docs/route.html` | 手写 | 详情页：筛选器 + 4 张 ECharts/SVG 图表 |
| `docs/vendor/charts.js` | 手写 | 零依赖 SVG 图表回退模块 |

`daily.yml` 中的调用顺序：`python -m src.main` → `gen_routes_meta.py` → `gen_detail.py` → commit。

## 图表引擎（ECharts 特性检测）

页面优先使用 **ECharts**，缺失时回退到内置的 `docs/vendor/charts.js`（纯 SVG，无依赖）。

> 构建环境网络受限，仓库中**未**内置 `echarts.min.js`，默认走 SVG 回退，功能完整可用。

升级为完整 ECharts（可选）：

1. 下载 <https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js>（或 unpkg / cdnjs / npmmirror 镜像）。
2. 保存为 `docs/vendor/echarts.min.js`。
3. 在 `docs/index.html` 与 `docs/route.html` 顶部取消这一行的注释：
   `<!-- <script src="./vendor/echarts.min.js"></script> -->`
4. 提交。页面检测到 `window.echarts` 后自动切换，无需改其它代码。

## 本地预览

必须通过 HTTP 服务器打开（`file://` 会因浏览器 CORS 无法 fetch 本地 JSON）：

```bash
cd docs && python3 -m http.server 8000
# 浏览器访问 http://localhost:8000/
```

深色 / 浅色跟随系统；移动端自适应；界面中文。
