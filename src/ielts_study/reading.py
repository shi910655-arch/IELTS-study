"""Reading: paste an English article → sentences with key-point marks, word glosses and Chinese translation.

Articles live in ROOT/articles/<id>/data.json, apart from the video library. Each sentence is shaped like a
video cue ({"i", "p", "text", "zh", "marks", "notes", "gl", "seg"}) so annotation and translation are shared.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
import uuid

from . import local_mt, usage
from .config import ARTICLES_DIR

MAX_CHARS = 40_000
# Articles have their own queue: they are quick (no download or transcription), so they should not
# wait behind a long video import that holds the pipeline's lock.
_article_lock = threading.Lock()
_annotate_lock = threading.Lock()
ARTICLE_ID = re.compile(r"^a[0-9a-f]{12}$")


def new_id() -> str:
    return "a" + uuid.uuid4().hex[:12]


def split_sentences(text: str) -> list[dict]:
    """Paragraphs are separated by line breaks; spaCy splits each paragraph into sentences."""
    from .annotate import nlp

    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\r?\n", text)]
    paragraphs = [p for p in paragraphs if p]
    cues = []
    for p, doc in enumerate(nlp().pipe(paragraphs, batch_size=32)):
        for sent in doc.sents:
            s = sent.text.strip()
            if s:
                cues.append({"i": len(cues), "p": p, "text": s})
    return cues


def article_path(aid: str):
    return ARTICLES_DIR / aid / "data.json"


def load_article(aid: str) -> dict | None:
    if not ARTICLE_ID.match(aid):
        return None
    try:
        return json.loads(article_path(aid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_article(data: dict) -> None:
    f = article_path(data["id"])
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


def delete_article(aid: str) -> bool:
    if not ARTICLE_ID.match(aid) or not (ARTICLES_DIR / aid).is_dir():
        return False
    shutil.rmtree(ARTICLES_DIR / aid)
    return True


def list_articles() -> list[dict]:
    items = []
    if not ARTICLES_DIR.exists():
        return items
    for d in ARTICLES_DIR.iterdir():
        data = load_article(d.name)
        if not data:
            continue
        cues = data.get("cues", [])
        items.append({
            "id": data["id"], "title": data.get("title") or "未命名文章", "created": data.get("created", 0),
            "sentenceCount": len(cues), "wordCount": sum(len(c["text"].split()) for c in cues),
            "preview": " ".join(c["text"] for c in cues[:2])[:160], "translator": data.get("translator"), "translatorModel": data.get("translatorModel") or "", "lang": data.get("lang", "zh-CN"),
            "readAt": data.get("readAt", 0),
            # 分级阅读: a built-in passage ("graded": its id) or one the API wrote ("ai"), and its level
            "level": data.get("level"), "graded": data.get("graded"), "ai": bool(data.get("ai")),
            "topic": data.get("topic", ""),
        })
    items.sort(key=lambda x: -x["created"])
    return items


def run_article_job(pipeline, job, text: str, title: str, extra: dict | None = None) -> None:
    """Runs on the pipeline's worker thread: split → translate → annotate → save."""
    from . import meanings
    from .annotate import ANNOTATION_VERSION, annotate_cues
    from .pipeline import friendly_error, learner_language
    from .settings import load_settings
    from .translate import translate_cues

    def log(msg: str):
        msg = str(msg).strip()
        if msg:
            job.log.append(msg)

    with _article_lock:
        try:
            pipeline._set(job, "split")
            cues = split_sentences(text)
            if not cues:
                raise RuntimeError("没有找到英文句子。")
            job.title = title or (cues[0]["text"][:60] + ("…" if len(cues[0]["text"]) > 60 else ""))
            settings = load_settings()
            pipeline._set(job, "translate")
            log(f"使用{'API' if settings.engine == 'api' else local_mt.describe()}翻译 {len(cues)} 句…")
            lang = learner_language()
            spent = usage.snapshot()
            translator = translate_cues(cues, lambda p: setattr(job, "percent", p), settings=settings, log=log,
                                        lang=lang)
            pipeline._set(job, "annotate")
            annotate_cues(cues, pipeline.dic, progress=lambda p: setattr(job, "percent", p), lang=lang)
            meanings.fill_notes(cues, lang, settings, log)
            log(usage.describe(usage.since(spent)))
            save_article({
                "id": job.video_id, "title": job.title, "created": int(time.time()), "translator": translator,
                "translatorModel": settings.model if translator != "local" else "",
                "annotation": ANNOTATION_VERSION, "lang": lang, "cues": cues, **(extra or {}),
            })
            pipeline._set(job, "done", 1.0)
            log("完成 ✓")
        except Exception as e:
            job.error = friendly_error(str(e))
            job.stage = "error"
            log(traceback.format_exc(limit=3))


def ensure_article_annotation(data: dict, dic) -> dict:
    """Re-mark key phrases and hard words for articles saved with an older annotation format."""
    from .annotate import ANNOTATION_VERSION, annotate_cues

    if data.get("annotation") == ANNOTATION_VERSION:
        return data
    with _annotate_lock:
        fresh = load_article(data["id"]) or data  # another request may have upgraded it already
        if fresh.get("annotation") != ANNOTATION_VERSION:
            annotate_cues(fresh["cues"], dic, lang=fresh.get("lang", "zh-CN"))
            fresh["annotation"] = ANNOTATION_VERSION
            save_article(fresh)
    return fresh


def run_retranslate_job(pipeline, job) -> None:
    from . import meanings
    from .annotate import ANNOTATION_VERSION, annotate_cues
    from .pipeline import friendly_error, learner_language
    from .settings import load_settings
    from .translate import translate_cues

    with _article_lock:
        try:
            data = load_article(job.video_id)
            if not data:
                raise RuntimeError("找不到这篇文章。")
            settings = load_settings()
            lang = learner_language()
            if data.get("lang", "zh-CN") != lang:
                pipeline._set(job, "annotate")
                annotate_cues(data["cues"], pipeline.dic, lang=lang)
                data["annotation"] = ANNOTATION_VERSION
            pipeline._set(job, "translate")
            job.log.append(f"使用{'API' if settings.engine == 'api' else local_mt.describe()}翻译 {len(data['cues'])} 句…")
            spent = usage.snapshot()
            same_language = data.get("lang", "zh-CN") == lang  # then a line that fails keeps its old translation
            data["translator"] = translate_cues(data["cues"], lambda p: setattr(job, "percent", p),
                                                settings=settings, log=job.log.append, lang=lang, keep=same_language)
            data["translatorModel"] = settings.model if data["translator"] in ("api", "api+local") else ""
            meanings.fill_notes(data["cues"], lang, settings, job.log.append)
            if spent_line := usage.describe(usage.since(spent)):
                job.log.append(spent_line)
            data["lang"] = lang
            save_article(data)
            pipeline._set(job, "done", 1.0)
        except Exception as e:
            job.error = friendly_error(str(e))
            job.stage = "error"
            job.log.append(traceback.format_exc(limit=3))


def retranslate_article(pipeline, aid: str):
    from .pipeline import Job

    key = f"retranslate-article:{aid}"
    with pipeline._jobs_lock:
        for job in pipeline.jobs.values():
            if job.url == key and job.stage not in ("done", "error"):
                return job
        job = Job(url=key)
        job.video_id = aid
        pipeline.jobs[job.id] = job
    threading.Thread(target=run_retranslate_job, args=(pipeline, job), daemon=True).start()
    return job


def submit_article(pipeline, text: str, title: str, key: str = "article", extra: dict | None = None):
    from .pipeline import Job

    job = Job(url=key)
    job.video_id = new_id()
    job.title = title or "新文章"
    with pipeline._jobs_lock:
        pipeline.jobs[job.id] = job
    threading.Thread(target=run_article_job, args=(pipeline, job, text, title, extra), daemon=True).start()
    return job
