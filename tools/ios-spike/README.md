# iOS 版采集层原型（tools/ios-spike）

安卓版靠无障碍节点树拿到「气泡文本 + 屏幕坐标」；iOS 没有这个能力。这个目录验证**换一种采集方式**是否可行：从一张聊天截图还原出「谁说的 + 说了什么」，再接到已经存在的 Jev 判断层上。

结论先写在这里：**感知层交给多模态视觉模型，本地 OCR 退为离线兜底。**依据见 `docs/ios/ocr_spike.md`。

## 快速开始

```bash
# 1) 配好密钥（只从环境变量读，绝不写进文件）
export JEV_API_KEY=...          # TypeSafe 直连，判断用
export DEEPSEEK_API_KEY=...     # 或 OPENROUTER_API_KEY，起草用
export OPENROUTER_API_KEY=...   # 视觉感知用

# 2) 看 provider 现状（哪些角色指向谁、密钥是否就绪）
python ../jev/providers.py

# 3) 离线跑：只做感知，不联网（本地 OCR + 启发式）
python pipeline.py --image shot.png --perceiver local --perceive-only

# 4) 全链路：截图 → Jev 判断 → 起草候选 → Jev 排序
python pipeline.py --image shot.png --perceiver vlm

# 5) 用视觉模型单独看感知结果
python vlm_extract.py --image shot.png
```

## provider 是可扩展的，不在代码里写死

端点、模型、密钥环境变量名全部在 `../jev/providers.json`。三个角色各自指向一个 provider：

| 角色 | 含义 | 默认指向 |
|---|---|---|
| `judge` | 7 道题的判断 | `typesafe`（`POST https://api.typesafe.ai/v1/systemone`，model `jev-latest`） |
| `analysis` | 起草 3 条候选回复 | `deepseek_official`（`https://api.deepseek.com`，model `deepseek-chat`） |
| `perception` | 截图感知 | `openrouter_vision`（`qwen/qwen3-vl-32b-instruct`） |

加一家 provider：在 `providers.json` 的 `providers` 里加一段（`kind` 取 `systemone` 或 `openai_chat`），再把某个角色指过去。**代码不用动**。可临时覆盖而不改配置：

```bash
python pipeline.py --image shot.png --judge-provider openrouter_jev
python pipeline.py --image shot.png --analysis-provider openrouter_chat --analysis-model deepseek/deepseek-chat-v3.1
python vlm_extract.py --image shot.png --model qwen/qwen3-vl-8b-instruct
```

`providers.json` 里**只写环境变量名**（`api_key_env`），真实密钥永远不落盘。

## 文件

| 文件 | 作用 |
|---|---|
| `extract.py` | 本地感知：OCR → 归属判定 → 过滤 → 折行合并。三种归属规则可选 |
| `synth.py` | 生成带 ground truth 的合成微信截图，用来量准确率 |
| `selftest.py` | 在合成集上量归属准确率与消息切分正确率 |
| `vlm_extract.py` | 视觉模型感知：截图 → 结构化对话 JSON（**保留每条消息的发言人**） |
| `pipeline.py` | 端到端串联：感知 → 选题目档案 → 判断 → 起草 → 排序 →（可选）写记忆 |

配套的 `tools/jev/` 侧：

| 文件 | 作用 |
|---|---|
| `providers.json` / `providers.py` | provider 注册表与传输层；加一家只改配置 |
| `questions.py` | 一对一 7 道题（已校准，与安卓 Kotlin 逐字一致） |
| `questions_group.py` | 群聊 7 道题（**尚未校准**）+ 群聊 state 构造（逐条带 sender） |
| `questions.json` | 两套档案的单一来源，由 `check_questions.py --write` 生成 |
| `check_questions.py` | 三端一致性校验（Kotlin / Python / JSON） |
| `calibrate.py` | 题目命中率与 danger_level 误差校准 |
| `consistency.py` | **跨题一致性**：测题目之间打不打架（`calibrate.py` 测不出这个） |
| `memory.py` | 群组/联系人长期记忆档案：蒸馏、按 key 覆盖、过期、按人分组注入 |

## 场景区分：群聊不是一对一

用户实际场景多为群聊，而一对一那套题目预设了"对方在测试你在不在乎"。实测：
标注集 30 条（全一对一）跨题矛盾 **0.0%**，真实群聊截图出现矛盾。所以：

- `pipeline.py` 按感知层的 `is_group` 自动切换题目档案，输出里标明是否已校准
- 群聊 state 里每条消息带 `sender`，并有 `senders` 列表与 `distinct_speakers` 计数
  —— 判断层因此能知道"这个问题是 A 问的、B 已经答了"，而不是把群成员当成一个人
- 群聊题目集目前**未经校准**，结论仅供参考；修它需要群聊标注数据（见 `docs/ios/product_memory.md`）

## 常用命令

```bash
export JEV_API_KEY=...          # 判断（TypeSafe 直连）
export DEEPSEEK_API_KEY=...     # 感知 + 起草（DeepSeek）

python ../jev/providers.py                                    # 看 provider 与密钥就绪状态
python pipeline.py --image shot.png --perceive-only --perceiver local   # 离线只看感知
python pipeline.py --image shot.png                           # 全链路（自动选档案）
python pipeline.py --image shot.png --group --memory-distill   # 强制群聊 + 写记忆档案
python ../jev/memory.py --list                                # 看记忆档案
python ../jev/memory.py --show "群名"                          # 看某个档案的条目与出处
python ../jev/consistency.py --profile one_on_one              # 跨题一致性
```

## 归属规则的实测数据（合成集 6 例，21 行文字）

| 规则 | 归属准确率 | 消息条数正确 |
|---|---|---|
| `center`（照搬安卓 `cx > 屏宽/2`） | 96.3% | 5/6 |
| `edge`（贴边优先，退回中心） | 96.3% | 5/6 |
| `color`（看气泡底色，绿=我 / 白=对方） | **100%** | **6/6** |

合成集上 `color` 全对；几何法唯一失败的是**折行长行越过中线**（安卓拿的是气泡 bounds，OCR 只能拿到文字行的框，所以这个"直接沿用"不成立）。

## 本地感知在真实截图上的已知失败

真实截图（手机版 + 桌面深色版）实测，本地启发式有四类问题，详见 `docs/ios/ocr_spike.md`：

1. 界面上的浮层文字会被当成聊天内容（安卓那张截图 7 条里 5 条是 Jev 面板自己的文字）
2. 截图底部的最新消息会被边界带误删（已修：改为检测到输入栏才裁底）
3. 群聊昵称被当成消息，还会和正文粘成一条（桌面截图 10 条里 4 条是昵称）
4. 深色模式没有颜色线索；被遮挡的半透明文字读不到；emoji 丢失

## 注意

- `out/` 是输出目录，含真实聊天截图，**已在 .gitignore 里排除**，不要提交。
- 合成图上的文字识别率不代表 iOS Vision 的表现（渲染字体不同）。归属准确率的结论不受影响。
