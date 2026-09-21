"""provider 解析与传输层。

原来端点、模型名、密钥环境变量三样都硬编码在代码里（写死走 OpenRouter）。
改成读 providers.json：加一家 provider 只改配置，不动代码。

密钥规则（与项目硬约束一致）：providers.json 里只写**环境变量名**，
真实密钥永远只从环境变量读，不打印、不落盘、不进日志。
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


def load_config(path: Path | None = None) -> dict:
    p = path or PROVIDERS_JSON
    if not p.exists():
        raise ProviderError(f"缺少 provider 配置: {p}")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{p.name} 不是合法 JSON: {exc}") from None
    if not isinstance(cfg.get("providers"), dict) or not cfg["providers"]:
        raise ProviderError(f"{p.name} 里没有 providers")
    return cfg


def role_provider(role: str, override: str | None = None, path: Path | None = None) -> tuple[str, dict]:
    """按角色取 provider：roles.<role> → providers.<id>。override 可临时指定 id。"""
    cfg = load_config(path)
    pid = override or (cfg.get("roles") or {}).get(role)
    if not pid:
        raise ProviderError(
            f"roles.{role} 未配置。可选 provider: {', '.join(cfg['providers'])}"
        )
    prov = cfg["providers"].get(pid)
    if prov is None:
        raise ProviderError(
            f"roles.{role} 指向的 provider {pid!r} 不存在。可选: {', '.join(cfg['providers'])}"
        )
    kind = prov.get("kind")
    if kind not in KINDS:
        raise ProviderError(f"provider {pid!r} 的 kind 必须是 {KINDS} 之一，当前 {kind!r}")
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
    return (prov.get("base_url") or "").rstrip("/") + (prov.get("path") or "")


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
    print("provider 配置:", PROVIDERS_JSON)
    for r in ("judge", "analysis", "perception"):
        print("  ", describe(r))
    print("\n可用 provider:")
    for pid, p in load_config()["providers"].items():
        print(f"   {pid:<18} {p.get('kind'):<13} {p.get('label')}")
