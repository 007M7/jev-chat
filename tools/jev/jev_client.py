"""Jev 判断接口的兼容层。

以前这里写死了 OpenRouter 的地址、模型名和密钥环境变量。现在端点与模型都来自
providers.json 的 roles.judge 指向的 provider——加一家判断服务只改配置，不动代码。

对外接口保持原样（ask / JevError / redact_secrets），因为 calibrate.py 与
demo_meme.py 依赖它们。密钥只从环境变量读，绝不打印或落盘。
"""

from __future__ import annotations

from providers import ProviderError, ask_systemone, describe, redact

# 兼容别名：调用方原来 catch jev_client.JevError，现在底层抛 ProviderError。
# ProviderError 同样带 .status，所以 `except JevError` 与 `JevError(msg, code)` 都能继续用。
JevError = ProviderError

# 兼容旧名：校准脚本/报告里出现过，保留但不再参与请求构造
MAX_RETRIES = 3
API_URL = "（由 providers.json 的 roles.judge 决定）"
MODEL = "（由 providers.json 的 roles.judge 决定）"


def redact_secrets(text: str) -> str:
    """把环境里出现过的密钥从文本里抹掉，再打印或落盘。"""
    return redact(text)


def ask(
    state: dict,
    questions: dict,
    timeout: float = 20,
    provider: str | None = None,
) -> dict:
    """把 state + questions 发给 Jev，返回原始 JSON。

    走 providers.json 里 roles.judge 指定的 provider；`provider` 参数可临时指定
    provider id（例如 "openrouter_jev" 切回 OpenRouter 中转）。
    """
    return ask_systemone(state, questions, role="judge", provider_override=provider, timeout=timeout)


def current_provider() -> str:
    """给日志用的一行说明（不含密钥）。"""
    return describe("judge")


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(current_provider())
