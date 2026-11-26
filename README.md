<p align="center"><img src="assets/icon-1024.png" width="96" alt=""></p>
<h1 align="center">IELTS Study</h1>
<p align="center"><strong>Prepare for IELTS with videos and articles you want to study.</strong></p>
<p align="center"><a href="README.zh-CN.md" lang="zh-CN" hreflang="zh-CN">简体中文</a> | <a href="README.md" lang="en" hreflang="en" aria-current="page">English</a></p>

![IELTS Study player showing phrase-by-phrase subtitles and a synced transcript](assets/readme/listening.png)

*Practise listening with subtitles that follow the recording.*

IELTS Study is a local study app for macOS, Windows, and Linux. Paste a YouTube, Bilibili, TED, or podcast link to start a listening session. Replay a sentence, look up the words you missed, and save them for review.

## What you can do

- **Video listening.** Follow bilingual captions with word-by-word highlighting and key phrases marked for your level. Loop a sentence, pause at its end, change playback speed, or click a word to look it up. A compatible translation API can put each phrase’s translation underneath it.
- **Dictation and mistakes.** Listen, type, and compare your answer word by word. Sentences below 90% similarity appear in the mistakes notebook for another try with the original audio. The app can keep the relevant clips when you delete a video.
- **Close reading.** Choose original passages at IELTS 5.5, 6.5 or 7+, with CET-4 and CET-6 material for Chinese-speaking learners. You can also paste your own article, toggle sentence translations, look up words, and save sentences.
- **Words and sentences.** Use four tabs for today’s review, word books, your saved words, and saved sentences. Spaced repetition brings words back as fill-in-the-blank cards using sentences you saw while studying, with the original audio when available. Mark a word as known to skip it across the app.
- **Speaking.** Record yourself shadowing a sentence or practise IELTS-style Part 1–3 questions, including a one-minute Part 2 timer. Compare transcripts, pace, pauses, and filler words. An optional API can give feedback against IELTS speaking criteria.
- **Writing.** Practise Task 1 charts, General Training letters, or Task 2 essays. Use the timer, live word count, and autosaved drafts. An optional API can give feedback by criterion, highlight sentences to revise, and suggest a rewritten version.
- **Goals and progress.** Take a vocabulary check for a rough band estimate, or enter your level manually. Set a target band and exam date, adjust daily tasks, and track study streaks and progress.

## Local tools and optional services

- **Translation, recognition and read-aloud.** Use the ECDICT offline dictionary, Argos local translation (lines it is probably wrong about are marked with a “?”) and, on Apple silicon Macs, an optional enhanced offline model, Tencent HY-MT1.5-7B (Settings → Models and services → Local translation; 16 GB download, 4.3 GB after conversion; Tencent HY Community License, not for the EU, UK or South Korea), Whisper speech recognition, and Kokoro read-aloud. Some resources download on first use. The app also supports compatible cloud translation and speech recognition.
- **Languages.** Choose from 16 languages. Set your interface language separately from the language used for translations and explanations.
- **Phone access.** Enable access on the same Wi-Fi and scan a QR code to review words, practise dictation, or read on your phone.
- **Backups.** Export your study history to ZIP, with videos optional. Restore from a ZIP or schedule daily backups to a folder of your choice.

Local study features work without an IELTS Study account. Cloud features send the text or audio needed for the request to the provider you configure. Playback and dictation can also start from links to individual Apple Podcasts or Xiaoyuzhou episodes and direct audio files.

## Screenshots

<table>
<tr>
<td width="50%"><a href="assets/readme/reading.png"><img src="assets/readme/reading.png" alt="An IELTS-style passage with Chinese translations and marked phrases."></a><br><strong>Close reading</strong><br>Read an IELTS-style passage with sentence translations and key phrases.</td>
<td width="50%"><a href="assets/readme/goals.png"><img src="assets/readme/goals.png" alt="The study goals page with a target band and daily task progress."></a><br><strong>Study goals</strong><br>See your target band, study stage, and daily tasks together.</td>
</tr>
</table>

*Screenshots use demo data. The video still was generated for this example.*

## Quick start

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these commands in Terminal or PowerShell.

```bash
git clone https://github.com/shi910655-arch/IELTS-study.git
cd IELTS-study
uv run ielts-study
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in your browser. The first launch installs dependencies and downloads the offline dictionary, so give it a few minutes.

### Install with an AI assistant

Copy the entire prompt below to your AI assistant on the computer where you want to install IELTS Study. You can also use it to update an existing installation.

```text
Please install and launch IELTS Study on this computer.
Project repository: https://github.com/shi910655-arch/IELTS-study.git

1. Identify the operating system, then check that Git and uv are available. If either is missing, follow its official installation instructions for this system.
2. If an IELTS Study checkout already exists, use that directory. Check that origin points to the repository above, and check the branch and working tree. On a clean main branch, run git pull --ff-only. If there are local changes or diverging branch histories, leave them in place and tell me. For a new installation, run git clone https://github.com/shi910655-arch/IELTS-study.git, then enter the IELTS-study directory.
3. From the project directory, run uv run ielts-study. The first launch installs dependencies and downloads and verifies an offline dictionary of about 66 MB. This can take several minutes. Keep waiting while the logs or download show progress. Investigate if an explicit error appears or you confirm progress has stopped.
4. Confirm that http://127.0.0.1:8765 opens in the browser, then report the project path and whether the app started. The launch command stays running while the server is active.
5. Installation and local study require no API key. Do not ask me for any API key, access token, password, or verification code during installation.
```

## License

MIT.
