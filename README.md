# 考编公告聚合站 · Job Aggregator（示例展示）

> 一个由 AI 辅助独立完成的"考编公告聚合站"示例：自动抓取多个官方渠道的招聘公告，按用户画像自动核对"能否投递"，把"海量信息"过滤成"只看符合硬条件的岗位"。

本仓库是**去个人化的示例展示版**：站点配置与筛选规则由 [job-aggregator-skill](https://github.com/lynnlueluelue/job-aggregator-skill) 的可复用模板包生成，画像为示例数据（24 岁 · 本科 · 法学 · 广东/深圳）。运行下方命令即可在本地跑起来，也可用该 skill 生成**属于你自己的**画像版本。

## 核心亮点

1. **画像核对决策引擎**：所有筛选规则由 `USER_PROFILE`（年龄/学历/专业/政治面貌/求职单位偏好）驱动，逐条硬核对并输出"为什么能投/不能投"，**宁可标"未解析"也不误杀**；
2. **结构化信号升级专业/学历判定**：高校人才网职位列表页含每行的**学历 + 需求专业**列，比正文精准——有硕士岗即解除标题"博士"误排、需求专业直接定级对口/相关/不符；
3. **免费替代付费工具**：本地离线运行 + 每 3 小时自动刷新 + 桌面通知，且多了商业工具没有的跨渠道去重、自定义追踪指定考试结果公示、按画像自动核对；
4. **全栈闭环**：Python + Flask + 原生 JS（零框架单文件前端），开机自启、离线可用；
5. **设计系统化**：token 化 + WCAG 对比度 + 静态稿先行，沉淀七步 UI 优化 SOP（详见 `docs/`）。

## 技术栈

Python 3.12 · Flask · requests · BeautifulSoup (lxml) · pdfplumber · plyer · 原生 JS

## 架构流程

```
渠道列表 → 内容判定(岗位/公告/公示) → 报考条件核对(年龄/党员/专业/招聘对象)
        → 附件岗位表兜底 → 硬排除 → 跨渠道去重排序 → 前端渲染
```

## 快速运行

```bash
pip install -r requirements.txt
python crawler.py    # 抓取 + 判定 + 核对 + 去重
python server.py     # 启动本地服务，浏览器打开 http://localhost:8765
```

## 目录结构

```
config.py             站点配置与筛选规则（改这里即可增删渠道/关键词）
crawler.py            抓取 / 内容判定 / 画像核对 / 附件解析 / 去重
server.py             本地 Flask 服务 + 桌面通知
index.html            单文件前端（原生 JS，零框架）
preview_stitch.html   Stitch 静态稿对照页（UI 优化 SOP 产物）
docs/                 方案与设计文档（"文档是第二产品"）
  └─ architecture.md    系统架构与筛选流程
  └─ ui-design.md       设计系统与 UI 优化 SOP
  └─ component-map.md   页面构件名称对照表
  └─ case-study.md      项目案例页
```

## 如何做成你自己的版本

本示例由 [job-aggregator-skill](https://github.com/lynnlueluelue/job-aggregator-skill) 生成：

1. 克隆该 skill 模板包，运行问卷脚本填写你的画像（年龄/学历/专业/目标地区等）；
2. 一键生成属于你的 `config.py` 与 `index.html`（全程本地运行，画像不离开你的电脑）；
3. 运行 `privacy_check.py` 校验零个人数据残留，再自行部署。

---

*注：`data/*.json` 为运行时数据（含画像筛选记录），不入库。*
