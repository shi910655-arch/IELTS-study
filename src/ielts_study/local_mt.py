"""Enhanced offline translation: Tencent HY-MT, a translation language model, run on the Mac's GPU with MLX.

Apple Silicon only. The official weights are downloaded from Hugging Face once and converted to a quantized MLX
copy in data/models/; after that it runs fully offline. Other computers, and Macs without the model, keep the
Argos models (translate.py). Measured on held-out subtitles it gets far more lines right than Argos.

Tencent HY is licensed under the Tencent HY Community License Agreement, Copyright © 2025 Tencent. All Rights
Reserved. The trademark rights of "Tencent HY" are owned by Tencent or its affiliate. The license does not apply in
the European Union, the United Kingdom or South Korea (see the NOTICE file written next to the model).
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import threading
from pathlib import Path

from . import config, languages

# The model the app downloads. 7B in 4 bits is about 4.3 GB on disk and 5 GB of memory while translating.
MODEL = {"repo": "tencent/HY-MT1.5-7B", "bits": 4, "group": 64, "download_gb": 16.1, "disk_gb": 4.3,
         "name": "HY-MT1.5-7B"}
MODELS_DIR = config.DATA_DIR / "models"
BATCH = 16  # lines translated at once on the GPU: about 4x faster than one by one
NOTICE = ("Tencent HY is licensed under the Tencent HY Community License Agreement, Copyright © 2025 Tencent. "
          "All Rights Reserved. The trademark rights of “Tencent HY” are owned by Tencent or its affiliate.\n"
          "This license does not apply in the European Union, the United Kingdom or South Korea.\n")

_state = {"status": "", "percent": 0, "error": ""}  # "", downloading, converting, error
_state_lock = threading.Lock()
_model_lock = threading.Lock()


def model_dir() -> Path:
    return MODELS_DIR / f"{MODEL['repo'].split('/')[1].lower()}-mlx-{MODEL['bits']}bit"


def supported() -> bool:
    return config.APPLE_SILICON and importlib.util.find_spec("mlx_lm") is not None


def installed() -> bool:
    folder = model_dir()
    return (folder / "config.json").exists() and any(folder.glob("*.safetensors"))


def ready() -> bool:
    return supported() and installed()


def status() -> dict:
    with _state_lock:
        state = dict(_state)
    if not supported():
        state["status"] = "unsupported"
    elif installed() and state["status"] not in ("downloading", "converting"):
        state["status"] = "ready"
    elif not state["status"]:
        state["status"] = "missing"
    return {**state, "name": MODEL["name"], "downloadGb": MODEL["download_gb"], "diskGb": MODEL["disk_gb"]}


def start_download() -> dict:
    with _state_lock:
        if supported() and not installed() and _state["status"] not in ("downloading", "converting"):
            _state.update(status="downloading", percent=0, error="")
            threading.Thread(target=_download, daemon=True).start()
    return status()


def _cache_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file() and not f.is_symlink()) if folder.exists() else 0


def _download() -> None:
    """Official weights from Hugging Face (resumable, retried), then a quantized MLX copy; the download is
    removed afterwards unless it was already on this computer."""
    import os

    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
    from huggingface_hub import constants, snapshot_download

    cache = Path(constants.HF_HUB_CACHE) / f"models--{MODEL['repo'].replace('/', '--')}"
    had_cache = cache.exists()
    total = MODEL["download_gb"] * 1e9
    done = threading.Event()

    def watch():
        while not done.wait(2):
            with _state_lock:
                if _state["status"] == "downloading":
                    _state["percent"] = min(99, int(_cache_size(cache) / total * 100))

    threading.Thread(target=watch, daemon=True).start()
    try:
        path = None
        for attempt in range(8):  # a stalled transfer fails after the timeout and resumes on the next try
            try:
                path = snapshot_download(MODEL["repo"])
                break
            except Exception:
                if attempt == 7:
                    raise
        done.set()
        with _state_lock:
            _state.update(status="converting", percent=0)
        from mlx_lm import convert

        target = model_dir()
        tmp = target.with_name(target.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        convert(hf_path=path, mlx_path=str(tmp), quantize=True, q_bits=MODEL["bits"], q_group_size=MODEL["group"])
        (tmp / "NOTICE").write_text(NOTICE, encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
        if not had_cache:
            shutil.rmtree(cache, ignore_errors=True)  # 16 GB of originals no longer needed
        with _state_lock:
            _state.update(status="", percent=100, error="")
    except Exception as error:  # noqa: BLE001 - reported in Settings
        done.set()
        with _state_lock:
            _state.update(status="error", error=f"{type(error).__name__}: {error}"[:300])


def _prompt(text: str, lang: str) -> str:
    if languages.is_chinese(lang):
        return f"将以下文本翻译为中文，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
    return f"Translate the following segment into {languages.english_name(lang)}, without additional explanation.\n\n{text}"


def translate(texts: list[str], lang: str = "zh-CN", progress=None) -> list[str]:
    """Translate lines (no context: given its neighbours the model tends to translate them too). The model is
    loaded for the call and released afterwards, so its memory is free while the app is idle."""
    from mlx_lm import batch_generate, load
    from mlx_lm.sample_utils import make_sampler

    import mlx.core as mx

    with _model_lock:
        model, tokenizer = load(str(model_dir()))
        greedy = make_sampler(temp=0.0)
        out: list[str] = []
        try:
            for start in range(0, len(texts), BATCH):
                chunk = texts[start:start + BATCH]
                prompts = [tokenizer.apply_chat_template([{"role": "user", "content": _prompt(t, lang)}],
                                                         add_generation_prompt=True) for t in chunk]
                limits = [min(512, 48 + 4 * len(t.split())) for t in chunk]
                result = batch_generate(model, tokenizer, prompts, max_tokens=limits, sampler=greedy)
                out.extend(re.sub(r"\s+", " ", text).strip() for text in result.texts)
                if progress:
                    progress(min(start + BATCH, len(texts)) / len(texts))
        finally:
            del model
            mx.clear_cache()
    return out


def describe() -> str:
    """Which local engine runs, for "使用…翻译 N 句" in job logs."""
    return "本地增强模型" if ready() else "本地"

