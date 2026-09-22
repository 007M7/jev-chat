"""provider 解析与传输层。

原来端点、模型名、密钥环境变量三样都硬编码在代码里（写死走 OpenRouter）。
改成读 providers.json：加一家 provider 只改配置，不动代码。

**用户可以不改仓库就换服务**：设 `JEV_PROVIDERS_OVERLAY` 指向一份库外的 JSON
（默认 `~/.jev/providers.json`），它按键合并到 providers.json 之上——
base_url / api_format / path / models / enabled / roles / role_models 都能改。
那份覆盖文件**不该进仓库**（里面可能有用户自己的端点）；密钥仍然只从环境变量读。

密钥规则（与项目硬约束一致）：providers.json 里只写**环境变量名**，
真实密钥永远只从环境变量读，不打印、不落盘、不进日志。

schema v2（2026-09-23）是**加法式**升级：kind / path / model / max_tokens 全部保留，
所以对老调用方的接口没变；新增的是
  - providers.<id>.models[]   设置页「模型列表」用
  - role_models.<role>        每个角色用哪个模型（与 roles 分开，便于同服务多模型）
既有调用方仍然拿 `(pid, prov)`，其中 prov["model"] 已**按角色解析好**。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROVIDERS_JSON = ROOT / "providers.json"

# 用户覆盖层：库外 JSON，按键合并到 providers.json 之上
OVERLAY_ENV = "JEV_PROVIDERS_OVERLAY"
DEFAULT_OVERLAY = Path.home() / ".jev" / "providers.json"

# systemone = TypeSafe/Jev 的判断类接口；openai_chat = OpenAI 兼容的对话接口
KINDS = ("systemone", "openai_chat")


class ProviderError(Exception):
    """可读的 provider 错误。消息里绝不包含密钥。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def redact(text: str) -> str:
    """把环境里出现过的任何 key 从文本里抹掉，再打印或落盘。"""
    if not isinstance(text, str):
        text = str(text)
    for name in ("JEV_API_KEY", "OPENROUTER_API_KEY", "LOCAL_LLM_API_KEY"):
        k = os.environ.get(name) or ""
        if k:
            text = text.replace(k, "[REDACTED]")
    return text


def _overlay_path() -> Path | None:
    """用户覆盖层的位置：JEV_PROVIDERS_OVERLAY 优先，否则 ~/.jev/providers.json。"""
    env = (os.environ.get(OVERLAY_ENV) or "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_OVERLAY


def _merge_overlay(cfg: dict, overlay: dict) -> dict:
    """把覆盖层按键合并到基础配置上。**只覆盖它写了的东西**，没写的保持默认。

    合并粒度到 provider 的字段级（而不是整个 provider 替换）：这样用户只想改
    一个 base_url 时，不必把该 provider 的其余字段（note、extra_headers…）抄一遍。
    """
    out = dict(cfg)
    for key in ("roles", "role_models"):
        if isinstance(overlay.get(key), dict):
            merged = dict(cfg.get(key) or {})
            merged.update(overlay[key])
            out[key] = merged

    if isinstance(overlay.get("providers"), dict):
        providers = {k: dict(v) for k, v in (cfg.get("providers") or {}).items()}
        for pid, patch in overlay["providers"].items():
            if not isinstance(patch, dict):
                continue
            if pid in providers:
                providers[pid].update(patch)
            else:
                providers[pid] = dict(patch)      # 用户自加的 provider
        out["providers"] = providers

    for key in ("api_formats",):
        if isinstance(overlay.get(key), dict):
            merged = dict(cfg.get(key) or {})
            merged.update(overlay[key])
            out[key] = merged
    return out


def load_config(path: Path | None = None, with_overlay: bool = True) -> dict:
    p = path or PROVIDERS_JSON
    if not p.exists():
        raise ProviderError(f"缺少 provider 配置: {p}")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{p.name} 不是合法 JSON: {exc}") from None
    if not isinstance(cfg.get("providers"), dict) or not cfg["providers"]:
        raise ProviderError(f"{p.name} 里没有 providers")

    if with_overlay and path is None:
        op = _overlay_path()
        if op and op.exists():
            try:
                overlay = json.loads(op.read_text(encoding="utf-8"))
                if isinstance(overlay, dict):
                    cfg = _merge_overlay(cfg, overlay)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"覆盖层 {op} 不是合法 JSON: {exc}") from None
    return cfg


def resolve_model(role: str, pid: str, prov: dict, cfg: dict) -> str:
    """这个角色该用哪个模型。

    优先 role_models.<role>；否则该 provider 的第一个启用模型；再否则 v1 的 model 字段。
    """
    want = (cfg.get("role_models") or {}).get(role)
    models = prov.get("models") or []
    ids = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
    if want and want in ids:
        return want
    for m in models:
        if isinstance(m, dict) and m.get("enabled") and m.get("id"):
            return m["id"]
    return want or prov.get("model") or (ids[0] if ids else "")


def role_provider(role: str, override: str | None = None, path: Path | None = None) -> tuple[str, dict]:
    """按角色取 provider：roles.<role> → providers.<id>。override 可临时指定 id。

    返回的 prov 是**副本**，其中 model 已按角色解析好——所以老调用方
    （直接读 prov["model"]）自动就支持「同一 provider 下不同角色用不同模型」。
    """
    cfg = load_config(path)
    pid = override or (cfg.get("roles") or {}).get(role)
    if not pid:
        raise ProviderError(
            f"roles.{role} 未配置。可选 provider: {', '.join(cfg['providers'])}"
        )
    raw = cfg["providers"].get(pid)
    if raw is None:
        raise ProviderError(
            f"roles.{role} 指向的 provider {pid!r} 不存在。可选: {', '.join(cfg['providers'])}"
        )
    prov = dict(raw)

    # 允许只填 base_url + api_format：path 与 kind 都能从 api_format 推出来。
    # 这样用户在设置页里新加一个 provider 时不必手写路径（少一处能填错的地方）。
    fmt = (cfg.get("api_formats") or {}).get(prov.get("api_format") or "") or {}
    if not prov.get("path"):
        prov["path"] = fmt.get("path") or ""
    if not prov.get("kind"):
        prov["kind"] = fmt.get("kind") or ""

    kind = prov.get("kind")
    if kind not in KINDS:
        raise ProviderError(
            f"provider {pid!r} 的 kind 必须是 {KINDS} 之一，当前 {kind!r}。"
            f"（想省事就填 api_format: {' 或 '.join(k for k in (cfg.get('api_formats') or {}) if not k.startswith('_'))}）"
        )

    model = resolve_model(role, pid, prov, cfg)
    if not model:
        raise ProviderError(f"provider {pid!r} 没有可用模型（models[] 全空或全被禁用）")
    prov["model"] = model

    # 模型级 max_tokens 覆盖 provider 级：思考型视觉模型需要的额度
    # 与起草模型不同，放在模型上更贴合实际。
    for m in prov.get("models") or []:
        if isinstance(m, dict) and m.get("id") == model and m.get("max_tokens"):
            prov["max_tokens"] = m["max_tokens"]
            break
    return pid, prov



def api_key(pid: str, prov: dict) -> str:
    env_name = prov.get("api_key_env")
    if not env_name:
        raise ProviderError(f"provider {pid!r} 没写 api_key_env")
    key = (os.environ.get(env_name) or "").strip()
    if not key:
        if prov.get("api_key_optional"):
            return ""  # 本地服务通常不校验密钥
        raise ProviderError(
            f"provider {pid!r} 需要密钥，但环境变量 {env_name} 未设置。\n"
            f"  Git Bash:   export {env_name}=...\n"
            f"  PowerShell: $env:{env_name}='...'\n"
            "密钥只从环境变量读，不要写进任何文件。"
        )
    return key


def build_url(prov: dict) -> str:
    base = (prov.get("base_url") or "").rstrip("/")
    path = prov.get("path") or ""
    if not path:
        return base
    # 用户很可能把**完整 URL** 直接粘进 Base URL（例如
    # https://api.deepseek.com/v1/chat/completions）。再拼一次 path 就会变成
    # .../v1/chat/completions/v1/chat/completions —— 报 404 还很难查。
    if base.endswith(path) or base.endswith(path.lstrip("/")):
        return base
    return base + path


def _headers(pid: str, prov: dict) -> dict:
    key = api_key(pid, prov)
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    h.update(prov.get("extra_headers") or {})
    return h


def post_json(
    pid: str,
    prov: dict,
    body: dict,
    timeout: float | None = None,
    retries: int = 3,
) -> dict:
    """POST 一个 JSON 并返回解析结果。429/529/5xx 指数退避重试。"""
    url = build_url(prov)
    if not url.startswith("http"):
        raise ProviderError(f"provider {pid!r} 的 base_url 不合法: {url!r}")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    to = float(timeout or prov.get("timeout_default") or 30)
    last_status: int | None = None
    last_body = ""

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=payload, method="POST", headers=_headers(pid, prov))
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    raise ProviderError(
                        f"{pid} 返回的不是 JSON（前 300 字）: {redact(raw)[:300]}"
                    ) from None
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            try:
                last_body = redact(exc.read().decode("utf-8", errors="replace"))[:600]
            except Exception:
                last_body = ""
            if last_status in (429, 500, 502, 503, 529) and attempt < retries:
                time.sleep(2**attempt)
                continue
            readable = {
                401: f"{pid} 密钥无效或未授权（401）。检查 {prov.get('api_key_env')}。",
                403: f"{pid} 拒绝访问（403）。检查密钥权限或该模型是否可用。",
                404: f"{pid} 端点不存在（404）: {url}。检查 providers.json 的 base_url/path。",
                422: f"{pid} 请求体被拒（422）。{last_body}",
            }.get(last_status, f"{pid} HTTP {last_status}: {last_body}")
            raise ProviderError(readable, last_status) from None
        except (TimeoutError, socket.timeout) as exc:
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise ProviderError(f"{pid} 请求超时（{to}s）: {url}") from exc
        except urllib.error.URLError as exc:
            reason = redact(str(getattr(exc, "reason", exc)))
            if isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
                raise ProviderError(
                    f"连不上 {pid}（{url}）。本地服务是否在运行？在 providers.json 里改 base_url 可换地址。"
                ) from None
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise ProviderError(f"{pid} 请求失败: {reason}") from None

    raise ProviderError(f"{pid} HTTP {last_status}: 重试耗尽。{last_body}", last_status)


# ---------------------------------------------------------------- 两种协议


def ask_systemone(
    state: dict,
    questions: dict,
    role: str = "judge",
    provider_override: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> dict:
    """判断类请求（TypeSafe systemone / OpenRouter decisions 同一个 body 形状）。"""
    pid, prov = role_provider(role, provider_override)
    body = {"model": model or prov.get("model"), "state": state, "questions": questions}
    return post_json(pid, prov, body, timeout=timeout)


def chat(
    messages: list[dict],
    role: str = "analysis",
    provider_override: str | None = None,
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> tuple[str, dict, dict]:
    """OpenAI 兼容的对话请求。返回 (content, usage, provider 快照)。

    `model` 可覆盖配置里的模型（同一家 provider 换模型时用）。
    """
    pid, prov = role_provider(role, provider_override)
    chosen = model or prov.get("model")
    body: dict = {"model": chosen, "messages": messages, "temperature": temperature}
    # 思考型模型（DeepSeek v4/flash 等）会把 max_tokens 耗在推理上，正文返回空。
    # 所以默认值优先取 provider 配置里的 max_tokens，而不是不传。
    mt = max_tokens if max_tokens is not None else prov.get("max_tokens")
    if mt:
        body["max_tokens"] = mt
    resp = post_json(pid, prov, body, timeout=timeout)
    choices = resp.get("choices") or []
    if not choices:
        raise ProviderError(f"{pid} 没有返回 choices: {redact(json.dumps(resp, ensure_ascii=False))[:300]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    usage = resp.get("usage") or {}
    if not content.strip():
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        finish = choices[0].get("finish_reason")
        hint = ""
        if reasoning:
            hint = (
                f" 这次 {reasoning} 个 token 全花在推理上——这是思考型模型，"
                f"把该 provider 的 max_tokens 调大重试。"
            )
        raise ProviderError(
            f"{pid}（{chosen}）返回了空正文（finish_reason={finish}）。{hint}"
        )
    return content, usage, {"id": pid, "model": chosen}


def describe(role: str, provider_override: str | None = None) -> str:
    """给日志用的一行说明。不含密钥。"""
    try:
        pid, prov = role_provider(role, provider_override)
    except ProviderError as exc:
        return f"{role}: 未配置（{exc}）"
    env_name = prov.get("api_key_env")
    has = "已设置" if os.environ.get(env_name or "") else ("可选" if prov.get("api_key_optional") else "未设置")
    return f"{role}: {pid} ({prov.get('kind')}) {build_url(prov)} model={prov.get('model')} key[{env_name}]={has}"


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    argv = sys.argv[1:]
    overlay = _overlay_path()
    print("默认配置:", PROVIDERS_JSON)
    if overlay and overlay.exists():
        print(f"用户覆盖层: {overlay}  ← 生效中")
    elif overlay:
        print(f"用户覆盖层: {overlay}  （不存在，用默认配置）")
        print(f"  想换服务/换模型又不改仓库：{OVERLAY_ENV} 指向一份库外 JSON，")
        print("  形如 {\"providers\": {\"deepseek\": {\"base_url\": \"https://…\"}}, \"role_models\": {\"perception\": \"…\"}}")
    print()

    if "--show" in argv or "--models" in argv:
        cfg = load_config()
        print("角色 → provider → 模型：")
        for r in ("judge", "analysis", "perception"):
            try:
                pid, prov = role_provider(r)
            except ProviderError as exc:
                print(f"   {r:<11} 未配置（{exc}）")
                continue
            print(f"   {r:<11} {pid:<16} {prov.get('model'):<30} {build_url(prov)}")
        print()
        print("各家 provider 的模型列表（[x]=启用，未启用不会被角色选中）：")
        for pid, p in cfg["providers"].items():
            fmt = p.get("api_format") or p.get("kind")
            print(f"   {pid}　{p.get('label')}　[{fmt}]")
            for m in p.get("models") or []:
                if not isinstance(m, dict):
                    continue
                mark = "x" if m.get("enabled") else " "
                tags = " ".join(m.get("tags") or [])
                ctx = m.get("context") or "—"
                key = "★" if m.get("id") in (cfg.get("role_models") or {}).values() else " "
                print(f"     [{mark}]{key} {m.get('id'):<32} {ctx:<6} {tags}")
        sys.exit(0)

    for r in ("judge", "analysis", "perception"):
        print("  ", describe(r))
    print("\n可用 provider:")
    for pid, p in load_config()["providers"].items():
        print(f"   {pid:<18} {p.get('kind'):<13} {p.get('label')}")
    print("\n（加 --show 看每个角色解析到哪个模型、各家有哪些模型）")
