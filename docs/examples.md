# Examples

<sub>[OmniSeek](../README.md) · [Tools](tools.md) · [Configuration](configuration.md) · [FAQ](faq.md)</sub>

Five real queries, real outputs, from a live instance.

---

## 1. One query, four kinds of sources

```
omniseek_search("is DeepSeek R1 actually open source")
```

Back came six results from four different worlds:

- **Simon Willison's blog** broke down DeepSeek V4-Flash benchmarks vs pricing, with an Artificial Analysis scatter plot
- **Two Bluesky posts** gave the practitioner take. Luke Steuber (90 likes): *"it's not actually open source ... Some folks built a decent equivalent with an open, ethical dataset."* Alexander Doria, more precisely: *"Open weights is probably the most straightforward. DeepSeek R1 meets [the Open Source Alliance definition] but not Llama."*
- **Zvi on LessWrong** had a 78,000-character dissection of Kimi K3 that placed DeepSeek in the open-weights landscape
- **Latent Space** covered the V4-Flash post-training update alongside their $70B pre-IPO fundraise
- **GitHub Trending** surfaced an unofficial PyTorch reproduction

One call. A technical blog, social-media debate with engagement numbers, a rationalist deep-dive, an industry newsletter, and a code repo. Google would have given you ten blue links to the same three news articles.

---

## 2. Same tool, Chinese

```
omniseek_search("大模型幻觉问题 最新解决方案")
```

Three Bilibili videos came back, including one with 32,367 views and 76 comments: "什么是大模型幻觉？为什么会产生幻觉？" Each carries a `transcribable` flag, meaning you can pipe it straight to `omniseek_transcribe` and get the spoken content as text (see Example 4).

Also: two ByteDance Seed job postings. Their Dola team's JD says *"攻克模型幻觉问题，研究RAG检索增强生成与长文本处理技术"*. Job descriptions are often more honest than press releases about what a team is actually building.

---

## 3. Behind the wall, into the comments

```
omniseek_read("https://www.xiaohongshu.com/...?xsec_token=...")
```

A xiaohongshu post titled "一些读博期间的强女心态", behind a login wall. OmniSeek navigated with the operator's own browser, on the operator's machine, and brought back the full text (9 mindset tips), 15 carousel images, and 23 comments with sub-reply threads.

The most interesting part wasn't the post. It was this comment:

> **蜜桃乌龙茶:** "不要觉得身边都是好人，第一感觉不好的人一定要警惕，保护好自己的学术成果。"

Protect your academic work from theft. The author never mentioned this. A commenter did, and another confirmed it. Search engines don't index comment sections; even if they could reach this page, they'd return the title and move on.

---

## 4. A video that was never written down

```
omniseek_transcribe("https://www.bilibili.com/video/BV1Uy411i7eQ",
                     start="0", duration="60", language="zh", segments=true)
```

That 32,367-view video from Example 2. Locally, on the machine, no API key, 67 seconds:

> 所以如果我们在微调过程没有控制好，其实也会增加大模型的幻觉。什么是大模型的幻觉，以及它为什么会产生。目前来看，大模型幻觉是阻碍大模型在产业界落地的一个非常重要的原因 ...那简单理解的话，幻觉其实对应到大模型的乱说。但这个乱说呢我们也可以把它分类成几大类型。那第一个分类我们可以把它归类为上下文的矛盾 ...

An expert explaining hallucination taxonomy to 32,000 people. Before this, that knowledge existed only as sound waves inside a video player.

---

## 5. A name becomes a map

```
omniseek_coauthors(["Yejin Choi"], hints=["University of Washington NLP"])
```

Input: a name and a hint. Output: 535 papers, 28,938 citations, current institution (Stanford), and a collaboration network:

| Collaborator | Joint papers |
|-------------|-------------|
| Ximing Lu | 48 |
| Liwei Jiang | 32 |
| Faeze Brahman | 31 |
| Jack Hessel | 29 |
| Ronan Le Bras | 23 |
| Hannaneh Hajishirzi | 22 |
| Maarten Sap | 19 |

Plus who among these also works together: Ximing Lu and Liwei Jiang share 22 papers. Faeze Brahman and Ximing Lu share 16. The clusters inside the group become visible.

An academic would spend an afternoon clicking through Google Scholar to assemble half of this. Your agent gets it in one call.

---

*[Quick start](../README.md#quick-start)*
