# 厂站分布地图 - 飞书自动同步

项目地址：https://gbtn7s79gt-dotcom.github.io/project-delivery-map-demo/

## 目录结构

```
├── index.html              # 页面模板（地图、样式、交互逻辑）
├── data.js                 # 厂站数据（每周自动从飞书同步）
├── coords.json             # 城市坐标字典（WGS84）
├── sync_feishu.py          # 飞书数据同步脚本
├── requirements.txt        # Python 依赖
├── .github/workflows/weekly-sync.yml  # GitHub Actions 定时任务
└── last_sync.log           # 同步日志
```

## 数据来源

- 飞书多维表格：https://newhope1982.feishu.cn/base/UlRTbRb3GaJ91Ps6vMkcCEEin9V
- 每周一 10:00（北京时间）自动检查更新
- 如有数据变更，自动推送 `data.js` 到仓库并刷新页面

## 收录口径（2026-11-13 起）

地图**只统计已中标的厂站**，不再按飞书表全量更新：

- 保留条件：`WP中标概率 == 已中` **或** `APC中标概率 == 已中`
- 其余取值（`中` / `高` / `低` / 空）视为未中标，不进入页面
- 同一厂站两条产品线都「已中」只计一次（按记录去重，天然满足）

实现位置：`sync_feishu.py` 的 `is_won()` / `filter_won()`，`BID_WON = "已中"`。
脚本会打印「原始 N 条 → 中标 M 座」，并把 `raw=` / `kept=` 双计数写入 `last_sync.log`。

安全阈值：过滤后为空、或低于原始记录的 40% 时脚本直接中止并报错（防止列名/取值变更导致静默丢数据）。
排查对比时可临时用 `python sync_feishu.py --include-all` 走全量口径，**正式更新不要加该参数**。

## 本地测试

```bash
pip install -r requirements.txt
export FEISHU_APP_ID=xxx
export FEISHU_APP_SECRET=xxx
export FEISHU_APP_TOKEN=xxx
export FEISHU_TABLE_ID=xxx
python sync_feishu.py
```

## 新增城市坐标

坐标取值优先级（`sync_feishu.py`）：

1. **推荐**：飞书表格中的 `经度`、`纬度` 两列（数字或数字字符串，WGS84）；也兼容飞书原生「地理位置」列（自动解析其中的 `lng,lat`）。
2. `coords.json` 中的城市坐标。
3. **自动兜底**：以上都没有时，落在该省的省份中心（`PROVINCE_CENTROID`），并在日志中输出告警（`coordWarnings=N`），**同步不会失败**。

> 兜底坐标仅为近似位置。出现告警后请尽快按方式 1 补充精确坐标。
> 如需恢复「缺坐标即报错」的严格行为，运行 `python sync_feishu.py --strict`。
