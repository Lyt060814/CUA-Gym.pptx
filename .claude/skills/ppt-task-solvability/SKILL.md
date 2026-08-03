---
name: ppt-task-solvability
description: Decide whether a degraded PPT task can actually be solved — is the required end state determinate from what the solver is given, or does something have to be guessed. Produces evidence, not a fixed file.
---

# 这个任务真的做得出来吗?

`reconcile` 判的是**一致性**:指令和文件说的是不是同一件事。
它从头到尾没试过做这个任务,所以抓不到四类问题:

| | |
|---|---|
| **无解** | 每条承诺都兑现了,但信息本身不足以定出唯一答案 |
| **白送** | 答案以静态检查看不出来的方式泄漏了 |
| **歧义** | 存在多个都说得过去的终态 |
| **过定** | 素材给多了,任务退化成照抄 |

你的活儿就是把这四类找出来。

---

## 你只能看求解者能看的东西

| 给你 | 明确禁止 |
|---|---|
| `input.pptx` —— 坏掉的文件 | `source.pptx` —— 原稿 |
| `task.json` 的 `instruction` 和 `assets` | `delta.json` —— 逐条改动 |
| `assets/` 目录里的素材 | `recipe.json` —— 怎么弄坏的 |
| | `proposal.json` —— 原始意图 |

**这不是纪律问题,是这一步能不能成立的前提。**看了 delta 就是在抄答案键,
之后你说"能做出来"没有任何信息量。

**流水线会扫你的日志。**读了禁止清单里的任何一份,这一步直接判失败,不看你的结论。

---

## 不要真的去修

不要写 `input.pptx`,不要写任何输出文件之外的东西。

**你要产出的是答案键,不是改好的文件。**逐条降级回答三个问题:

1. **终态必须是什么样?** 具体到能判分的程度
2. **什么证据把它钉死了?** 页内的同类元素?参考图?指令里的数字?
3. **哪些部分我定不下来?** 说清楚是哪一部分、为什么

第 3 条是这一步最有价值的输出。**写不出证据的,就是欠定。**

允许用 `python -m pptxgym.tools shapes <deck-dir>` 看坏文件的结构,
也允许解压 `input.pptx` 翻 XML —— 求解者也能这么干,而且如果这样能翻出答案,
那正是要报的"白送"。

---

## 四类判定的具体判据

**无解 / 欠定。**某处降级的终态,你无法从给你的东西里推出来。
典型:唯一的那份内容随着形状一起被删了,deck 里别处没有副本,也没给参考图。

**白送。**满足任一条:
- 解压 `input.pptx` 能读到被删内容的文字或数据
- 别的页上有一个完全等价的孪生元素,照抄即可
- 给的参考图直接把答案画出来了,而这处降级的 `disclosure` 本该是打码或描述

前两条是 bug,要报。第三条要看 `task.json` 里那处降级的 `disclosure`
是不是本来就打算这么给 —— 是的话不算问题。

**歧义。**你能想出两个都说得过去、但成品明显不同的终态。
说清楚是哪两个,以及需要什么才能消歧。

**过定。**素材直接把答案摆出来,任务退化成照抄,agent 学不到东西。

---

## 顺带估一个步数

按 `ppt-task-proposal` 里的量级估这个任务在 GUI 里实际要多少步:
补一张卡片 ~30,重画一组标注 ~60,建一张图表 ~90,整页重建 ~150。

和 `task.json` 里的 `est_steps` 差一档以上就报出来 ——
难度目前是提案阶段拍的,你是第一个基于**真实坏文件**估它的人。

---

## 输出:`solvability.json`

```json
{
  "verdict": "solvable | undetermined | leaked | ambiguous | overdetermined",
  "verdict_reason": "一句话",
  "degradations": [
    {"id": "d1", "slides": [4],
     "end_state": "第 4 页要重新出现那张世界产量图,位置和原来一致",
     "evidence": "assets/p04-Picture-3.emf 就是原图;打码参考图给了它的边框位置",
     "determinate": true,
     "undetermined": ""}
  ],
  "leaks": [
    {"what": "解压 input.pptx 能在 ppt/diagrams/data5.xml 里读到被删的五条法律名",
     "where": "ppt/diagrams/data5.xml"}
  ],
  "est_steps_measured": 240,
  "est_steps_declared": 290,
  "rework": [
    {"stage": "materialise",
     "what": "第 15 页需要一张参考图",
     "why": "那四句话随 SmartArt 一起没了,deck 里别处没有,求解者无从得知"}
  ]
}
```

- `verdict` 不是 `solvable` 时,**`rework` 必填**,而且要指明退回
  `proposed` / `recipe` / `materialise` 中的哪一步 —— 流水线照着它决定重跑什么
- `leaks` 有内容时,即便每处降级都 determinate,verdict 也应该是 `leaked`
- **判"做不出来"是合格的答案。**硬说能做,等于往数据集里塞一个无解样本

---

## 一句话原则

**你不修任务,你只报告它的状态。**

"做不出来"会触发修复回路,而修复最省事的办法就是**把任务改简单**。
所以你的报告要精确到"缺的是哪一样东西",而不是"太难了" ——
前者能被补上,后者只会被用来削任务。
