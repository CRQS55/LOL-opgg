# ⚔️ LOL 克制查询 & 英雄推荐 & 符文出装 & AI 教练

基于 OP.GG 数据的英雄联盟辅助工具，单文件网页，也可一键启动本地服务器使用 AI 教练功能。

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 🎯 **克制查询** | 选分路 → 选对方英雄 → 查看 Top5 克制英雄（多地区/段位） |
| 🏆 **英雄推荐** | 选分路 → 查看该位置胜率排名（从真实对局数据推导） |
| 📜 **符文出装** | 任意英雄独立查询，查看符文/召唤师技能/加点/装备/连招 |
| 🤖 **AI 教练** | 选双方阵容 → DeepSeek 大模型分析对局策略 |
| 🌐 **多地区/段位** | 支持全球/韩国/中国，钻石/大师/宗师/王者 |

## 📦 快速开始（使用）

### 方式一：双击即用（基础功能）

直接双击 `index.html`，**克制查询、英雄推荐、符文出装**均可使用。

> AI 教练需要方式二。

### 方式二：一键启动（全部功能）

```bash
# 1. 安装 Python（如已安装跳过）
#    Microsoft Store 搜索 "Python 3.12" 安装
#    或 https://python.org/downloads/ 下载（勾选 "Add Python to PATH"）

# 2. 启动
python server.py
# → 浏览器自动打开 http://localhost:8899
```

无需安装任何第三方库，`server.py` 仅使用 Python 标准库。

## 📁 文件说明

| 文件 | 作用 |
|------|------|
| `index.html` | **成品**（单文件，可直接使用） |
| `server.py` | 本地服务器（静态文件 + AI API 代理） |
| `scrape.py` | OP.GG 数据抓取（Playwright） |
| `build.py` | 构建脚本（注入数据 → 生成 index.html） |
| `gen_combos.py` | AI 生成连招（DeepSeek API） |
| `gen_tier.py` | 从对局数据推导英雄排名 |
| `trans_combos.py` | 连招翻译为简体中文 |
| `template.html` | HTML 骨架 |
| `app.js` | 前端逻辑 |
| `lanes.txt` | 分路英雄名单 |
| `proxy.py` | 独立 CORS 代理（已废弃，用 server.py 替代） |

### 数据文件

| 文件 | 说明 |
|------|------|
| `champions.json` | Data Dragon 英雄映射（172 英雄，中英文） |
| `meta.json` | 物品/符文/召唤师技能图标 |
| `counter_data.json` | 克制数据（~17000 条对局记录） |
| `build_data.json` | 符文/出装/加点/连招 |
| `tier_data.json` | 英雄排名（从 counter 数据推导） |

## 🔧 开发（数据更新）

LOL 版本更新后，需要重新抓取数据：

```bash
# 1. 安装依赖
pip install requests playwright
playwright install chromium

# 2. 抓取 OP.GG 数据（约 15-30 分钟）
python scrape.py --fresh

# 3. 推导英雄排名
python gen_tier.py

# 4. 生成 AI 连招（约 5 分钟）
python gen_combos.py

# 5. 翻译连招为中文（约 5 分钟）
python trans_combos.py

# 6. 构建成品
python build.py
```

### 抓取选项

```bash
python scrape.py --debug ashe     # 单英雄调试
python scrape.py --limit 3        # 小范围验证
python scrape.py --fresh          # 全新抓取（忽略进度）
python scrape.py --full           # 全量模式（所有地区+段位）
```

## 🛠️ 技术架构

```
OP.GG (Playwright 抓取)         Data Dragon (Riot API)
        │                              │
        ▼                              ▼
  counter_data.json              champions.json
  build_data.json                meta.json
        │                              │
        └──────────┬───────────────────┘
                   ▼
              build.py
                   │
                   ▼
             index.html  ←── gen_combos.py (DeepSeek API 连招)
                           ←── gen_tier.py  (排名推导)
                           ←── trans_combos.py (连招翻译)
                   │
                   ▼
             server.py (本地服务器)
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
   静态文件服务          API 代理 (/v1/messages)
        │                     │
        ▼                     ▼
     浏览器               DeepSeek API
```

## 📄 数据来源

- 克制/出装数据：OP.GG（`op.gg`）
- 英雄/物品/符文元数据：Riot Data Dragon（`ddragon.leagueoflegends.com`）
- AI 连招/教练：DeepSeek API（`api.deepseek.com`）
- 英雄头像：OP.GG CDN

## ⚠️ 注意事项

- 数据有版本时效性，LOL 更新后需重新抓取
- OP.GG 使用 Cloudflare 防护，必须用 Playwright 真实浏览器抓取
- AI 教练功能需要 `server.py` 运行中
- API Key 配置在 `.env` 文件中（`DEEPSEEK_API_KEY=...`），由 `server.py`/Python 脚本自动读取
- 参考 `.env.example` 创建你的 `.env` 文件

## 📜 License

仅供学习交流使用。数据版权归 OP.GG 和 Riot Games 所有。
