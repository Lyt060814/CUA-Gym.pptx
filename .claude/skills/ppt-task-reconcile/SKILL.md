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
  "verdict_reason": "一句话"
}
```

- `assets` 里的 `file` **必须**是 `assets/` 下真实存在的文件名,流水线会逐个检查
- `instruction_changed` 为 true 时 `notes` 不能空
- `verdict` 判 `needs_rework` 是**合格的答案**:文件和指令对不上而且改指令救不回来,
  就该退回去,不该硬凑成一个能跑但标注错误的任务

---

## 一句话原则

**做不到就如实写,不要假装做到。**

漏报比做不到严重得多。做不到是已知的工具缺口,可以补;
漏报是数据集里混进了一个**标注和内容不符**的样本,
它会一路跑到训练里,而且再也没人查得出来。
