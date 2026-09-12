# AstrBot 偶像大师 LIVE 图片日历

群内提供两个查询命令：

```text
/imaslive
/imaslive 1—12
/imaslive next [imas/cg/ml/sidem/sc/gk]
/imaslive enable|disable
/imasticket
/imasticket get <活动编号>
/imasticket enable|disable
```

在 AstrBot 插件管理中使用仓库地址 `https://github.com/huvz04/astrbot_plugin_imas_live` 安装或更新。插件启动、安装和热重载后都会自动同步，第一次查询会等待活动目录就绪（最多 40 秒，后台同步继续运行）。未同步完成时图片会明确提示，不会把同步失败解释成没有活动。

`/imaslive` 会生成标题为 `IM@S LIVE! · Next 30 Days` 的完整 PNG 长图，只展示从今天 00:00 起未来 30 个自然日内的演出。`/imaslive 1—12` 查询最近一次对应月份的完整自然月。每场活动有稳定的 `#编号`，可供 `/imasticket get <编号>` 查询。

`/imasticket` 只展示当前正在开放、来源新鲜且已核验的 `onsite + lottery` 轮次；不把未开始、已结束、缺少起止时间或陈旧来源画成开放。剩余不超过 24 小时为淡红，超过 24 小时为淡绿；没有合格轮次仍返回空状态 PNG。`get` 可查看历史活动，明确区分已结束抽选、一般销售和官方转售。

这不是票务代办工具。插件不会登录票务站、查询个人中签/付款/库存，也不会把先到先得、转售、配信或物販整理券当作抽选通知或截止提醒。

## 图片与数据口径

- 六家企划沿用 birthday 插件的色彩基线，并显示紧凑的官方英文品牌名，例如 `Gakuen`、`Shiny Colors`、`Million Live!`；多企划归属的官方合同活动使用粉色。未知品牌使用灰色，不猜成合同。
- 场次优先显示专题页中已解析的场地；没有场次级场地时，回退使用官网活动目录的 `event_place` 字段。
- 同日昼夜场会各自保留；长标题自动换行，按内容自动增加图片高度，完整展示全部日期，不分页、不裁切或缩小字体。
- LIVE 图不混入抽票卡。抽票图中，24 小时内截止为淡红色“24小时内截止”，正在抽选为淡绿色“正在抽选”；未核验信息不会列为开放卡。
- 抽票卡标出活动 `#编号`、轮次、含年份的显示时区与 JST 开始/截止时间；已知的演出日期和场地会附在卡内。图片和全部官方链接以同一条消息链发送，图片在前。
- 官网专题成功核验后，如已有 LIVE 的稳定 `onsite + lottery` 轮次新增，或完整活动目录首次发现的新 LIVE 首次公布了抽选，会向已执行 `/imaslive enable` 的群发送“新增现场抽选”图文公告。公告含活动编号、活动名、轮次、北京时间/JST 起止时间、当前开放状态与官方申请链接；尚未开始也会公告，已结束轮次不发。
- `/imaslive next` 会返回最近尚未开始的场次及该日的官方出演资料。若专题页在“出演者”区实际提供完整名单图（例如 Million 14th 的 `bnr_day1.webp`/`bnr_day2.webp`），插件会校验并缓存官方原图后随详情发送；否则本地渲染已解析的官方文字名单，不从角色资料反推声优出演。
- 没有记录时仍会生成清晰的空状态图片。
- 本地 Pillow 渲染，自动寻找 Windows 微软雅黑/游ゴシック或 Linux Noto CJK/文泉驿。Linux/Docker 环境需要安装中日文字体，例如 Debian/Ubuntu 的 `fonts-noto-cjk`，也可将字体挂载到容器内并配置 `font_path`。无需浏览器。

## WebUI 配置

群管理员或 AstrBot 管理员可在群内单独设置 `/imaslive enable|disable` 和 `/imasticket enable|disable`；私聊不会设置订阅。`/imaslive enable` 同时控制 LIVE 开演前一小时提醒和新增现场抽选公告；`/imasticket enable` 只控制抽票截止前 24 小时与 1 小时提醒。两种订阅分别按完整 UMO 保存，互不影响。

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `enabled` | `true` | 官网后台同步与自动提醒总开关；查询仍可读取缓存。 |
| `white_umos` | `[]` | 旧版抽票白名单，仅首次迁移到抽票订阅；绝不隐式开启 LIVE。 |
| `live_reminder_enabled` | `true` | 开关 LIVE 开演提醒。 |
| `live_reminder_before_hours` | `1` | 官网明确开演时间换算到北京时间后，提前一小时提醒。 |
| `ticket_new_announcement_enabled` | `true` | 开关官网新增的现场抽选公告，发送到 LIVE 订阅群。 |
| `reminder_enabled` | `true` | 开关现场抽选截止提醒。 |
| `ticket_reminder_hours` | `[24, 1]` | 抽票截止前 24 小时和 1 小时各提醒一次。 |
| `brands` | `[]` | 图片和提醒的企划代码筛选；空表示全部。 |
| `display_timezone` | `Asia/Shanghai` | 图片主展示时区。 |
| `font_path` | 空 | 自定义中日文字体文件。 |

提醒检查每 5 分钟独立运行，不等待较慢的官网同步。LIVE 仅对官网给出明确时区和开演时间的场次触发；官网常用 JST 会先转换成北京时间再按绝对时刻计算。新增抽选公告只比较已经成功核验过的同一专题来源中的稳定轮次；首次安装、升级后的首次成功核验、分批首次覆盖旧专题，以及群晚开启/重新开启都只建立基线，绝不回放历史轮次。抽票截止提醒仅限已开始、来源新鲜的 `onsite + lottery`；24 小时和 1 小时节点独立去重，晚开启不会补发错过的节点。

新增抽选公告使用独立的 `ticket_new` 待发送记录，按群和稳定轮次去重，不与 24 小时/1 小时截止提醒或 LIVE 开演提醒混用；标题、时间或链接的小修不会当作新轮次。每群、每个实际截止时间和提醒节点最多成功发送一次；LIVE 按群、场次和开演时刻去重。失败投递可重试、重启可恢复，关闭订阅或轮次结束后不再发送。外部消息与本地数据库无法构成单一原子事务，因此不会声称严格 exactly-once。

## 来源与降级

官网匿名 CMS 令牌仅在内存中使用，不写入日志或 SQLite。默认每 6 小时完整刷新活动目录，每小时轮换核验最多 12 个专题，并跟随专题内真实的日程、票务链接。当前及日期未明确的活动优先于已结束活动。目录中明确列举的演出日可先展示；连续日期范围不会擅自展开为每日演出。

插件保存结构化事实、短证据片段、内容 hash 与官方链接。抓取失败保留旧缓存，并暂停该来源的提醒；其他来源成功不会使失败来源被当作最新数据。完整目录异常或分页不全也不会覆盖为成功空结果。初次目录就绪后，票务信息还会继续补齐。

前端快照已核实 `detail_page → /live_events/{url_name}`、`lp_detail → /lp/{path}` 的公开跳转。动态新闻正文及上述动态路由的通用 CMS 正文接口尚未验证，因此不会猜测 `Content/get` 等端点；未能解析的来源保留为候选/待核验，而非虚构日期或截止时间。

## 本地验证与预览

在仓库根目录运行（需 Python 3.10+ 和可用的中日文字体）：

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

离线样本包含在 `tests/fixtures`。测试涵盖专题链接遍历、多段落多日演出、完整目录保存、令牌刷新、异常分页、首次查询等待、热重载、月份跨年/闰年、30 天边界、现场抽选状态与来源时效、图片排版，以及新增抽选的来源基线、群路由、晚开启、去重和失败重试。

已导出并视觉检查的 PNG 预览：

- [默认未来 30 天 LIVE](previews/live-default.png)
- [指定月份 LIVE](previews/live-october.png)
- [抽票状态卡](previews/ticket-statuses.png)
- [抽票空状态](previews/ticket-empty.png)
- [新增现场抽选公告](previews/ticket-new-announcement.png)
- [截止提醒卡](previews/deadline-reminder.png)（模拟进入截止前 60 分钟）

没有真实 AstrBot/QQ 运行环境时，命令注册、真实图片发送和平台主动推送不被宣称为端到端已验证。
