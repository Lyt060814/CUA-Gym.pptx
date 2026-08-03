# CUA-Gym.pptx

把真实的 PowerPoint deck 变成 computer-use agent 的 RL 训练任务。

给一个装满 `.pptx` 的目录,流水线会读懂每个 deck 的结构、判断它该出什么任务、
把任务实现成一份**真正弄坏的文件**,并在每一步用可执行的判据卡住不合格的产物。

```bash
pip install -e .
pptxgym ingest corpus/
pptxgym run --workers 6
pptxgym status
```

---

## 它现在做到哪一步

```
ingested → inspected → proposed → recipe → degraded → materialised → reconciled
            确定性       agent      agent    确定性      确定性         agent
```

`materialise` 把指令承诺的素材真的做出来:参考图、**打码参考图**、被删掉的图片、
图表/表格的数值 CSV、动画关键帧。打码那件事看着像判断题,其实不是——
`delta.json` 里记着每处降级的原始 bbox,遮罩就是这些框的并集。

`reconcile` 是最后一道判断关口,回答三个问题:指令描述的破坏和文件里的一致吗?
指令承诺的东西在 `assets/` 里吗?近似之后难度还准吗?产出 `task.json`,
可以判 `needs_rework` 退回。

### 被打回怎么办

`reconcile` 判 `needs_rework` 时,**流水线不会停,也不会假装没看见**。判决里
必须带一条机器可读的 `rework`,写明退回哪一步(`proposed` / `recipe` /
`materialise`)。`repair`(orchestrator agent)改那份上游产物,Python 把受影响的
下游阶段作废并重跑,最后重新 `reconcile`。最多 3 次,之后标 `needs_human` 停在那里。

回路有三道防洗白的锁:

- **orchestrator 不许写 `task.json`** —— 判决是 reconcile 的,改它就是自己发通行证
- **每次重跑前先归档**到 `attempts/<stage>-NN/`,产物和日志都留着 ——
  否则"修好了"和"把判决洗白了"事后分不出来
- skill 强制要求在 `repair.md` 里写一段**"为什么这不是把任务改小"**;
  写不出来通常就说明正在改小它

**`reconciled` 之后的阶段(奖励函数、验证、打包)故意还没有。**
那些代码在别处存在,但没有经过批量验证——**没跑过的阶段不该出现在一条要给别人用的
流水线里**,会让人以为它可靠。等一个一个验证过再往里放。

---

## 设计

**判断在哪,skill 就在哪;其余全是脚本。**

skill 会进上下文,脚本只被执行。所以只有两处是 skill:

| skill | 为什么它不能是代码 |
|---|---|
| `ppt-task-proposal` | 哪页值得出题、难度几档、参照放多远、指令怎么写 —— 没有断言能表达 |
| `ppt-degrade-recipe` | 大白话 → 形状 path 要看渲染图,而且要跑一遍再看对不对 |

census / digest / render / degrade / smartart / charts / 完整性闸门全是普通模块,
agent 不需要读,只需要跑。惯用法写在 [TOOLS.md](TOOLS.md),不写在 skill 里。

**agent 文件写岗位契约,skill 写领域判断。**`.claude/agents/*.md` 只说
"你的输入是什么、输出必须是哪个文件、完成判据是什么";怎么想全在 skill 里。
同一个 skill 因此能被批处理和人工模式共用。

**阶段之间只通过文件交接。**没有任何东西存在对话里,所以任何一步都能单独重跑、
能人工接管其中一步再交回去、能断点续跑。

**写配方的人不给自己盖章。**recipe-writer 必须跑一遍配方、渲染出来看,
否则没法知道 path 选对没有——但**执行**和**提交**是两件事。它用
`tools trial` 跑进 scratch 目录,产物和状态都不落地;真正提交由编排层做,
deck 锁会拒掉它对 `pptxgym degrade` 的调用。否则 `status` 里那个 ✓ 是作者自己盖的。

**完成 ≠ 文件存在。**每个 agent 阶段跑完都会被重新校验:提案要能解析、字段齐全、
难度档和步数自洽、引用的页码存在;配方要只用已注册的算子、页码在范围内、不是空操作。
不合格就标 `failed` 并留下日志,不会带着看似合理的垃圾往下走。

---

## 目录

```
.claude/
  agents/    proposer.md  recipe-writer.md          岗位契约,几十行
  skills/    ppt-task-proposal/  ppt-degrade-recipe/  领域判断
pptxgym/
  census.py styles.py text_style.py                 OOXML 解析
  render.py anim_steps.py deck_digest.py            渲染 / 动画 / 结构摘要
  degrade_exec.py smartart.py charts.py             降级执行
  pkg_check.py                                      完整性与答案泄漏闸门
  pipeline.py agent.py cli.py tools.py              状态机 / 无头 agent / CLI
work/<deck-id>/                                     每个 deck 一个目录
```

---

## 一个 deck 目录

```
work/deck0001/
  meta.json  source.pptx  digest.json  digest.min.json  renders/p-NN.png
  proposal.json  recipe.json  input.pptx  delta.json  state.json
  proposed.jsonl  recipe.jsonl            每个 agent 阶段的完整轨迹
```

`source.pptx` 既是输入也是 ground truth,任何阶段都不写它。
`delta.json` 记录每一处改动**连同改动前的值**,所以同一份记录既能造文件、
也描述了求解者要还原什么。

---

## 两个必须知道的机制

**答案泄漏。** 把形状从 spTree 删掉是不够的:图片的位图、SmartArt 的
`data*.xml`(含每个节点的文字)、图表的内嵌工作簿都还活着,`unzip` 就能读到。
执行器会连关系一起清,闸门会复查——`degrade` 输出 `gate=ok` 才算过。

**复合对象要局部编辑。** SmartArt 删一列、图表删一条 series、表格删一行、
文字只改某几段,都有专门入口。整块删会把幸存元素这个**锚点**一起毁掉,
把"照着补齐"变成"从零重建",难度和题意都变了。

---

## 需要什么

- Python 3.10+
- LibreOffice(`soffice`)和 Poppler(`pdftoppm`)—— 渲染用
- Claude Code CLI(`claude`)—— agent 阶段用

无头 agent 默认会走权限确认。批量运行时设 `PPTXGYM_SKIP_PERMISSIONS=1`,
它只在 `work/` 下读写。
