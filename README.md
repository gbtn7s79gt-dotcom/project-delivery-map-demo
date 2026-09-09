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

如果飞书表中新增城市导致同步失败，有两种解决方式：

1. **推荐**：在飞书表格中添加 `经度`、`纬度` 两列，填写该城市的 WGS84 坐标。
2. **兜底**：在 `coords.json` 中补充城市坐标后提交到仓库。
