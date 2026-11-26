"""Offline translation, phase one: omissions are caught and retried sentence by sentence, terms follow context,
contractions are spelled out only when unambiguous, and a failed retranslation never wipes out a good one."""
import copy
from types import SimpleNamespace

import pytest

from ielts_study import translate

CONSISTENT = "helps you stay consistent And consistency is what truly changes your English over time Now let's"


class Model:
    """A fake (translator, tokenizer, prefix): replies maps source -> (translation, score); `raises` lists sources
    that throw; `boom_batches` makes every multi-line batch throw."""

    def __init__(self, replies=None, raises=(), boom_batches=False):
        self.replies, self.raises, self.boom_batches, self.calls = dict(replies or {}), set(raises), boom_batches, []

    encode = staticmethod(lambda text: [text])
    decode = staticmethod(lambda tokens: tokens[0])

    def translate_batch(self, tokens, target_prefix=None, **options):
        sources = [t[0] for t in tokens]
        self.calls.append(sources)
        if self.boom_batches and len(sources) > 1:
            raise RuntimeError("batch")
        out = []
        for source in sources:
            if source in self.raises:
                raise RuntimeError("line")
            text, score = self.replies.get(source, ("这是一句完整而通顺的中文译文。", -0.5))
            out.append(SimpleNamespace(hypotheses=[[text]], scores=[score]))
        return out


@pytest.fixture
def model(monkeypatch):
    fake = Model()
    monkeypatch.setattr(translate, "_model", lambda lang="zh-CN", log=None: (fake, fake, ""))
    return fake


# ------------------------------------------------------------------ catching omissions

def test_the_known_omission_is_retried_sentence_by_sentence(model):
    # the model answered with invented subtitle styling and two words, and scored it well
    model.replies[CONSISTENT] = ("{\\fn黑体\\fs22}我们来", -0.51)
    model.replies["Helps you stay consistent"] = ("帮助你保持一致", -0.8)
    model.replies["And consistency is what truly changes your English over time"] = ("而一致性才是随着时间真正改变你英语的东西", -0.9)
    model.replies["Now let us"] = ("现在让我们", -1.0)
    [(text, low)] = translate.translate_local([CONSISTENT])
    assert not low
    assert "坚持" in text and "改变你英语" in text and "我们来" not in text
    assert model.calls[-1][-3:] == ["Helps you stay consistent",
                                    "And consistency is what truly changes your English over time", "Now let us"]


def test_an_empty_translation_is_marked(model):
    model.replies["The meeting has been moved to another room."] = ("", -0.4)
    assert translate.translate_local(["The meeting has been moved to another room."]) == [("", True)]


def test_a_long_line_with_a_tiny_translation_is_marked_when_no_retry_helps(model):
    line = "people who are trying to learn a new language often give up much too early because it feels slow"
    model.replies[line] = ("人们", -0.4)
    model.replies[line[:1].upper() + line[1:]] = ("人们", -0.4)
    assert translate.translate_local([line]) == [("人们", True)]


def test_a_short_natural_translation_passes(model):
    model.replies["I couldn't agree more."] = ("完全同意", -0.6)
    assert translate.translate_local(["I couldn't agree more."]) == [("完全同意", False)]
    assert len(model.calls) == 1  # no second round


def test_names_numbers_and_addresses_do_not_count_as_words(model):
    line = "Visit www.example.com or call 020 7946 0958, Monday to Friday, Hong Kong, Taipei and London."
    model.replies[line] = ("访问www.example.com或致电020 7946 0958，周一至周五。", -0.6)
    assert translate.translate_local([line])[0][1] is False
    assert translate._content_words(line) < 12


def test_a_missing_number_is_caught(model):
    model.replies["You need 6.5 in every part of the test."] = ("你每部分都需要分数。", -0.6)
    model.replies["You need 6.5 in every part of the test"] = ("你每部分都需要分数。", -0.6)
    assert translate.translate_local(["You need 6.5 in every part of the test."])[0] == ("你每部分都需要分数。", True)


def test_a_line_without_a_reliable_boundary_is_kept_whole():
    assert translate._pieces("I think the most important thing is to keep going even when it is hard") == []
    assert translate._pieces("Learn How to Think In English") == []
    assert translate._pieces("I passed. Then I celebrated with my friends.") == ["I passed.", "Then I celebrated with my friends."]


# ------------------------------------------------------------------ contractions: only when the meaning is certain

@pytest.mark.parametrize("source, plain", [
    ("She'd already left.", "She had already left."),
    ("She'd like to leave.", "She would like to leave."),
    ("They'd finished the work.", "They had finished the work."),
    ("You'd better go.", "You had better go."),
    ("It's been a long day.", "It has been a long day."),
    ("It's great.", "It is great."),
    ("I'd read it.", "I'd read it."),          # had or would: left alone
    ("That's done.", "That's done."),          # is or has: left alone
    ("He's gone home.", "He's gone home."),
    ("I ain't going.", "I ain't going."),
])
def test_contractions_are_expanded_only_when_unambiguous(source, plain):
    assert translate._plain_source(source) == plain


# ------------------------------------------------------------------ terms by context

@pytest.mark.parametrize("source, raw, expected", [
    ("HubSpot released the ChatGPT prompt engineering playbook.", "HubSpot发布了ChatGPT即时工程游戏本。", "HubSpot发布了ChatGPT提示词工程指南。"),
    ("Thanks for the prompt reply.", "谢谢你的及时答复。", "谢谢你的及时答复。"),
    ("Download the free marketing playbook.", "下载免费营销游戏本。", "下载免费营销指南。"),
    ("The coach opened his playbook.", "教练打开了他的游戏本。", "教练打开了他的战术手册。"),
    ("Consistency is the key to learning English.", "一致性是学习英语的关键。", "坚持是学习英语的关键。"),
    ("The colors in this design are consistent.", "这种设计中的颜色是一致的。", "这种设计中的颜色是一致的。"),
])
def test_terms_follow_their_context(model, source, raw, expected):
    model.replies[source] = (raw, -0.5)
    assert translate.translate_local([source])[0] == (expected, False)


# ------------------------------------------------------------------ protecting translations

def cues_with_timing():
    return [{"start": 1.0, "end": 2.5, "text": "Hello there, how are you today?", "wt": [[1.0, 1.2], [1.3, 1.5]],
             "zh": "你好，你今天好吗？", "seg": [{"s": 0, "e": 5, "zh": "你好"}]},
            {"start": 2.5, "end": 4.0, "text": "The meeting has been moved to another room.", "wt": [[2.5, 2.7]],
             "zh": "会议已改到另一个房间。"},
            {"start": 4.0, "end": 6.0, "text": "Good luck with your exam next week.", "wt": [[4.0, 4.4]]}]


def test_same_language_retranslation_keeps_old_lines_that_fail_now(model):
    model.replies["The meeting has been moved to another room."] = ("", -0.4)
    cues = cues_with_timing()
    before = copy.deepcopy(cues)
    translate.translate_cues(cues, lang="zh-CN", keep=True)
    assert cues[1]["zh"] == "会议已改到另一个房间。" and "zhLow" not in cues[1]   # kept
    assert cues[0]["zh"] == "这是一句完整而通顺的中文译文。" and "seg" not in cues[0]  # replaced
    assert cues[2]["zh"]
    for old, new in zip(before, cues):  # the subtitle itself never changes
        assert (old["start"], old["end"], old["text"], old["wt"]) == (new["start"], new["end"], new["text"], new["wt"])


def test_a_new_language_never_shows_the_old_translation(model):
    model.replies["The meeting has been moved to another room."] = ("", -0.4)
    cues = cues_with_timing()
    translate.translate_cues(cues, lang="zh-CN", keep=False)
    assert cues[1]["zh"] == "" and cues[1]["zhLow"] is True


def test_a_failing_retry_round_keeps_the_first_round(model, monkeypatch):
    model.replies[CONSISTENT] = ("{\\fn黑体}我们来", -0.5)
    real = translate._run_model

    def run_model(m, sources):
        if any(s.startswith("Helps you") for s in sources):
            raise RuntimeError("second round broke")
        return real(m, sources)

    monkeypatch.setattr(translate, "_run_model", run_model)
    [(text, low)] = translate.translate_local([CONSISTENT, ][:1])
    assert (text, low) == ("我们来", True)  # first round kept, and marked


def test_one_failing_line_does_not_stop_the_others(monkeypatch):
    fake = Model({"Second line is here.": ("第二句在这里。", -0.5)}, raises={"Broken line here.", "Broken line here"},
                 boom_batches=True)
    monkeypatch.setattr(translate, "_model", lambda lang="zh-CN", log=None: (fake, fake, ""))
    out = translate.translate_local(["Broken line here.", "Second line is here.", "Third line is also here."])
    assert out[0] == ("", True)
    assert out[1] == ("第二句在这里。", False)
    assert out[2][0] and not out[2][1]


# ------------------------------------------------------------------ added after the first evaluation round

def test_an_invented_number_is_retried_without_the_dangling_words(model):
    model.replies["so between 1982 and"] = ("因此，在1982年至2001年期间，", -0.97)
    model.replies["So between 1982"] = ("所以在1982年之间", -0.64)
    assert translate.translate_local(["so between 1982 and"]) == [("所以在1982年之间", False)]


def test_number_words_and_parts_of_numbers_are_not_invented():
    assert not translate._invented_numbers("Benny had five years of German.", "本尼学了5年德语。")
    assert not translate._invented_numbers("She got 7.5 overall.", "她总分7.5。")
    assert translate._invented_numbers("to 2010 in a", "截至2010年12月31日的")


def test_a_dropped_negation_is_caught_but_idioms_are_not(model):
    line = "not a picky eater so if I have to pick my favorite food I think it is Vietnamese food"
    model.replies[line] = ("所以如果我要选我最喜欢的食物，应该是越南菜", -0.7)
    model.replies["Not a picky eater so if I have to pick my favorite food I think it is Vietnamese food"] = (
        "我不挑食，所以如果要选最喜欢的食物，我想是越南菜", -0.8)
    assert translate.translate_local([line]) == [("我不挑食，所以如果要选最喜欢的食物，我想是越南菜", False)]
    model.replies["I can't wait to see you."] = ("我迫不及待想见你。", -0.5)
    model.replies["You did it, I couldn't agree more."] = ("你做到了，完全同意。", -0.5)
    assert translate.translate_local(["You did it, I couldn't agree more."]) == [("你做到了，完全同意。", False)]


def test_bands_are_scores_not_groups(model):
    model.replies["students from score five and six up to the sevens"] = ("学生从5分到6分到7分", -0.8)
    assert translate.translate_local(["students from band five and six up to the sevens"])[0][0] == "学生从5分到6分到7分"
