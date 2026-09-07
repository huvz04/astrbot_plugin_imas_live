# AstrBot 偶像大师 LIVE 图片日历

群内只提供一个命令：

```text
/imaslive
```

可附群开关参数：`/imaslive enable` 开启当前群，`/imaslive disable` 关闭当前群。状态按群 UMO 持久保存在插件数据库；关闭后该群不接收截止提醒、也不能输出日历，重启后仍生效。官网后台同步不受单个群开关影响。

它会生成真实 PNG 图片，展示按北京时间从今天起 30 个自然日的纵向日期列表。演出日和已核验的现场抽选截止日分别进入同一滚动窗口：即使演出较远，只要抽选截止落在这 30 天内也会显示。

这不是票务代办工具。插件不会登录票务站、查询个人中签/付款/库存，也不会把先到先得、转售、配信或物販整理券当作抽选截止提醒。

## 图片与数据口径

- 六家企划沿用 birthday 插件的色彩基线，并显示紧凑的官方英文品牌名，例如 `Gakuen`、`Shiny Colors`、`Million Live!`；多企划归属的官方合同活动使用粉色。未知品牌使用灰色，不猜成合同。
- 场次优先显示专题页中已解析的场地；没有场次级场地时，回退使用官网活动目录的 `event_place` 字段。
- 同日昼夜场会各自保留；长标题自动换行，跨月有标记，多页会分页而不裁切或无限缩小字体。
- 截止卡标出活动、轮次及含年份的北京时间。演出日期缺失或待核验时不会编造。
- 没有记录时仍会生成清晰的空状态图片。
- 本地 Pillow 渲染，默认尝试 Windows 的微软雅黑/游ゴシック；可以用 `font_path` 覆盖。无需浏览器和图像生成模型。

## WebUI 配置

除总开关外，所有自动行为都放在插件 WebUI，不提供订阅、刷新、统计等群子命令。

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `enabled` | `true` | 官网后台同步开关，也是未设置群开关时的默认群状态。 |
| `white_umos` | `[]` | 自动提醒的明确 UMO 白名单；空列表绝不发送。 |
| `reminder_enabled` | `true` | 开关现场抽选截止提醒。 |
| `reminder_before_minutes` | `60` | 距截止多少分钟进入提醒窗口。 |
| `brands` | `[]` | 图片和提醒的企划代码筛选；空表示全部。 |
| `display_timezone` | `Asia/Shanghai` | 图片主展示时区。 |
| `font_path` | 空 | 自定义中日文字体文件。 |

提醒检查每 30 秒独立运行，不等待较慢的官网同步。仅在轮次为 `onsite + lottery`、起售已开始、截止时间明确、数据未陈旧，且满足 `deadline - reminder_before_minutes <= now < deadline` 时触发。

每群、每稳定轮次、每个实际截止时间最多成功发送一次。更改提前量、重复查询和重启不会重发；官网把截止时间改为新值后，新截止进入窗口时可再提醒一次。失败投递保留为可重试，白名单移除、停用提醒或截止已过时不再发送。外部消息与本地数据库无法构成单一原子事务，因此不会声称严格 exactly-once。

## 来源与降级

官网匿名 CMS 令牌仅在内存中使用，不写入日志或 SQLite。插件使用公开目录和可直接获取的专题页，并保存必要的结构化事实、短证据片段、内容 hash 与官方链接；失败时保留旧缓存。

前端快照已核实 `detail_page → /live_events/{url_name}`、`lp_detail → /lp/{path}` 的公开跳转。动态新闻正文及上述动态路由的通用 CMS 正文接口尚未验证，因此不会猜测 `Content/get` 等端点；未能解析的来源保留为候选/待核验，而非虚构日期或截止时间。

## 本地验证与预览

```powershell
$env:PYTHONPATH = (Resolve-Path .\astrbot_plugin_imas_live).Path
python -m unittest discover -s .\astrbot_plugin_imas_live\tests -v
```

离线测试涵盖三种官方专题、30 天边界、远期演出的截止条目、合同色、空状态、长标题、60/30 分钟截止窗口、重启/重复检查去重、失败重试、白名单移除和截止改期版本。

已导出并视觉检查的 PNG 预览：

- [普通日历](previews/normal-calendar.png)
- [长标题、合同和跨月](previews/long-cross-month.png)
- [空状态](previews/empty-calendar.png)
- [截止提醒卡](previews/deadline-reminder.png)

没有真实 AstrBot/QQ 运行环境时，命令注册、真实图片发送和平台主动推送不被宣称为端到端已验证。
