# 同一个问题，两个 Agent

2026-10-11，我们把同样三个问题分别问了两个 Agent：一个只有自带的网页搜索，一个装了 OmniSeek。下面每一句引文都从记录下来的回答里原样截取，没有改写。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-zh-dark.png?v=2026-10-11b">
    <img src="https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-table-zh-light.png?v=2026-10-11b" width="1200" alt="三个问题，两个 Agent。第一题：李沐评价 Transformer 论文写作的原话和分钟。网页搜索给出大意和大约在开头第 1 到 8 分钟；OmniSeek 找到原话在 1:21:51，附从这一秒打开的链接。第二题：这个视频下的 B 站热评。网页搜索只归纳了几类，没有评论原文；OmniSeek 返回评论原文和点赞数，1,954 与 1,271。第三题：同时引用 Leviathan 2022 和 Chen 2023 这两篇推测解码论文的有哪些。网页搜索确认不了一篇；OmniSeek 的引用图里有 29 篇。">
  </picture>
</p>

**短视频（38 秒）：**[第一个问题的左右对照](https://raw.githubusercontent.com/Battam1111/omniseek/main/assets/demo/compare-zh-dark.mp4)

## 每个回答的出处

1. **李沐的原话和分钟。** 视频是 B 站的 [Transformer论文逐段精读【论文精读】](https://www.bilibili.com/video/BV1pu411o7BE)，87 分钟。OmniSeek 在跑 Agent 的那台机器上把它整段转成文字，找到原话在 1:21:51，链接从这一秒打开视频。再问一次时，它直接读存下的文字稿，不再重转。
2. **热评。** 同一个视频的评论区，经 OmniSeek 的 B 站来源读取，每条带点赞数；评论者的名字这里不放。
3. **同时引用两篇的论文。** 这两篇论文在 Semantic Scholar 上的引用图。29 这个数是从工具在 2026-10-11 返回的引用关系里数出来的；每天都有新论文引用它们，以后再跑会更多。

左边的 Agent 在同一天用的是它自带的网页搜索。第一题用中文问，后两题用英文问。
