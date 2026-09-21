"""探针 R1：Windows Graphics Capture 能不能截到微信窗口的真实像素。

为什么先验这一条：安卓版靠无障碍节点树拿文字，Windows 没有这条路可用
（微信 4.x 是 Qt + MMUI 自绘，UIA 树只有两个节点——本项目探针与参考项目
rezoch340/jev-chat-JARVIS-windows 的 probe_win*.py 独立得到同一结论）。
所以采集只能走窗口截图。而 BitBlt / ImageGrab 截的是**屏幕像素**，窗口被遮挡
就停摆；WGC 截的是窗口自己的合成表面，理论上遮挡也能出帧。这条要实测。

参考项目的探针都没验"遮挡时是否出帧"，README 自己把 PrintWindow 标为未验证。
本探针把三种状态都验掉，并把"近黑/零方差"判定做进结果，避免把黑屏当成成功。

不擅自动别人的窗口：最小化测试要显式 --minimize-test 才会做，且做完还原原状。

用法:
  python probe_capture.py --list                     # 列出候选窗口
  python probe_capture.py --capture 3                # 截 3 秒，报帧统计
  python probe_capture.py --capture 3 --save         # 顺带存一帧 PNG 供人工核对
  python probe_capture.py --capture 2 --minimize-test  # 测最小化时是否出帧（会还原）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 中文 Windows 控制台默认 CP936，中文输出会乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
VENDOR = REPO / "desktop" / "vendor"      # 依赖装在 D 盘，不占 C 盘
OUT = SPIKE / "out"

if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

# 微信 4.x 的窗口类名（本项目与参考项目实测一致）
WECHAT_CLASSES = ("Qt51514QWindowIcon", "WeChatMainWndForPC", "ChatWnd")


# ---------------------------------------------------------------- 窗口枚举


def _exe_of(hwnd: int) -> str:
    """窗口所属进程的 exe 名。只读进程元信息，不需要提权、不碰目标进程内存。"""
    import win32api
    import win32process

    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        h = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            return win32process.GetModuleFileNameEx(h, 0)
        finally:
            win32api.CloseHandle(h)
    except Exception:
        return ""


def list_windows(verbose: bool = False) -> list[dict]:
    """所有顶层窗口，附带类名/标题/矩形/是否可见/是否最小化/exe。"""
    import win32gui

    out: list[dict] = []
    wechat: list[dict] = []

    def cb(hwnd, _):
        if not win32gui.IsWindow(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        exe = _exe_of(hwnd)
        info = {
            "hwnd": hwnd,
            "class": cls,
            "title": title,
            "rect": rect,
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
            "minimized": bool(win32gui.IsIconic(hwnd)),
            "exe": Path(exe).name if exe else "",
            "exe_path": exe,
        }
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        info["size"] = (w, h)
        if "weixin" in info["exe"].lower() or "wechat" in info["exe"].lower() or cls in WECHAT_CLASSES:
            wechat.append(info)
        if verbose:
            out.append(info)
        return True

    win32gui.EnumWindows(cb, None)
    return out if verbose else wechat


def pick_wechat() -> dict | None:
    """挑主窗口：优先 exe=Weixin.exe 且类名是 Qt 的那个（其它是辅助/子进程窗口）。"""
    cands = list_windows()
    if not cands:
        return None

    def score(w: dict) -> tuple:
        main = 1 if w["class"] in WECHAT_CLASSES else 0
        vis = 1 if w["visible"] else 0
        area = max(0, w["size"][0]) * max(0, w["size"][1])
        return (main, vis, area)

    return sorted(cands, key=score, reverse=True)[0]


# ---------------------------------------------------------------- 帧统计


def frame_stats(buf) -> dict:
    """帧的健康度判定。黑屏/纯色/无内容必须能被识别出来，否则会把失败当成功。"""
    import numpy as np

    a = np.asarray(buf)
    rgb = a[:, :, :3].astype(np.float32)
    mean = float(rgb.mean())
    std = float(rgb.std())
    # 唯一行数：纯色/黑屏时接近 1；有内容的界面通常几十以上
    uniq_rows = int(np.unique(rgb.reshape(-1, 3).view([("r", "f4"), ("g", "f4"), ("b", "f4")])).shape[0])
    near_black = float((rgb.max(axis=2) < 12).mean())
    verdict = "ok"
    if near_black > 0.97:
        verdict = "全黑（PrintWindow 类失败或窗口未渲染）"
    elif std < 1.5:
        verdict = "近似纯色（无内容）"
    elif uniq_rows < 5:
        verdict = "色彩极单一（可疑）"
    return {
        "shape": tuple(a.shape),
        "mean": round(mean, 2),
        "std": round(std, 2),
        "uniq_colors": uniq_rows,
        "near_black_frac": round(near_black, 4),
        "verdict": verdict,
    }


# ---------------------------------------------------------------- WGC 采集


def capture_window(hwnd: int, seconds: float, save: bool, draw_border: bool | None = False,
                   save_name: str = "") -> dict:
    """用 WGC 采一段时间，返回帧统计与耗时。"""
    from windows_capture import WindowsCapture

    from PIL import Image
    import numpy as np

    got: list[dict] = []
    errors: list[str] = []
    t0 = time.time()
    first: dict | None = None
    n_stats = 0

    cap = WindowsCapture(window_hwnd=hwnd, draw_border=draw_border)

    @cap.event
    def on_frame_arrived(frame, capture_control):  # noqa: ANN001
        nonlocal first, n_stats
        if first is None:
            first = {"first_frame_at": time.time() - t0}
        if n_stats < 3:
            n_stats += 1
            got.append(frame_stats(frame.frame_buffer))
        if save and "saved" not in first:
            OUT.mkdir(parents=True, exist_ok=True)
            p = OUT / (save_name or f"capture_{hwnd}.png")
            # 用 PIL 存，避免依赖 cv2（--no-deps 装的 windows-capture）
            Image.fromarray(np.asarray(frame.frame_buffer)[:, :, :3][:, :, ::-1]).save(p)
            first["saved"] = str(p)

    @cap.event
    def on_closed():
        pass

    # 必须用 start_free_threaded + 自己计时停止：最小化/未渲染的窗口不会有帧回调，
    # 用阻塞的 start() 会永久挂住（这是最小化测试里最容易踩的坑）。
    control = None
    try:
        control = cap.start_free_threaded()
        deadline = t0 + seconds
        while time.time() < deadline and not control.is_finished():
            time.sleep(0.05)
        if not control.is_finished():
            control.stop()
        control.wait()
    except Exception as exc:  # 采集不可用时给出可读原因
        errors.append(f"{type(exc).__name__}: {exc}")
        if control is not None:
            try:
                control.stop()
            except Exception:
                pass

    elapsed = time.time() - t0
    frames = list(got)
    return {
        "elapsed_s": round(elapsed, 2),
        "n_frames_sampled": len(frames),
        "first_frame_s": round(first["first_frame_at"], 3) if first and "first_frame_at" in first else None,
        "saved": first.get("saved") if first else None,
        "errors": errors,
        "samples": frames,
    }


def occlude_test(hwnd: int, seconds: float = 3.0) -> int:
    """用纯色窗口盖住微信，再采一次，判断 WGC 拿的是窗口表面还是屏幕像素。

    判定依据：如果 WGC 截的是"窗口自己的合成表面"，盖住后仍应拿到聊天内容；
    如果截的是屏幕像素（BitBlt 那一类），盖住后整帧会变成遮挡色。
    参考项目始终没验这条，而它决定"副驾能不能在微信被挡在后面时继续工作"。
    """
    import tkinter as tk

    import win32gui

    from PIL import Image
    import numpy as np

    OCC = (255, 0, 170)  # 醒目的洋红，绝不会和微信界面混淆
    r = win32gui.GetWindowRect(hwnd)
    w, h = r[2] - r[0], r[3] - r[1]

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.geometry(f"{w}x{h}+{r[0]}+{r[1]}")
    cv = tk.Canvas(root, bg="#FF00AA", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    cv.create_text(w // 2, h // 2, text="OCCLUSION TEST", fill="#FFFFFF",
                   font=("Arial", max(16, h // 20), "bold"))
    root.update()
    time.sleep(0.8)  # 让遮挡窗口真正上屏

    print(f"已用 {w}x{h} 的纯色窗口盖住微信（颜色 #FF00AA），开始采集…")

    saved_path = None
    try:
        res = capture_window(hwnd, seconds, save=True, draw_border=False,
                             save_name=f"occluded_{hwnd}.png")
        saved_path = res["saved"]
    finally:
        root.destroy()

    print(f"遮挡下: 采样帧 {res['n_frames_sampled']}  首帧延迟 {res['first_frame_s']}s  "
          f"报错 {res['errors']}")
    for s in res["samples"]:
        print(f"  {s['shape']} mean={s['mean']} std={s['std']} colors={s['uniq_colors']} → {s['verdict']}")

    if not saved_path:
        print("没拿到帧 → WGC 在遮挡下不出帧（或被遮挡时帧被抑制）")
        return 0

    # 统计遮挡色占比：这是判定的关键
    a = np.asarray(Image.open(saved_path).convert("RGB")).astype(np.int16)
    occ = (np.abs(a - np.array(OCC, dtype=np.int16)).sum(axis=2) < 30).mean()
    print(f"\n遮挡色(#FF00AA)占比 = {occ:.3f}")
    if occ > 0.5:
        print("结论：占据大半 → WGC 拿的是**屏幕像素**，被遮挡即失效（与 BitBlt 同类）。")
    elif occ < 0.05:
        print("结论：几乎不含遮挡色 → WGC 拿的是**窗口自己的表面**，被遮挡仍可工作。")
    else:
        print("结论：部分含遮挡色 → 混合，需要人工看一眼图。")
    print(f"图: {saved_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="WGC 能否截到微信窗口真实像素（探针 R1）")
    ap.add_argument("--list", action="store_true", help="列出候选窗口")
    ap.add_argument("--all-windows", action="store_true", help="列出全部顶层窗口")
    ap.add_argument("--capture", type=float, metavar="秒", help="采集指定秒数")
    ap.add_argument("--save", action="store_true", help="存一帧 PNG 到 out/ 供人工核对")
    ap.add_argument("--hwnd", type=int, help="指定窗口句柄（默认自动挑微信）")
    ap.add_argument("--border", action="store_true", help="尝试让 WGC 画采集边框（默认关闭）")
    ap.add_argument("--minimize-test", action="store_true",
                    help="测最小化时是否出帧；会改动窗口状态，测完还原")
    ap.add_argument("--occlude-test", action="store_true",
                    help="测被遮挡时是否仍出帧（用纯色窗口盖住微信，再判定截到的是什么）")
    args = ap.parse_args()

    if args.all_windows:
        wins = list_windows(verbose=True)
        print(f"全部顶层窗口 {len(wins)} 个：")
        for w in wins:
            print(f"  hwnd={w['hwnd']:<10} vis={int(w['visible'])} min={int(w['minimized'])} "
                  f"{w['size'][0]:>5}x{w['size'][1]:<5} {w['class'][:26]:<26} {w['exe'][:18]:<18} {w['title'][:30]!r}")
        return 0

    if args.list or not args.capture:
        wins = list_windows()
        if not wins:
            print("没找到微信窗口。微信是否在运行？（tasklist | grep -i weixin）")
            return 1
        print(f"候选窗口 {len(wins)} 个：")
        for w in wins:
            print(f"  hwnd={w['hwnd']:<10} vis={int(w['visible'])} min={int(w['minimized'])} "
                  f"{w['size'][0]:>5}x{w['size'][1]:<5} {w['class']:<26} {w['exe']:<16} {w['title'][:30]!r}")
        best = pick_wechat()
        print(f"\n会选中: hwnd={best['hwnd']} class={best['class']} size={best['size']}")
        if best["minimized"]:
            print("注意：该窗口当前是最小化状态。WGC 对最小化窗口不出帧，先还原再测。")
        return 0

    w = {"hwnd": args.hwnd, "class": "", "size": (0, 0), "minimized": False, "title": ""}
    if args.hwnd:
        import win32gui

        r = win32gui.GetWindowRect(args.hwnd)
        w = {"hwnd": args.hwnd, "class": win32gui.GetClassName(args.hwnd),
             "size": (r[2] - r[0], r[3] - r[1]), "minimized": bool(win32gui.IsIconic(args.hwnd)),
             "title": win32gui.GetWindowText(args.hwnd)}
    else:
        picked = pick_wechat()
        if picked is None:
            print("没找到微信窗口。先 --list 看看，或微信没在运行。")
            return 1
        w = picked

    print(f"目标窗口 hwnd={w['hwnd']} class={w['class']!r} size={w['size']} "
          f"minimized={w['minimized']} title={w['title']!r}")

    if args.occlude_test:
        if w["minimized"]:
            print("窗口是最小化的，遮挡测试无意义（最小化本身就不出帧）。先还原微信。")
            return 1
        return occlude_test(w["hwnd"], args.capture or 3.0)

    print(f"采集 {args.capture}s（draw_border={args.border}）…")

    res = capture_window(w["hwnd"], args.capture, args.save, draw_border=args.border)
    print(f"\n耗时 {res['elapsed_s']}s   采样帧 {res['n_frames_sampled']}  "
          f"首帧延迟 {res['first_frame_s']}s")
    if res["errors"]:
        for e in res["errors"]:
            print(f"  采集报错: {e}")
    for i, s in enumerate(res["samples"], 1):
        print(f"  帧{i}: {s['shape']} mean={s['mean']} std={s['std']} "
              f"colors={s['uniq_colors']} 近黑={s['near_black_frac']}  → {s['verdict']}")
    if res["saved"]:
        print(f"  已存 {res['saved']}")

    if args.minimize_test:
        import win32gui
        import win32con

        was_min = bool(win32gui.IsIconic(w["hwnd"]))
        if was_min:
            print("\n窗口本来就是最小化，跳过（还原会改变你的窗口状态）。")
        else:
            print("\n--- 最小化测试（会还原）---")
            win32gui.ShowWindow(w["hwnd"], win32con.SW_MINIMIZE)
            time.sleep(0.8)
            r2 = capture_window(w["hwnd"], 2.0, save=False, draw_border=False)
            print(f"最小化时: 采样帧 {r2['n_frames_sampled']}  首帧延迟 {r2['first_frame_s']}s  "
                  f"报错 {r2['errors']}")
            for s in r2["samples"]:
                print(f"  {s['shape']} std={s['std']} → {s['verdict']}")
            win32gui.ShowWindow(w["hwnd"], win32con.SW_RESTORE)
            time.sleep(0.5)
            print("已还原窗口。")
            r3 = capture_window(w["hwnd"], 2.0, save=False, draw_border=False)
            print(f"还原后: 采样帧 {r3['n_frames_sampled']}  首帧延迟 {r3['first_frame_s']}s")
            for s in r3["samples"]:
                print(f"  {s['shape']} std={s['std']} → {s['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
