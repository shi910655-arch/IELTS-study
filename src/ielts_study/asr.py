"""Speech recognition: which engine and model turn speech into English text with word timings.

Local (default, works offline)
    Apple Silicon → mlx-whisper; Windows / Linux / Intel Mac → faster-whisper (NVIDIA GPU if there is one).
    The model size is chosen in Settings. "Auto" means the large model where it runs fast (Apple Silicon, an
    NVIDIA GPU) and the small English model on a plain CPU. So the first video doesn't wait for a 1.6 GB
    download, "auto" starts with the small model (about 480 MB) and fetches the large one in the background;
    once it is there, new imports use it.

Cloud (optional)
    Any OpenAI-compatible /audio/transcriptions endpoint with word timestamps: OpenAI whisper-1, Groq's
    whisper-large-v3-turbo, or a custom one. The audio is compressed to small MP3 pieces first. If the cloud
    service can't be reached, recognition falls back to the local model.

The same engine reads the learner's own recordings on the speaking page (`recognize`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import APPLE_SILICON, DATA_DIR, WHISPER_MODEL

ASR_PATH = DATA_DIR / "asr.json"
_lock = threading.Lock()

# size -> (MLX repo, faster-whisper name, download size in MB)
MODELS = {
    "base": ("mlx-community/whisper-base.en-mlx", "base.en", 145),
    "small": ("mlx-community/whisper-small.en-mlx", "small.en", 485),
    "medium": ("mlx-community/whisper-medium.en-mlx", "medium.en", 1530),
    "large": ("mlx-community/whisper-large-v3-turbo", "large-v3-turbo", 1620),
}
SIZES = list(MODELS)
QUICK = "small"  # what "auto" uses while the large model is still downloading

CLOUD_PROVIDERS = {
    "openai": ("OpenAI", "https://api.openai.com/v1", "whisper-1", "https://platform.openai.com/api-keys"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo", "https://console.groq.com/keys"),
    "custom": ("自定义 OpenAI 兼容接口", "", "whisper-1", ""),
}
CHUNK_SECONDS = 600  # cloud services take files up to 25 MB; 10 minutes of 32 kbps MP3 is about 2.4 MB


# ------------------------------------------------------------------ settings

@dataclass(frozen=True)
class AsrSettings:
    engine: str = "local"        # local | cloud
    model: str = "auto"          # auto | base | small | medium | large
    provider: str = "openai"     # openai | groq | custom: the provider's address and key come from settings.py
    base_url: str = ""           # an address saved here before the keys moved to settings.py (custom only)
    cloud_model: str = ""
    keys: dict = field(default_factory=dict, repr=False)  # keys saved here before; moved by migrate_keys()

    def endpoint(self) -> tuple[str, str, str]:
        from .settings import provider_access

        _, default_url, model, _ = CLOUD_PROVIDERS.get(self.provider, CLOUD_PROVIDERS["custom"])
        url, key = provider_access(self.provider)
        if self.provider == "custom" and self.base_url and self.base_url.rstrip("/") != url:
            url, key = self.base_url, self.keys.get("custom", "")  # a separate recognition server set up before
        return (url or default_url).rstrip("/"), self.cloud_model or model, key or self.keys.get(self.provider, "")

    def public(self) -> dict:
        from .settings import API_PROVIDERS, provider_access

        url, model, key = self.endpoint()
        return {
            "engine": self.engine, "model": self.model, "provider": self.provider, "baseUrl": url,
            "cloudModel": model, "hasKey": bool(key),
            "providers": [{"id": pid, "name": API_PROVIDERS[pid][0], "baseUrl": provider_access(pid)[0] or u,
                           "model": m, "hasKey": bool(provider_access(pid)[1] or self.keys.get(pid))}
                          for pid, (_, u, m, _) in CLOUD_PROVIDERS.items()],
            "local": local_status(self),
        }


def load_settings() -> AsrSettings:
    try:
        data = json.loads(ASR_PATH.read_text(encoding="utf-8"))
        return AsrSettings(**{k: v for k, v in data.items() if k in AsrSettings.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return AsrSettings()


def _write(settings: AsrSettings) -> None:
    ASR_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".asr-", dir=ASR_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(settings), f, ensure_ascii=False, indent=2)
        os.replace(name, ASR_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def migrate_keys() -> int:
    """Keys typed into the old speech-recognition form move to the one list of providers (settings.py).
    A provider that already has a key there keeps it. Returns how many keys moved."""
    from . import settings as provider_settings

    with _lock:
        current = load_settings()
        if not current.keys:
            return 0
        moved = 0
        for pid, key in current.keys.items():
            if pid not in CLOUD_PROVIDERS or not key:
                continue
            address = current.base_url.rstrip("/") if pid == "custom" else ""
            shared_url, shared_key = provider_settings.provider_access(pid)
            if pid == "custom" and shared_url and shared_url != address:
                continue  # a recognition server of its own: its key stays with it (see endpoint)
            if not shared_key:
                provider_settings.store_key(pid, key, address)
                moved += 1
        keep = {"custom": current.keys["custom"]} if current.keys.get("custom") and current.base_url and \
            provider_settings.provider_access("custom")[0] != current.base_url.rstrip("/") else {}
        _write(AsrSettings(current.engine, current.model, current.provider, current.base_url, current.cloud_model, keep))
        return moved


def forget_key(provider: str) -> None:
    """A provider deleted in Settings: drop any key for it still kept from the old recognition form."""
    with _lock:
        current = load_settings()
        if provider in current.keys:
            keys = {k: v for k, v in current.keys.items() if k != provider}
            _write(AsrSettings(current.engine, current.model, current.provider, current.base_url,
                               current.cloud_model, keys))


def save_settings(payload: dict) -> AsrSettings:
    with _lock:
        current = load_settings()
        engine = payload.get("engine", current.engine)
        model = payload.get("model", current.model)
        provider = payload.get("provider", current.provider)
        if engine not in ("local", "cloud") or model not in ("auto", *SIZES) or provider not in CLOUD_PROVIDERS:
            raise ValueError("请选择有效的识别方式。")
        cloud_model = str(payload.get("cloudModel", current.cloud_model if provider == current.provider else "")
                          or "").strip()[:200]
        settings = AsrSettings(engine, model, provider, current.base_url if provider == "custom" else "",
                               cloud_model, current.keys)
        if engine == "cloud":
            url, _, key = settings.endpoint()
            if provider == "custom" and not url:
                raise ValueError("先在「服务商与密钥」里填写自定义 API 的地址。")
            if provider != "custom" and not key:
                raise ValueError("先在「服务商与密钥」里填写这个服务商的 API Key。")
        _write(settings)
    if engine == "local":
        _start_upgrade(settings)
    return settings


# ------------------------------------------------------------------ local models

def _gpu() -> bool:
    if APPLE_SILICON:
        return True
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - no CUDA runtime
        return False


def auto_size() -> str:
    return "large" if _gpu() else "small"


def _repo(size: str) -> str:
    mlx, name, _ = MODELS[size]
    if APPLE_SILICON:
        return mlx
    from faster_whisper.utils import _MODELS

    return _MODELS.get(name, name)


def _weights_in(folder: Path) -> bool:
    return any((folder / n).is_file() for n in ("weights.npz", "weights.safetensors", "model.safetensors", "model.bin"))


def is_downloaded(size: str) -> bool:
    try:
        from huggingface_hub import snapshot_download

        return _weights_in(Path(snapshot_download(_repo(size), local_files_only=True)))
    except Exception:  # noqa: BLE001 - not in the cache (or no network library)
        return False


def _cache_bytes(size: str) -> int:
    try:
        from huggingface_hub.constants import HF_HUB_CACHE

        blobs = Path(HF_HUB_CACHE) / ("models--" + _repo(size).replace("/", "--")) / "blobs"
        return sum(f.stat().st_size for f in blobs.iterdir()) if blobs.is_dir() else 0
    except Exception:  # noqa: BLE001
        return 0


_download = {"size": None, "status": "idle", "error": ""}


def _fetch(size: str) -> None:
    try:
        if APPLE_SILICON:
            from huggingface_hub import snapshot_download

            snapshot_download(_repo(size))
        else:
            from faster_whisper.utils import download_model

            download_model(MODELS[size][1])
        _download.update(status="done", error="")
    except Exception as error:  # noqa: BLE001 - shown in Settings; the next start continues it
        _download.update(status="error", error=str(error)[:300])


def download(size: str) -> None:
    """Fetch a model in the background (one at a time)."""
    with _lock:
        if _download["status"] == "downloading" or is_downloaded(size):
            return
        _download.update(size=size, status="downloading", error="")
    threading.Thread(target=_fetch, args=(size,), daemon=True).start()


def _start_upgrade(settings: AsrSettings | None = None) -> None:
    settings = settings or load_settings()
    if settings.engine == "local" and settings.model == "auto" and not WHISPER_MODEL:
        target = auto_size()
        if target != QUICK and is_downloaded(QUICK) and not is_downloaded(target):
            download(target)


def local_model(settings: AsrSettings | None = None, log=print) -> str:
    """The model size to use now. With "auto" and the large model not downloaded yet: the quick model, while
    the large one comes down in the background."""
    settings = settings or load_settings()
    if settings.model != "auto":
        return settings.model
    target = auto_size()
    if target == QUICK or is_downloaded(target):
        return target
    download(target)
    ready = next((s for s in ("large", "medium", "small") if is_downloaded(s)), QUICK)
    log("大模型还在后台下载，这次先用较小的模型识别；下载好后新导入的视频自动换成大模型")
    return ready


def local_status(settings: AsrSettings | None = None) -> dict:
    settings = settings or load_settings()
    sizes = {s: {"downloaded": is_downloaded(s), "mb": MODELS[s][2]} for s in SIZES}
    state = dict(_download)
    if state["status"] == "downloading" and state["size"]:
        total = MODELS[state["size"]][2] * 1_000_000
        state["percent"] = min(99, round(100 * _cache_bytes(state["size"]) / total))
    target = settings.model if settings.model != "auto" else auto_size()
    using = target if sizes[target]["downloaded"] or settings.model != "auto" else next(
        (s for s in ("large", "medium", "small") if sizes[s]["downloaded"]), QUICK)
    return {"sizes": sizes, "auto": auto_size(), "target": target, "using": using, "download": state,
            "override": WHISPER_MODEL or "", "engine": "mlx" if APPLE_SILICON else "faster-whisper",
            "gpu": _gpu()}


_models: dict = {}


def _faster_model(name: str):
    from faster_whisper import WhisperModel

    if name not in _models:
        gpu = _gpu()
        _models[name] = WhisperModel(name, device="cuda" if gpu else "cpu", compute_type="float16" if gpu else "int8")
    return _models[name]


def _ffmpeg() -> str:
    from .media import FFMPEG_NAME, ffmpeg_dir

    return str(ffmpeg_dir() / FFMPEG_NAME)


def _audio_16k(path: Path):
    import numpy as np

    raw = subprocess.run([_ffmpeg(), "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


_run_lock = threading.Lock()  # one recognition at a time: the model already uses the whole GPU / CPU


def local_words(path: Path, log=print, progress=None, settings: AsrSettings | None = None) -> tuple[list, list]:
    """(words, segments) from the local model. Words: {word, start, end, probability}."""
    with _run_lock:
        return _local_words(path, log, progress, settings)


def _local_words(path: Path, log, progress, settings) -> tuple[list, list]:
    size = "large" if WHISPER_MODEL else local_model(settings, log)  # IELTS_WHISPER names the model itself
    if APPLE_SILICON:
        import mlx_whisper

        repo = WHISPER_MODEL or MODELS[size][0]
        log(f"本地语音识别中（模型 {repo}，首次使用会自动下载）…")
        if progress:
            progress(0.05)
        result = mlx_whisper.transcribe(str(path), path_or_hf_repo=repo, language="en", word_timestamps=True,
                                        condition_on_previous_text=False, verbose=None)
        segments = result.get("segments", [])
        words = [w for seg in segments for w in seg.get("words", [])]
        return words, [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in segments]
    name = WHISPER_MODEL or MODELS[size][1]
    log(f"本地语音识别中（模型 {name}，首次使用会自动下载）…")
    if progress:
        progress(0.05)
    model = _faster_model(name)
    segs, info = model.transcribe(_audio_16k(path), language="en", word_timestamps=True,
                                  condition_on_previous_text=False, vad_filter=True)
    words, plain = [], []
    for seg in segs:  # a generator: recognition happens while iterating
        plain.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        words.extend({"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                     for w in (seg.words or []))
        if progress and info.duration:
            progress(min(0.99, 0.05 + 0.95 * seg.end / info.duration))
    return words, plain


# ------------------------------------------------------------------ cloud

class CloudError(RuntimeError):
    pass


def _duration(path: Path) -> float:
    out = subprocess.run([_ffmpeg(), "-nostdin", "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                         errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def _multipart(fields: list[tuple[str, str]], file_name: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----ielts" + uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
                 f"Content-Type: audio/mpeg\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _cloud_request(mp3: bytes, settings: AsrSettings) -> dict:
    url, model, key = settings.endpoint()
    if not url:
        raise CloudError("请在设置里填写云端识别的 API 地址。")
    body, ctype = _multipart([("model", model), ("language", "en"), ("response_format", "verbose_json"),
                              ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")],
                             "audio.mp3", mp3)
    headers = {"Content-Type": ctype, "Accept": "application/json", "User-Agent": "ielts-study"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    last = None
    for delay in (0, 3, 8):
        time.sleep(delay)
        try:
            with urlopen(Request(url + "/audio/transcriptions", data=body, headers=headers), timeout=300) as r:
                return json.loads(r.read(20_000_000))
        except HTTPError as error:
            hints = {401: "请检查 API Key。", 403: "请检查密钥权限。", 404: "请检查 API 地址和模型名称。",
                     413: "音频太大。", 429: "请检查服务商的额度或频率限制。"}
            last = CloudError(f"云端识别返回 HTTP {error.code}。{hints.get(error.code, '')}")
            if error.code < 500 and error.code not in (408, 429):
                raise last from None
        except (URLError, OSError, ValueError) as error:
            last = CloudError(f"连不上云端识别服务：{getattr(error, 'reason', error)}")
    raise last


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def punctuate(words: list[dict], segments: list[dict]) -> list[dict]:
    """Cloud word timestamps come without punctuation; take each word's spelling (with punctuation) from the
    segment text, so cues still break at full stops."""
    tokens = [t for s in segments for t in str(s.get("text", "")).split()]
    out, k = [], 0
    for w in words:
        text = str(w.get("word", "")).strip()
        n = _norm(text)
        for look in range(k, min(k + 4, len(tokens))):
            if _norm(tokens[look]) == n and n:
                text, k = tokens[look], look + 1
                break
        out.append({"word": " " + text, "start": float(w["start"]), "end": float(w["end"])})
    return out


def cloud_words(path: Path, settings: AsrSettings, log=print, progress=None) -> tuple[list, list]:
    """(words, segments) from the cloud service, sent as 10-minute MP3 pieces."""
    total = _duration(path) or 1.0
    pieces = max(1, int(-(-total // CHUNK_SECONDS)))
    words, segments = [], []
    log(f"云端语音识别中，共 {pieces} 段…")
    for i in range(pieces):
        start = i * CHUNK_SECONDS
        mp3 = subprocess.run([_ffmpeg(), "-nostdin", "-v", "error", "-ss", str(start), "-t", str(CHUNK_SECONDS),
                              "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-f", "mp3", "-"],
                             capture_output=True, check=True).stdout
        if not mp3:
            break
        reply = _cloud_request(mp3, settings)
        segs = [{"start": float(s["start"]) + start, "end": float(s["end"]) + start, "text": str(s["text"]).strip()}
                for s in reply.get("segments") or []]
        ws = [{**w, "start": float(w["start"]) + start, "end": float(w["end"]) + start} for w in reply.get("words") or []]
        if not segs and reply.get("text"):
            segs = [{"start": start, "end": start + CHUNK_SECONDS, "text": str(reply["text"]).strip()}]
        words += punctuate(ws, segs) if ws else []
        segments += segs
        if progress:
            progress(min(0.99, (i + 1) / pieces))
    return words, segments


# ------------------------------------------------------------------ entry points

def words_and_segments(path: Path, log=print, progress=None) -> tuple[list, list, str]:
    """(words, segments, engine used). Cloud when chosen, with the local model as the fallback."""
    settings = load_settings()
    if settings.engine == "cloud":
        try:
            words, segments = cloud_words(path, settings, log, progress)
            return words, segments, "cloud"
        except (CloudError, subprocess.CalledProcessError) as error:
            log(f"{error} 改用本地模型识别。")
    words, segments = local_words(path, log, progress, settings)
    return words, segments, "local"


def recognize(path: Path) -> dict:
    """A short recording (speaking practice): text, words with timings and, locally, how sure the model was."""
    words, segments, engine = words_and_segments(path, log=lambda m: None)
    out = [{"word": str(w["word"]).strip(), "start": round(float(w["start"]), 2), "end": round(float(w["end"]), 2),
            **({"p": round(float(w["probability"]), 2)} if w.get("probability") is not None else {})}
           for w in words if str(w.get("word", "")).strip()]
    text = " ".join(s["text"] for s in segments if s.get("text")).strip() or " ".join(w["word"] for w in out)
    return {"text": text, "words": out, "duration": round(_duration(path), 2), "engine": engine}
