---
name: ppt-task-reconcile
description: Check a degraded PPT task against its own instruction — does the broken file still match what the solver is told, are the promised assets there, is the difficulty still honest — and produce the final task record. Use after materialise, as the last gate before a task is considered real.
---

# 对账:指令说的,和文件里发生的,还是同一件事吗?

指令是在**提案**阶段写的,那时还没有文件。之后配方阶段可能近似了某些东西,
素材阶段可能只做出了一部分。**没有人回头核对过。**

你的活儿就是核对,然后产出这个任务的最终记录 `task.json`。

**你是这条链上最后一道人为判断的关口。**过了你,这个任务就被当成真的了。

---

## 你要看的四样东西

| 文件 | 看它干什么 |
|---|---|
| `proposal.json` | 指令原文、当初声明要给哪些素材、每处降级本来打算破坏什么 |
| `recipe.json` | 实际做了什么,**尤其是每一步的 `_why` 里写的近似和跳过** |
| `delta.json` | 逐条改动,连同改动前的值 |
| `assets/manifest.json` | 实际做出来的素材 |

外加**渲染图**:`python -m pptxgym.tools pair <deck-dir> <页号...>`
—— 原稿和坏文件逐页对照。**必须看**,这是唯一能验证"文件真的坏成指令说的样子"的办法。

**动手之前先跑一遍机器对账**:`python -m pptxgym.consistency <deck-dir>`

它把下面这些问题里**能判定的那部分**先跑一遍 —— 指令声称坏掉的东西在
`delta.json` / 两个文件的差集里找不找得到、ground truth 自己能不能通过这道题、
发出去的素材是不是就是答案、被删的图求解者拿不拿得到、有没有改动不属于任何一处降级。
它报的每一条 `fail` 都必须在 `task.json` 里交代掉。

**但"它不报"推不出"没问题"**:难度准不准、遮罩够不够、"knocked apart"
这种措辞是不是描述了真实的那处破坏,它一概判不了。那些还是得你自己看。

---

## 四个必须回答的问题

### 1. 指令描述的破坏,和文件里的破坏一致吗?

逐条比对指令里提到的每一处和 `delta.json`。典型的对不上:

- **指令说"补齐缺的那一档",实际整块删了。**配方近似成整块删除时,
  幸存元素这个锚点没了,任务从"照着补齐"变成"从零重建"。
  指令必须改写,而且难度多半要上调。
- **指令说"某几页",实际动的是另几页。**配方在等价页里换了一个。
- **指令描述的破坏根本没发生**(配方跳过了那条)。这时要么删掉指令里那一段,
  要么把整个任务退回。

上一批十个任务发出去跑过真模型。读了四条轨迹,其中**两个就是死在这一节上,
而且都是从这道关口放行的**:

- **指令说少了一张图,ground truth 里两张图都在。**(deck0004 第 9 页)
  指令写的是"the illustrations that sat above the other two went with them",
  实际只掉了 SmartArt 的节点,`delta.json` 第 9 页除了 `smartart_drop_nodes`
  一条没有,两张图在坏文件里原样躺着。模型花了几十步找那张不存在的图,
  最后为了让这一页说得通,把真的 SmartArt 删了 —— 0.63 的活儿判 0 分。
  **指令里每出现一句"少了什么",就去 `delta.json` 里找产生它的那一条改动;
  找不到,就是它没发生。**

- **指令要求"第 6 页必须是一张真表格,不是贴上去的图片",而 ground truth 就是一张图片。**
  (deck0006)那一页的原对象是 `picture`,随任务发出去的参考图 `p06--2.png`
  和它逐字节相同,奖励里唯一那个可得分项认的就是这张图的字节。
  **照指令做得 0 分,唯一能得分的做法正是指令禁止的那一种。**模型照做了,得 0。

**这一节真正的判据是两句话,必须同时成立:**

> **一、ground truth 必须是它自己这道题的一个合法解。**
> 指令里任何一句"最后应该是什么样"的要求,都拿 `source.pptx` 去对一遍;
> 对不上,这道题就没有正确答案。**凡是指定对象类型的措辞** ——
> "a real, editable table"、"a native chart"、"not a pasted picture"、
> "rather than a screenshot" —— **落笔之前先去看 ground truth 那个东西的
> `kind` 到底是 `picture` 还是 `table`/`chart`/`smartart`。**
> 是 `picture` 就不许这么写,写了就是给任务设了一个无解的终点。
>
> **二、指令里每一句"什么坏了",都必须对应 `delta.json` 里一条真实存在的改动。**
> 对不上就删掉指令里那句话,或者退回 `recipe`;不要靠改写措辞把它糊过去。

同源的三个坑,都能直接从已有产物里看出来,顺手一起查了:

- **发出去的素材本身就是答案。**参考图 / 原始位图和被删对象逐字节相同,
  单看是**允许的**(裁下来的扫描件画不出来,只能给)。
  但**一旦指令同时要求"重建成别的东西"**,这两条要求就互斥了 ——
  贴文件得分、照指令做不得分,就是 deck0006。
- **被删的图,求解者根本拿不到。**那张图的字节既不在 `assets/`,
  `input.pptx` 里也没有任何一页还在画它 —— 那就只有 `source.pptx` 有。
  于是 ground truth 自己会被 media 闸门判成"把原始素材贴回去了"。
  要么把它放进 `assets/`,要么改成一个不需要那些字节的目标。
- **`delta.json` 里有条目不带 `deg`。**这条改动不属于任何一处降级,
  指令里也就没有一句话对得上它。奖励阶段会照着它给"没人要求过的活儿"打分,
  而求解者无从知道要做它。

### 2. 指令承诺的东西,在 `assets/` 里吗?

指令里凡是写了 "the reference image shows…"、"the logo file is provided"、
"the data is below" 这类话,就是对求解者的承诺。
**逐句找出来,逐句去 `assets/manifest.json` 里核对。**

承诺了但没有 → 要么改指令不再承诺(如果任务不靠它也能解),
要么这个任务不成立。**不要假装它在。**

### 3. 反过来:指令有没有泄题?

素材做出来之后要重新问一遍:

- 给了完整渲染图的那一页,答案是不是就等于给了?这是**允许的**,
  但要确认提案当初就是这么设计的(`disclosure: reference_image`),
  而不是本来打算给打码图、结果做成了完整图。
- 打码图遮住的区域够不够?**去看那张图**。如果被破坏的东西还露着一角,
  或者旁边有个没被遮住的孪生元素直接把答案摆出来了,就得说明。

### 4. 难度还准吗?

近似会改变工作量。整块删除比补齐缺口重得多。
按提案里的量级重新估:补一张卡片 ~30 步,重画一组标注 ~60 步,
建一张图表 ~90 步,整页重建 ~150 步。

改了就在 `notes` 里说明为什么改。**≤100 easy / 100–300 medium / 300+ hard。**

---

## 改指令的规矩

和提案阶段完全一致,一个字都不放松:

**只说目标状态,不说操作步骤。**

| ❌ | ✅ |
|---|---|
| "把第二张图片向左移 200pt" | "The images on this slide are out of order" |
| "选中标题,填充改成 #A92D55" | "Some elements no longer match the deck's colour scheme" |

- 不给精确数值,除非那个数值本身就是提供给 agent 的信息
- 可以说清哪几页有问题,这不算泄题
- 保持真实工作场景的口吻,不要写成"任务:请执行以下操作"
- **能不改就不改。**改动越小越好,你是在对账不是在重写。

---

## 输出:`task.json`

```json
{
  "name": "<沿用提案里的 task name>",
  "instruction": "英文指令原文 —— 核对过、必要时改过的版本",
  "instruction_changed": true,
  "difficulty": "medium",
  "est_steps": 290,
  "assets": [
    {"kind": "reference_image", "file": "reference-p13.png", "slide": 13,
     "masked": false, "why": "这页的两栏图版布局只存在于这一页"}
  ],
  "degradations": [
    {"id": "d1", "slides": [4], "implemented": "as_proposed|approximated|skipped",
     "what_the_file_looks_like": "一句话:这一页现在是什么样",
     "note": "近似了什么、代价是什么(可留空)"}
  ],
  "notes": "改了指令的原因、难度调整的原因、剩下的已知弱点",
  "verdict": "ready|needs_rework",
  "verdict_reason": "一句话",
  "rework": [
    {"stage": "materialise",
     "what": "第 15 页需要一张参考图 —— 四句话随 SmartArt 一起没了,deck 里别处没有",
     "why": "五分之一的任务答案求解者无从得知,改指令补不上"}
  ]
}
```

- `assets` 里的 `file` **必须**是 `assets/` 下真实存在的文件名,流水线会逐个检查
- `instruction_changed` 为 true 时 `notes` 不能空
- `verdict` 判 `needs_rework` 是**合格的答案**:文件和指令对不上而且改指令救不回来,
  就该退回去,不该硬凑成一个能跑但标注错误的任务
- **判 `needs_rework` 时 `rework` 必填**,而且要写明**退回哪一步**:
  `materialise`(素材没做出来 / 遮罩盖错了)、`recipe`(删错了东西)、
  `proposed`(这处降级本身立不住)。流水线照着它决定重跑哪些阶段;
  只写一段散文没人能照着动手,校验器会拒

---

## 一句话原则

**做不到就如实写,不要假装做到。**

漏报比做不到严重得多。做不到是已知的工具缺口,可以补;
漏报是数据集里混进了一个**标注和内容不符**的样本,
它会一路跑到训练里,而且再也没人查得出来。
