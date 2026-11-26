"""API providers and the translation engine. The API keys stay on the server.

Each provider's address, model and key are saved once here (TranslationSettings.profiles, plus the provider in
use for translation). Everything that calls a provider reads them from here: subtitle translation, AI
marking and articles (the provider in use), and cloud speech recognition (asr.py, through provider_access).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from threading import Lock
from urllib.parse import urlsplit

from .config import DATA_DIR

SETTINGS_PATH = DATA_DIR / "settings.json"
_lock = Lock()

API_PROVIDERS = {
    "openai": ("OpenAI API", "https://api.openai.com/v1", "gpt-6.2-sol", "https://platform.openai.com/api-keys"),
    "gemini": ("Google Gemini API", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-4-hydrogen", "https://aistudio.google.com/apikey"),
    "grok": ("Grok API", "https://api.x.ai/v1", "grok-4.7", "https://console.x.ai/"),
    "deepseek": ("DeepSeek API", "https://api.deepseek.com", "deepseek-flash", "https://platform.deepseek.com/api_keys"),
    "qwen": ("通义千问 · 阿里云百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "https://bailian.console.aliyun.com/"),
    "kimi": ("Kimi · 月之暗面", "https://api.moonshot.cn/v1", "", "https://platform.moonshot.cn/"),
    "glm": ("GLM · 智谱", "https://open.bigmodel.cn/api/paas/v4", "", "https://open.bigmodel.cn/"),
    "doubao": ("豆包 · 火山方舟", "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2-1-pro-260915", "https://console.volcengine.com/ark/"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "", "https://console.groq.com/keys"),
    "custom": ("自定义 OpenAI 兼容 API", "", "", ""),
}


@dataclass(frozen=True)
class TranslationSettings:
    engine: str = "local"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    provider: str = "custom"
    profiles: dict = field(default_factory=dict, repr=False)

    def api_profiles(self) -> dict:
        profiles = self.profiles.copy()
        if self.base_url and self.provider in API_PROVIDERS:
            profiles[self.provider] = {"base_url": self.base_url, "model": self.model, "api_key": self.api_key}
        return profiles

    def public(self) -> dict:
        profiles = self.api_profiles()
        return {
            "engine": self.engine,
            "baseUrl": self.base_url,
            "model": self.model,
            "hasApiKey": bool(self.api_key),
            "provider": self.provider,
            "providers": [
                {"id": provider, "name": name, "baseUrl": profile.get("base_url", base_url),
                 "model": profile.get("model", model), "hasSavedModel": bool(profile.get("model")),
                 "hasApiKey": bool(profile.get("api_key")), "console": console}
                for provider, (name, base_url, model, console) in API_PROVIDERS.items()
                for profile in (profiles.get(provider, {}),)
            ],
        }


def load_settings() -> TranslationSettings:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return TranslationSettings()
    except (OSError, ValueError):
        raise ValueError("无法读取本机的翻译设置。") from None
    try:
        settings = TranslationSettings(**data)
    except TypeError:
        raise ValueError("本机的翻译设置格式有误。") from None
    return settings


def _text(payload: dict, name: str, default: str, limit: int) -> str:
    value = payload.get(name, default)
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("设置字段的格式或长度有误。")
    return value.strip()


def _base_url(value: str) -> str:
    value = value.rstrip("/").removesuffix("/chat/completions").rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError("请输入有效的 API 地址。") from None
    if (not parsed.hostname or parsed.scheme not in ("http", "https") or parsed.username is not None
            or parsed.query or parsed.fragment or re.search(r"[\s\\]", value)):
        raise ValueError("请填写服务商提供的 API 地址，地址中不应包含密钥或查询参数。")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("远程 API 地址需要使用 HTTPS。本机服务可以用 HTTP。")
    return value


def settings_from(payload: dict, current: TranslationSettings, *, require_model: bool = True) -> TranslationSettings:
    engine = payload.get("engine", current.engine)
    if engine not in ("local", "api"):
        raise ValueError("请选择一个翻译方式。")
    profiles = current.api_profiles()
    if engine == "local":
        return TranslationSettings(engine="local", profiles=profiles)
    provider = payload.get("provider", current.provider)
    if not isinstance(provider, str) or provider not in API_PROVIDERS:
        raise ValueError("请选择一个 API 服务商。")
    _, default_url, default_model, _ = API_PROVIDERS[provider]
    profile = profiles.get(provider, {})
    base_url = _text(payload, "baseUrl", profile.get("base_url", default_url), 2048)
    model = _text(payload, "model", profile.get("model", default_model), 200)
    if base_url:
        base_url = _base_url(base_url)
    if not base_url:
        raise ValueError("请填写 API 地址。")
    keep_active = payload.get("keepActive") is True and not (current.engine == "api" and current.provider == provider)
    if require_model and not model and not keep_active:
        raise ValueError("请填写 API 地址和模型名称。")
    supplied_key = _text(payload, "apiKey", "", 4096)
    if supplied_key and not re.fullmatch(r"[\x21-\x7e]+", supplied_key):
        raise ValueError("API Key 含有无效字符，请重新复制。")
    saved_key = profile.get("api_key", "") if base_url == profile.get("base_url") else ""
    key = "" if payload.get("clearKey") is True else supplied_key or saved_key
    if not key and provider != "custom" and payload.get("clearKey") is not True:
        raise ValueError("请填写这个服务商的 API Key。")
    if keep_active:  # save this provider's key (for speech recognition, say) without switching translation to it
        profiles[provider] = {"base_url": base_url, "model": model, "api_key": key}
        if current.engine == "api":
            profiles.pop(current.provider, None)
        return TranslationSettings(current.engine, current.base_url, current.model, current.api_key,
                                   current.provider, profiles)
    profiles.pop(provider, None)
    return TranslationSettings(engine, base_url, model, key, provider, profiles)


def save_settings(payload: dict) -> TranslationSettings:
    with _lock:
        settings = settings_from(payload, load_settings())
        _write(settings)
        return settings


def _write(settings: TranslationSettings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=SETTINGS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(asdict(settings), file, ensure_ascii=False, indent=2)
        os.replace(name, SETTINGS_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def provider_access(provider: str, settings: TranslationSettings | None = None) -> tuple[str, str]:
    """(address, API key) saved for a provider; the default address and no key when none is saved."""
    settings = load_settings() if settings is None else settings
    profile = settings.api_profiles().get(provider, {})
    default = API_PROVIDERS.get(provider, ("", "", "", ""))[1]
    return (profile.get("base_url") or default).rstrip("/"), profile.get("api_key", "")


def delete_provider(provider: str) -> TranslationSettings:
    """Forget a provider's address, model and API key. If translation was using it, translation switches to the
    offline engine; the other providers are kept as they are."""
    if provider not in API_PROVIDERS:
        raise ValueError("请选择一个 API 服务商。")
    with _lock:
        current = load_settings()
        profiles = current.api_profiles()
        profiles.pop(provider, None)
        if current.provider == provider:
            new = TranslationSettings(engine="local", profiles=profiles)
        else:
            if current.base_url:
                profiles.pop(current.provider, None)  # held in the top-level fields, as api_profiles() expects
            new = TranslationSettings(current.engine, current.base_url, current.model, current.api_key,
                                      current.provider, profiles)
        _write(new)
        return new


def store_key(provider: str, key: str, base_url: str = "") -> None:
    """Save a key for a provider that has none yet (moving keys saved elsewhere before), keeping everything else."""
    with _lock:
        current = load_settings()
        if provider not in API_PROVIDERS or provider_access(provider, current)[1]:
            return
        profiles = current.api_profiles()
        old = profiles.get(provider, {})
        profiles[provider] = {"base_url": base_url or old.get("base_url") or API_PROVIDERS[provider][1],
                              "model": old.get("model", API_PROVIDERS[provider][2]), "api_key": key}
        if current.engine == "api" and current.provider == provider:
            new = TranslationSettings(current.engine, profiles[provider]["base_url"], current.model, key, provider,
                                      {k: v for k, v in profiles.items() if k != provider})
        else:
            if current.engine == "api":
                profiles.pop(current.provider, None)
            new = TranslationSettings(current.engine, current.base_url, current.model, current.api_key,
                                      current.provider, profiles)
        _write(new)
