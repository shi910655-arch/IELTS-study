"""Offline translation: batching, the Chinese fixes, and the low-confidence mark, with a fake model."""
from types import SimpleNamespace

import pytest

from ielts_study import translate


class FakeModel:
    """Looks like (ctranslate2.Translator, tokenizer, prefix). replies: source -> (translation, score)."""

    def __init__(self, replies, fail_batches=False):
        self.replies, self.fail_batches, self.calls = replies, fail_batches, []

    def encode(self, text):
        return [text]

    def decode(self, tokens):
        return tokens[0]

    def translate_batch(self, tokens, target_prefix=None, **options):
        sources = [t[0] for t in tokens]
        self.calls.append(sources)
        if self.fail_batches and len(sources) > 1:
            raise RuntimeError("bad batch")
        out = []
        for source in sources:
            if source.lower() == "boom":
                raise RuntimeError("bad line")
            text, score = self.replies.get(source, (f"译:{source}", -0.5))
            out.append(SimpleNamespace(hypotheses=[[text]], scores=[score]))
        return out


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel({})
    monkeypatch.setattr(translate, "_model", lambda lang="zh-CN", log=None: (fake, fake, ""))
    return fake


def test_short_replies_skip_the_model(model):
    assert translate.translate_local(["Okay.", "Thank you!", "Oh my God."]) == [
        ("好的。", False), ("谢谢！", False), ("天哪。", False)]
    assert model.calls == []


def test_ielts_terms_are_rephrased_then_corrected(model):
    model.replies["What score will I get in the IELTS oral exam?"] = ("在IELTS口试中我会得到什么分数?", -0.6)
    [(text, low)] = translate.translate_local(["What band score will I get in the IELTS speaking test?"])
    assert text == "在雅思口语考试中我会得到什么分数？" and not low


def test_markup_spaces_and_full_stops_are_cleaned(model):
    model.replies["Good afternoon, my name is Ross."] = ("{\\fn黑体\\fs22}下午好 我叫罗斯.", -0.6)
    model.replies["Things that I could do, you know."] = ("做一些我能做到的事... ... ......", -0.6)
    model.replies["It costs 3.5 dollars."] = ("它要3.5美元.", -0.6)
    model.replies["The number is 43271."] = ("编号为43271.", -0.6)
    model.replies["He lives in the U.S."] = ("他住在U.S.", -0.6)
    assert [t for t, _ in translate.translate_local(
        ["Good afternoon, my name is Ross.", "Things that I could do, you know.", "It costs 3.5 dollars.",
         "The number is 43271.", "He lives in the U.S."])] == [
        "下午好，我叫罗斯。", "做一些我能做到的事……", "它要3.5美元。", "编号为43271。", "他住在U.S."]


def test_a_low_score_line_is_retried_with_a_plainer_source(model):
    model.replies["I'd like you to talk about it."] = ("请师论曰.", -1.8)
    model.replies["I would like you to talk about it."] = ("我希望你谈谈这个。", -0.9)
    model.replies["Blah blah."] = ("胡言乱语", -2.0)
    assert translate.translate_local(["I'd like you to talk about it.", "Blah blah."]) == [
        ("我希望你谈谈这个。", False), ("胡言乱语", True)]
    assert model.calls[-1] == ["I would like you to talk about it."]  # "Blah blah." has no plainer form


def test_repeated_lines_are_translated_once(model):
    result = translate.translate_local(["Hello there.", "", "Hello there."])
    assert result == [("译：Hello there。", False), ("", False), ("译：Hello there。", False)]
    assert model.calls == [["Hello there."]]


def test_other_languages_get_no_chinese_fixes(model):
    model.replies["Okay."] = ("Vale.", -0.5)
    model.replies["The IELTS speaking test."] = ("La prueba oral de IELTS.", -0.5)
    assert [t for t, _ in translate.translate_local(["Okay.", "The IELTS speaking test."], "es")] == [
        "Vale.", "La prueba oral de IELTS."]


def test_a_failing_batch_falls_back_to_one_line_at_a_time(monkeypatch):
    fake = FakeModel({"Hi there.": ("你好。", -0.5)}, fail_batches=True)
    monkeypatch.setattr(translate, "_model", lambda lang="zh-CN", log=None: (fake, fake, ""))
    # the line that fails is retried once; failing again it comes back empty and marked, never silently blank
    assert translate.translate_local(["Hi there.", "boom"]) == [("你好。", False), ("", True)]


def test_cues_get_the_low_mark_and_api_translations_clear_it(model, monkeypatch):
    model.replies["Blah blah."] = ("胡言乱语", -2.0)
    cues = [{"text": "Blah blah.", "seg": [{"s": 0, "e": 4, "zh": "x"}]}, {"text": "Fine words here.", "zhLow": True}]
    progress = []
    assert translate.translate_cues(cues, progress.append) == "local"
    assert cues[0]["zhLow"] is True and "seg" not in cues[0]
    assert "zhLow" not in cues[1]
    assert progress and progress[-1] == 1

    monkeypatch.setattr(translate, "translate_batch_aligned", lambda lines, settings, lang: [("好", [])] * len(lines))
    settings = SimpleNamespace(engine="api")
    assert translate.translate_cues(cues, settings=settings) == "api"
    assert all("zhLow" not in c for c in cues)
