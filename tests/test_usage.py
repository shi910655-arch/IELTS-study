"""API usage records and the rule that a paid-for failure is never sent again."""
import io
import json
from datetime import datetime, timezone

import pytest

from ielts_study import translate, usage
from ielts_study.settings import TranslationSettings

DEEPSEEK = TranslationSettings(engine="api", provider="deepseek", base_url="https://api.deepseek.com",
                               model="deepseek-flash", api_key="k")
OPENAI = TranslationSettings(engine="api", provider="openai", base_url="https://api.openai.com/v1", model="m", api_key="k")
PEAK = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)      # Monday 10:00 in Beijing
OFF_PEAK = datetime(2026, 11, 22, 2, 0, tzinfo=timezone.utc)  # Sunday
REPLY_USAGE = {"prompt_tokens": 1000, "completion_tokens": 2000, "prompt_cache_hit_tokens": 200,
               "completion_tokens_details": {"reasoning_tokens": 500}}


def test_deepseek_requests_get_a_cost_at_its_published_prices():
    usage.record(DEEPSEEK, "subtitles", REPLY_USAGE, now=PEAK)
    usage.record(DEEPSEEK, "subtitles", REPLY_USAGE, now=OFF_PEAK)
    data = json.loads(usage.PATH.read_text(encoding="utf-8"))["days"]
    peak = (200 * 0.04 + 800 * 2.0 + 2000 * 8.0) / 1e6
    rows = [row["subtitles"] for row in data.values()]
    assert sorted(round(r["cost"], 8) for r in rows) == [round(peak / 2, 8), round(peak, 8)]
    assert rows[0] | {"cost": 0} == {"requests": 1, "input": 1000, "cached": 200, "output": 2000, "reasoning": 500,
                                     "timeouts": 0, "unpriced": 0, "cost": 0}


def test_other_providers_are_counted_in_tokens_only():
    usage.record(OPENAI, "writing", {"input_tokens": 10, "output_tokens": 20}, now=PEAK)
    month = usage.summary(now=PEAK)["month"]
    assert (month["input"], month["output"], month["cost"], month["unpriced"]) == (10, 20, 0, 1)


def test_summary_splits_today_month_and_purpose():
    usage.record(DEEPSEEK, "subtitles", REPLY_USAGE, now=datetime(2026, 10, 30, 2, tzinfo=timezone.utc))
    usage.record(DEEPSEEK, "subtitles", REPLY_USAGE, now=OFF_PEAK)
    usage.record(DEEPSEEK, "meanings", REPLY_USAGE, now=PEAK)
    s = usage.summary(now=PEAK)
    assert (s["today"]["requests"], s["month"]["requests"], s["all"]["requests"]) == (1, 2, 3)
    assert set(s["byPurpose"]) == {"subtitles", "meanings"}
    assert s["since"] == "2026-10-30"


def test_a_job_log_line_shows_the_difference():
    before = usage.snapshot()
    assert usage.describe(usage.since(before)) == ""
    usage.record(DEEPSEEK, "subtitles", REPLY_USAGE, now=PEAK)
    line = usage.describe(usage.since(before))
    assert line.startswith("API 用量：1 次请求 · 输入 1,000 token · 输出 2,000 token · 其中思考 500 token · 约 ¥0.02")


def test_a_timeout_is_counted_and_marked_as_billed(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise TimeoutError("timed out")

    monkeypatch.setattr(translate, "build_opener", lambda *handlers: Opener())
    with pytest.raises(translate.TranslationError) as raised:
        translate._completion(["Hello."], DEEPSEEK)
    assert raised.value.billed
    assert usage.summary()["today"]["timeouts"] == 1


def test_answered_requests_are_recorded_under_their_purpose(monkeypatch):
    reply = {"choices": [{"message": {"content": "[\"x\"]"}}], "usage": REPLY_USAGE}

    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(translate, "build_opener", lambda *handlers: Opener())
    translate._completion(["Hello."], DEEPSEEK, purpose="examples")
    assert usage.summary()["byPurpose"]["examples"]["output"] == 2000


@pytest.fixture
def local(monkeypatch):
    def fake_local(cues, progress=None, done=0, total=None, lang="zh-CN", log=None, keep=False):
        for c in cues:
            c["zh"] = "本地"
    monkeypatch.setattr(translate, "_translate_local", fake_local)
    monkeypatch.setattr(translate, "_sleep", lambda s: None)


def test_a_paid_failure_is_not_sent_again(monkeypatch, local):
    calls = []

    def aligned(lines, settings, lang):
        calls.append(lines[0])
        if lines[0] == "line 0":
            raise translate.TranslationError("格式不对", billed=True)
        return [(f"译{line}", []) for line in lines]

    monkeypatch.setattr(translate, "translate_batch_aligned", aligned)
    monkeypatch.setattr(translate, "API_PARALLEL", 1)
    cues = [{"text": f"line {i}"} for i in range(20)]
    logs = []
    assert translate.translate_cues(cues, settings=DEEPSEEK, log=logs.append) == "api+local"
    assert calls == ["line 0", "line 10"]  # the failed batch was sent once
    assert cues[0]["zh"] == "本地" and cues[10]["zh"] == "译line 10"
    assert logs[:2] == ["⚠ 格式不对", "第 1–10 句改用本地翻译：这批已经计费，为免重复付费不再重发。"]


def test_two_paid_failures_stop_the_remaining_batches(monkeypatch, local):
    calls = []

    def aligned(lines, settings, lang):
        calls.append(lines[0])
        raise translate.TranslationError("翻译 API 太久没有回应，这次请求可能已经计费。", billed=True)

    monkeypatch.setattr(translate, "translate_batch_aligned", aligned)
    monkeypatch.setattr(translate, "API_PARALLEL", 1)
    cues = [{"text": f"line {i}"} for i in range(40)]
    logs = []
    assert translate.translate_cues(cues, settings=DEEPSEEK, log=logs.append) == "local"
    assert len(calls) == 2
    assert any("2 批请求已经计费却没有用上，为免继续扣费，其余字幕改用本地翻译" in line for line in logs)


def test_free_failures_are_still_retried(monkeypatch, local):
    calls = []

    def aligned(lines, settings, lang):
        calls.append(1)
        if len(calls) == 1:
            raise translate.TranslationError("翻译 API 返回 HTTP 503。", retryable=True)
        return [("好", [])] * len(lines)

    monkeypatch.setattr(translate, "translate_batch_aligned", aligned)
    cues = [{"text": "hello"}]
    assert translate.translate_cues(cues, settings=DEEPSEEK) == "api"
    assert len(calls) == 2
