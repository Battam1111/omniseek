# Same question, two agents

On 2026-10-11 we asked two agents the same three questions: one with only its built-in web search, one with OmniSeek added. Every quote below is cut from the recorded answers, not reworded.

<p align="center">
  <a href="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-en-light.png?v=2026-10-11d#gh-light-mode-only"><picture>
    <source media="(max-width: 600px)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-en-light-narrow.png?v=2026-10-11d#gh-light-mode-only">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-en-light.png?v=2026-10-11d#gh-light-mode-only" width="1200" alt="Three questions, two agents. One: Mu Li's exact words on the Transformer paper's writing, and the minute. Web search gives a paraphrase and around minutes 1 to 8; OmniSeek quotes him at 1:21:51 with a link that opens there. Two: the top Bilibili comments under that video. Web search sorts them into types with no comment text; OmniSeek returns the comments with their like counts, 1,954 and 1,271. Three: papers that cite both Leviathan 2022 and Chen 2023 on speculative decoding. Web search could not confirm one; OmniSeek's citation graph has 29.">
  </picture></a>
  <a href="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-en-dark.png?v=2026-10-11d#gh-dark-mode-only"><picture>
    <source media="(max-width: 600px)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-en-dark-narrow.png?v=2026-10-11d#gh-dark-mode-only">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-en-dark.png?v=2026-10-11d#gh-dark-mode-only" width="1200" alt="Three questions, two agents. One: Mu Li's exact words on the Transformer paper's writing, and the minute. Web search gives a paraphrase and around minutes 1 to 8; OmniSeek quotes him at 1:21:51 with a link that opens there. Two: the top Bilibili comments under that video. Web search sorts them into types with no comment text; OmniSeek returns the comments with their like counts, 1,954 and 1,271. Three: papers that cite both Leviathan 2022 and Chen 2023 on speculative decoding. Web search could not confirm one; OmniSeek's citation graph has 29.">
  </picture></a>
</p>

**Video (38 seconds):** [the first question, played side by side](https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-en-dark.mp4?v=2026-10-11b)

## Where each answer came from

1. **Mu Li's words and the minute.** The video is [Transformer论文逐段精读【论文精读】](https://www.bilibili.com/video/BV1pu411o7BE) on Bilibili, 87 minutes. OmniSeek transcribed it on the machine that ran the agent and found the line at 1:21:51; the link opens the video at that second. Asked again, it read the saved transcript instead of transcribing again.
2. **Top comments.** The comment section of the same video, read through OmniSeek's Bilibili source with each comment's like count; commenter names are left out here.
3. **Papers citing both.** The citation graph of the two papers on Semantic Scholar. The 29 is counted from the links the tool returned on 2026-10-11; new papers cite them every day, so a later run will count more.

The agent on the left used its own web search on the same day. The question was asked in Chinese for the first row and in English for the other two.
