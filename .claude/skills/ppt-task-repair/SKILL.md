---
name: ppt-task-repair
description: Fix a PPT task that reconcile rejected — read the rework directive, change the upstream artefact that caused it, and let the pipeline re-run. Use only when a deck's verdict is needs_rework.
---

# 修复被打回的任务

`reconcile` 判了 `needs_rework`,并在 `task.json` 的 `rework` 里写明了**该退回哪一步**。
你的活儿是**改上游那份产物**,然后让流水线重跑。

**你不判决,你只修。**判决权在 `reconcile`——它会重跑一次,通过与否由它说了算。

---

## 四条不能碰的红线

**1. 不许写 `task.json`。**那是 reconcile 的输出。改它就是**自己给自己发通行证**,
整条链上最后一道独立判断就没了。

**2. 不许改 `source.pptx`。**它是 ground truth。

**3. 不许改 `pptxgym/` 里的任何代码。**你修的是**一个 deck**,而工具是**所有 deck 共用**的。
一次修复回路里改产出器或闸门,影响面远超你手上这个任务,而且没有任何人复核。
最危险的形态很好认:**改闸门让它别再报警,而不是修好 deck**——那看起来和修好了一模一样。

真发现是工具的毛病(确实会有,已经发生过一次),**写进 `repair.md` 就停手**:
说清根因、影响哪些 deck、建议怎么改。这条 deck 停在人工那里是合格结果。
顺带记住:就算改了也不会立刻生效——你的进程早就 import 过那个模块了,
上一次这么干的结果是产出文件出自修复前的旧模块,而日志显示"已修复"。

**4. 不许把任务改小到能过关。**这是最危险的失败模式,而且看起来像成功:
删掉那条过不了的降级、把指令写得含糊一点、把难度降一档——`reconcile` 就通过了,
数据集里多了一个**被稀释过的**样本,没人看得出来。

> **判断标准:修复应该让任务变得可解,而不是变得容易。**
> 如果你的改动让 agent 要做的事变少了,停下来想清楚这是不是在洗白。
> 真的救不回来就在 `repair.md` 里说明,让它停在人工那里——**这是合格的结果。**

---

## 你能改什么

`rework[].stage` 指向哪里,就改哪里:

| stage | 改什么 | 典型情形 |
|---|---|---|
| `materialise` | `proposal.json` 里的 `assets` 声明 | 承诺的参考图没做出来 / 遮罩把该露的东西盖了 / 缺一张必需的素材 |
| `recipe` | `recipe.json` | 删错了东西 / 破坏了 deck 里独一份的内容 / 该局部编辑却整块删了 |
| `proposed` | `proposal.json` 的 degradations 或 instruction | 这处降级本身就立不住(参照物不存在、答案不唯一) |

改完之后**不要**自己跑后续阶段——流水线会把受影响的阶段标为待重跑并自动执行。

---

## 四种最常见的打回,和它们的修法

**① 承诺的素材做不出来。**
先问:这个素材**能不能换个来源**?比如提案要"图表数值 CSV",而原图是位图——
那就把 `assets` 改成给**原图本身**,并在 proposal 的 instruction 里相应改写。
真的没有替代来源,就说明这处降级立不住,退到 `proposed`。

**② 遮罩把该披露的东西盖住了。**
`materialise` 的遮罩是 delta 里所有 bbox 的并集,**它不会判断遮完还剩不剩线索**。
如果被破坏的东西占满了整页,打码图就等于白纸。修法通常是把这处降级的
`disclosure` 从 `reference_image_masked` 改成 `reference_image`,或者改成
`deck_anchor`(如果别的页有同类元素)。

**③ 配方毁掉了 deck 里独一份的内容。**
被删的东西在别处没有副本,又没给参考图 —— 无解。
要么在 `recipe.json` 里缩小破坏范围(留下一个同类的当锚点),
要么在 `proposal.json` 的 `assets` 里补一张那页的参考图。

**④ 该局部编辑却整块删了。**
`smartart` / `chart` 这两个顶层键就是干这个的(见 `ppt-degrade-recipe`)。
把 `recipe.json` 里那条 `delete` 换成对应的局部编辑。

---

## 输出

改完上游产物,再写一份 `repair.md`,append 到已有内容后面(**不要覆盖**):

```markdown
## 第 N 次修复 — <日期>

**打回原因**:<照抄 rework 里的 what>

**改了什么**
- `recipe.json` p19:把整块 `delete` 换成 `smartart.drop_text`,保留其余四格作锚点

**为什么这不是把任务改小**
- 破坏范围没变,agent 仍要重建两格;变的是它现在有参照可依

**没能修的**
- (如果有)…… 以及为什么
```

**"为什么这不是把任务改小"这一节是强制的。**写不出来,通常就说明你正在改小它。

如果这次修不了,`repair.md` 里写清楚卡在哪、需要人做什么决定,然后停手。
