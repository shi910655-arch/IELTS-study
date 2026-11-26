"""The enhanced offline model: when it is used, how its output is checked, and Argos as the fallback."""
import pytest

from ielts_study import local_mt, translate


@pytest.fixture
def enhanced(monkeypatch):
    replies = {}
    monkeypatch.setattr(local_mt, "ready", lambda: True)
    monkeypatch.setattr(local_mt, "translate", lambda texts, lang="zh-CN", progress=None: [replies.get(t, "好的译文。") for t in texts])
    argos_calls = []

    def argos(texts, lang="zh-CN", progress=None, log=None):
        argos_calls.append(list(texts))
        return [(f"标准：{t}", False) for t in texts]

    monkeypatch.setattr(translate, "_translate_argos", argos)
    return replies, argos_calls


def test_the_enhanced_model_translates_when_installed(enhanced):
    replies, argos_calls = enhanced
    replies["The hippocampus loses 5% of its neurons every decade."] = "海马体每十年会失去5%的神经元。"
    assert translate.translate_local(["The hippocampus loses 5% of its neurons every decade.", "Okay."]) == [
        ("海马体每十年会失去5%的神经元。", False), ("好的。", False)]
    assert argos_calls == []


def test_a_line_failing_the_checks_falls_back_to_argos(enhanced):
    replies, argos_calls = enhanced
    replies["There's nothing wrong with your English, you gave good answers."] = "你的英语有问题，你给了好的答案。"
    replies["We had 20 students in 2019."] = "我们在2019年有学生。"  # the 20 went missing
    out = translate.translate_local(["There's nothing wrong with your English, you gave good answers.",
                                     "We had 20 students in 2019."])
    # both are caught: the negation went missing in the first, the 20 in the second
    assert argos_calls == [["There's nothing wrong with your English, you gave good answers.", "We had 20 students in 2019."]]
    assert out[0] == ("你的英语有问题，你给了好的答案。", True)  # Argos has no negation either: kept, with a "?"
    assert out[1] == ("标准：We had 20 students in 2019.", False)  # Argos keeps the 20: it wins


def test_an_error_in_the_enhanced_model_falls_back_to_argos(monkeypatch):
    monkeypatch.setattr(local_mt, "ready", lambda: True)

    def broken(texts, lang="zh-CN", progress=None):
        raise RuntimeError("GPU busy")

    monkeypatch.setattr(local_mt, "translate", broken)
    monkeypatch.setattr(translate, "_translate_argos", lambda texts, lang="zh-CN", progress=None, log=None: [("标准", False)] * len(texts))
    logs = []
    assert translate.translate_local(["Hello there."], log=logs.append) == [("标准", False)]
    assert "改用标准离线翻译" in logs[0]


def test_status_reports_what_this_computer_can_do(monkeypatch, tmp_path):
    monkeypatch.setattr(local_mt, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(local_mt, "supported", lambda: False)
    assert local_mt.status()["status"] == "unsupported"
    monkeypatch.setattr(local_mt, "supported", lambda: True)
    assert local_mt.status()["status"] == "missing"
    folder = local_mt.model_dir(); folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}"); (folder / "model.safetensors").write_bytes(b"x")
    assert local_mt.status()["status"] == "ready"


def test_a_retranslation_keeps_old_lines_the_enhanced_model_cannot_translate(monkeypatch):
    monkeypatch.setattr(local_mt, "ready", lambda: True)
    monkeypatch.setattr(local_mt, "translate", lambda texts, lang="zh-CN", progress=None: ["" if "Joseph" in t else "新的译文。" for t in texts])
    monkeypatch.setattr(translate, "_translate_argos", lambda texts, lang="zh-CN", progress=None, log=None: [("", True)] * len(texts))
    cues = [{"text": "The candidate is Joseph.", "zh": "考生是约瑟夫。", "start": 0, "end": 1},
            {"text": "Let us begin the test.", "zh": "旧译文", "start": 1, "end": 2}]
    translate.translate_cues(cues, lang="zh-CN", keep=True)
    assert cues[0]["zh"] == "考生是约瑟夫。" and cues[1]["zh"] == "新的译文。"
