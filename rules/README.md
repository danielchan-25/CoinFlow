# CoinFlow 规则库

所有可调行为都在这里，代码不写死参数。规则按 JSON 文件组织，**追加即新增文件或新增键，删除即删文件或删键**。

## 目录

```
rules/
├── core/
│   └── 01_logging.json    日志开关、目录、OCR 预览行数
├── human/                 拟人化参数（默认全部生效）
│   ├── 01_rhythm.json     手速/耐心/停留/点击间隔/轮询/修正确认窗口
│   ├── 02_touch.json      点击偏移/长按/滑动几何/反向滑动/抖动
│   ├── 03_consume.json    观看时长/回看/评论区阅读/评论点赞/点赞收藏
│   ├── 04_volume.json     媒体音量区间（默认 5%–10%）
│   ├── 05_tasks.json      逛街停留/广告收尾/抢红包连点频率
│   └── 06_after_task.json 任务完成后延迟退出（默认 5–10 秒）
├── safety/                安全规则（任何应用都生效）
│   └── 01_denylist.json   危险按钮正则 + 永不点击文案
└── apps/                  应用专属参数
    ├── soda_music.json    汽水音乐：任务顺序、归一化坐标、已知坑位
    ├── douyin.json        抖音极速版
    └── kuaishou.json      快手极速版
```

## 文件格式

```json
{
  "id": "human.rhythm",
  "description": "说明",
  "priority": 100,
  "values": {
    "human.rhythm.speed.range": [0.85, 1.2]
  },
  "schema": {
    "human.rhythm.speed.range": {"type": "range", "min": 0.5, "max": 2.0}
  }
}
```

- `priority` 越小越先合并；同优先级按文件路径排序，**后加载覆盖先加载**。
- `values` 的键是点分路径，代码用 `rules.get("human.rhythm.speed.range")` 读取。
- `schema` 可选，用于 `validate` 校验（`range` / `point` / `number` / `integer` / `string` / `boolean` / `list`）。
- 应用覆盖：`rules/apps/<app>.json` 里用同样的 `human.*` 键即可覆盖全局拟人参数。

## 常用命令

```bash
python3 -m droidfleet.rules list                 # 列出文件
python3 -m droidfleet.rules validate             # 校验
python3 -m droidfleet.rules get human.volume.high --app soda_music
python3 -m droidfleet.rules dump --app soda_music

# 追加/覆盖一个键（写入指定文件）
python3 -m droidfleet.rules set human.volume.high 0.08 --file human/04_volume.json

# 删除一个键
python3 -m droidfleet.rules unset human.volume.high --file human/04_volume.json

# 新增一个规则文件（之后用 set 往里加键）
python3 -m droidfleet.rules add-file human/06_night.json --id human.night --description "夜间节奏"

# 删除整个规则文件
python3 -m droidfleet.rules remove-file human/06_night.json
```

规则文件改动后自动生效（加载器按文件 mtime 失效缓存），长跑进程可用 `load_rules(reload=True)` 强制刷新。

## 公用操作规则

汽水音乐、抖音极速版和快手极速版在每次点击、滑动和等待时使用 `HumanProfile` 抽样。点击范围来自当前截图或控件边界，滑动在应用指定的屏幕区域内选择起点、终点、距离和时长。任务倒计时与预算由应用流程控制。

- 汽水音乐 `tap PATTERN --expect REGEX` 用新出现的 OCR 文案确认成功。只有「去逛街」「返回顶部」「关闭弹窗」在画面持续未变时可以重新定位并重试一次。
- 抖音在「去逛街」和「去看视频」入口核对任务卡、余额和进度。快手在「去观看」和「去浏览」入口核对前台页面和任务中心。连续识别到原状态才允许重新定位并重试一次。
- 领取、签到和点赞按页面结果核对。状态不明时不使用自动纠错重试。实际设备上的纠错由观测到的点空触发。
- **任务收尾**：广告、逛街、指定视频、直播任务确认完成后，先按 `human.after_task.exit_delay` 随机停留 5–10 秒，再离开页面，模拟注意力分散。离开时按「页面上的返回/退出控件 → 本地 AI 指认 → 系统返回键」三级回退：AI 给的坐标必须在当前 OCR 里对回同一行文字；对不上时，只有点位附近没有领奖/支付类文案才允许盲点，点击后再用截图确认页面真的变了。`ai_fallback` / `ai_blind_tap` / `ai_verify_seconds` 可分别关闭 AI、关闭盲点和调整复核等待。恢复、关弹窗、导航重试不受这一延迟影响。

### 页面稳定、列表查找和中断恢复

| 规则 | 默认值与触发条件 |
|---|---|
| 页面稳定 | 连续两次识别同一目标，边界允许约 2 像素识别误差。采样间隔 0.25–0.55 秒，稳定观察窗口 3 秒。汽水音乐的 `tapx` 要求两次截图一致。 |
| 按目标滑动 | 找不到目标时使用屏高的 32%–44%，看到目标靠边时用 17%–25%，滑过头时用 8%–14% 反向修正。抽样后按会话滑动系数调整，受应用内容区边界约束。 |
| 滑动停滞 | 连续两次观察到同一列表内容时结束当前查找。汽水音乐使用 `scroll down --until REGEX --times N` 按目标查找，普通 `scroll` 保留按次数执行。 |
| 中断恢复 | 发生短暂前台切换或设备错误时，在 6 秒窗口内轮询重读。汽水音乐的 `tap --expect` 核对新文案，抖音按当前倒计时、任务进度或金币余额处理，快手记录当前任务和金币余额并暂缓状态不明的任务。恢复阶段只重读页面，避免重放刚才的输入。 |

## 编写约定

1. 拟人参数必须写成**分布参数**（区间、均值、形状），不要写固定阈值。
2. 安全规则只拦截"危险动作"，不要包含"领取/去逛街"等正常任务词。
3. 应用坐标统一用**归一化逻辑坐标**（0–1，基于 1080×2400 逻辑分辨率）。
4. 新增键如果会影响金额/账号安全，先在 `safety/` 里加拦截规则。
