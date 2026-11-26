<p align="center"><img src="assets/icon-1024.png" width="96" alt=""></p>
<h1 align="center">IELTS Study</h1>
<p align="center"><strong>用你想看的视频和文章，练英语、备考雅思。</strong></p>
<p align="center"><a href="README.zh-CN.md" lang="zh-CN" hreflang="zh-CN" aria-current="page">简体中文</a> | <a href="README.md" lang="en" hreflang="en">English</a></p>

![IELTS Study 精听播放器，展示短语对照字幕和同步原文](assets/readme/listening.png)

*逐句精听时，字幕会跟着原声同步高亮。*

IELTS Study 是一款在本机运行的英语学习工具，支持 macOS、Windows 和 Linux。贴入 YouTube、Bilibili、TED 或播客链接，就可以逐句精听、查生词，把想掌握的词留到之后复习。

## 主要功能

- **视频精听**。看双语字幕和逐词高亮，按自己的水平查看重点短语和难词。支持单句循环、逐句暂停、调速和点词查义。配置兼容的翻译 API 后，可在英文短语下方显示对应译文。
- **听写与错句本**。听一句、打一句，提交后逐词比对。相似度低于 90% 的句子会进错句本，可以听原声再练。删除视频时，应用可以保留错句和收藏单词的原句片段。
- **文章精读**。内置雅思 5.5、6.5、7+ 分级文章，也为中文学习者提供四级、六级短文。可粘贴自己的文章，逐句看译文、点词查义，收藏有用的句子。
- **单词与句子**。通过「今日复习」「词书」「我的生词」「句子收藏」管理学习内容。间隔复习把词带回原句做填空，有可用原声时也能回听。在任一页面标记「认识」，其他页面也会跳过这个词。
- **口语练习**。录音跟读，或练习雅思风格的 Part 1–3 题目。Part 2 有一分钟准备计时。可查看识别文本、语速、停顿和口头禅，配置 API 后还能获得按雅思口语维度给出的练习反馈。
- **写作练习**。练习 Task 1 图表作文、通用类书信和 Task 2 大作文。支持计时、实时字数统计和草稿自动保存；配置 API 后可获得分项反馈、逐句纠错和参考改写。
- **备考目标与进度**。用词汇测试粗估当前水平，也可手动设置分数。填写目标分数和考试日期后，可以调整每日任务、查看连续学习天数和进度。

## 本地工具与可选服务

- **离线翻译**。不联网，也不产生 API 费用。所有电脑都能用 Argos，可能译错的句子旁会标一个“?”。Apple 芯片的 Mac 还可以在 设置 → 模型与服务 → 本地翻译 里下载腾讯混元 HY-MT1.5-7B（下载约 16 GB，转换后占 4.3 GB；使用腾讯混元社区许可证，不适用于欧盟、英国和韩国）。在从没见过的新视频里随机抽 120 句盲评，它的准确率是 93%，Argos 是 74%。
- **API 翻译**。接入 DeepSeek、OpenAI 等兼容接口后，英文下方会按短语显示译文，口语和写作也能得到 AI 反馈。设置页会显示每次请求用了多少 token，DeepSeek 还会估算费用；已保存的密钥随时可以删除。
- **词典、识别与朗读**。提供 ECDICT 离线词典、Whisper 本地语音识别和 Kokoro 朗读。部分资源在首次使用时下载，也可以用云端语音识别。
- **界面与译文语言**。支持 16 种语言，界面语言可以和译文、释义语言分别设置。
- **手机访问**。开启后，同一 Wi-Fi 下扫码即可在手机上复习单词、听写或阅读。
- **备份与恢复**。学习记录可导出为 ZIP，视频可选。可以从 ZIP 恢复，也能每天自动备份到指定文件夹。

本地学习功能无需 IELTS Study 账号。启用云端功能时，处理所需的文字或音频会发送给你配置的服务商。也可以导入 Apple 播客、小宇宙的单集链接和音频直链。

## 界面预览

<table>
<tr>
<td width="50%"><a href="assets/readme/reading.png"><img src="assets/readme/reading.png" alt="带中文对照和短语标注的雅思风格阅读文章"></a><br><strong>文章精读</strong><br>逐句看译文，查看重点短语。</td>
<td width="50%"><a href="assets/readme/goals.png"><img src="assets/readme/goals.png" alt="目标分数、备考阶段和每日任务进度"></a><br><strong>备考计划</strong><br>集中查看目标分数、备考阶段和每日任务。</td>
</tr>
</table>

*截图使用演示数据，视频画面为生成示例。*

## 快速开始

安装 Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在终端或 PowerShell 中运行：

```bash
git clone https://github.com/shi910655-arch/IELTS-study.git
cd IELTS-study
uv run ielts-study
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。第一次启动会安装依赖并下载离线词典，请预留几分钟。

### 交给 AI 安装

把下面整段复制给要安装 IELTS Study 的那台电脑上的 AI 助手。这段提示词也可以用来更新已有安装。

```text
请在这台电脑上安装并启动 IELTS Study。
项目仓库：https://github.com/shi910655-arch/IELTS-study.git

1. 确认系统是 macOS、Windows 或 Linux，再检查 Git 和 uv。缺少工具时，按对应系统的官方安装说明安装。
2. 如果已有 IELTS Study 仓库，沿用原目录。确认 origin 指向上面的仓库，并检查分支和工作区。工作区干净且位于 main 时，执行 git pull --ff-only 更新。若有本地修改或分支历史分叉，请保留现状并告诉我。如果还没有仓库，执行 git clone https://github.com/shi910655-arch/IELTS-study.git，再进入 IELTS-study 目录。
3. 在项目目录运行 uv run ielts-study。首次启动安装依赖、下载并校验约 66 MB 的离线词典，需要几分钟属正常。日志或下载仍在推进时请继续等待；出现明确错误或确认没有进展后再排查。
4. 确认浏览器能打开 http://127.0.0.1:8765，并告诉我项目路径和启动结果。服务正常启动后，启动命令会持续运行。
5. 安装和本地学习无需 API Key。整个安装过程中不得向我索要 API Key、访问令牌、密码或验证码。
```

## 许可证

MIT.
