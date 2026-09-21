# Windows 端副驾（只读 + 分析 + 悬浮窗 + 复制）

挂在微信 Windows 4.x 旁边的对话副驾：**窗口截图 → 视觉模型读屏 → Jev 判断 → 起草 3 条候选 → Jev 排序 → 悬浮窗展示 → 一键复制**。

**只读**：不 hook、不注入、不碰微信进程内存、不读它的数据库，只截自己屏幕上的窗口。
**不发送**：v1 只做到「复制」。连"填入输入框"都没做，更不会点发送。

## 跑起来

```bash
# 两个密钥都只从环境变量读，不落盘
export DEEPSEEK_API_KEY=...     # 感知（读屏）+ 起草
export JEV_API_KEY=...          # 判断 + 排序（TypeSafe 直连）

python desktop/app.py
```

可选参数：

```bash
python desktop/app.py --whitelist "妈妈,张三"   # 只分析标题含这些字样的会话，空=全部
python desktop/app.py --relationship "同事，催我交东西"  # 覆盖关系描述
python desktop/app.py --opacity 80              # 悬浮窗不透明度 35-100
python desktop/app.py --judge-provider openrouter_jev   # 临时换判断 provider
```

provider / 模型全部来自 `tools/jev/providers.json` 的 `roles`（judge / analysis / perception），换供应商只改配置。

## 依赖

`python -m pip install --target desktop/vendor --no-deps windows-capture`

装在仓库内的 `desktop/vendor`（C 盘只剩 2.6 GB，不往系统 site-packages 装）。其余依赖（`pywin32`、`Pillow`、`numpy`、`tkinter`）本机已有。

## 模块

| 文件 | 职责 |
|---|---|
| `area.py` | 消息区定位：像素锚点，不硬编码坐标（实测数据见 `docs/win/gate0_findings.md`） |
| `capture.py` | WGC 采集 + 像素门 + 静默门 + 窗口定位/重挂 |
| `perceive.py` | 内存图像 → 结构化对话（**不落盘**，base64 直传） |
| `engine.py` | 复用 `tools/ios-spike/pipeline.py` 的编排：判断 → 起草 → 排序，外加**会话隔离** |
| `overlay.py` | tkinter 半透明置顶悬浮窗（气泡 + 面板，两段式渲染） |
| `app.py` | 把上面几件串起来 |

## 两个设计要点

**成本闸门在本地。** 感知一次约 5 秒且要花钱，所以绝不能每帧都调：`capture.py` 只在**消息区像素真的变了**（`np.array_equal`，免费）且**稳定 0.3 秒**之后才往下走。静默期零调用。

**会话隔离不能靠标题精确匹配。** 视觉模型对群名的读取不稳定（实测同一次会话读出「硅基妙妙屋」和「硅基炒饭屋」），所以 `engine.py` 用归一化 + 子串 + 相似度 ≥0.60 判定，既能在真正切换会话时清空历史，又不会因误读反复清空。

## 已知限制

- **延迟 8.7~16.5s**，瓶颈是感知（思考型视觉模型把 700~900 token 花在推理上）。要更快得换非思考型视觉模型。
- **微信最小化时读不到**（WGC 对最小化窗口零帧）。App 只提示，不强制还原你的窗口。
- **群聊档案未校准**（`questions.json` 里 `group` 标着 `calibrated: false`），结论仅供参考；一对一档案已校准。
- 只在微信 **4.1.13.65 + 深色模式 + 默认主题**下测过；浅色/自定义背景/多显示器未验。

## 探针

`tools/win-spike/probe_capture.py`（采集通道 + 遮挡/最小化）、`tools/win-spike/probe_area.py`（消息区定位）。
两者都把"黑屏/退化定位"做成显式判定，避免把失败当成功。
