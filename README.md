# CUA-Gym.pptx

把真实的 PowerPoint deck 变成 computer-use agent 的 RL 训练任务。

给一个装满 `.pptx` 的目录,流水线会读懂每个 deck 的结构、判断它该出什么任务、
把任务实现成一份**真正弄坏的文件**,并在每一步用可执行的判据卡住不合格的产物。

```bash
pip install -e .
pptxgym ingest corpus/
pptxgym run --workers 6                       # 6 个 agent 阶段同时跑
pptxgym status
```

**并发分两种货币。**`--workers`(即 `--agent-workers`)限制同时有几个
`claude -p`,`--cpu-workers` 限制同时有几个 soffice / 渲染(默认 cores/4)。
10 个 deck 实测,agent 阶段占了 85% 的墙钟时间,所以一个统一的数字要么饿死渲染、
要么把 API 打爆 —— 两种我们都撞过。deck 本身不限流:槽位按**阶段**申领、用完立刻
归还,所以卡在修复回路里的 deck 不会白占着它没在用的资源。

---

## 它现在做到哪一步

```
ingested → inspected → proposed → recipe → degraded → materialised → reconciled → solvable
            确定性       agent      agent    确定性      确定性         agent       agent

         → scored → hardened → packaged
            确定性    确定性      确定性
```

`materialise` 把指令承诺的素材真的做出来:参考图、**打码参考图**、被删掉的图片、
图表/表格的数值 CSV、动画关键帧。打码那件事看着像判断题,其实不是——
`delta.json` 里记着每处降级的原始 bbox,遮罩就是这些框的并集。

`reconcile` 是最后一道判断关口,回答三个问题:指令描述的破坏和文件里的一致吗?
指令承诺的东西在 `assets/` 里吗?近似之后难度还准吗?产出 `task.json`,
可以判 `needs_rework` 退回。

`solvable` 问的是 reconcile 问不到的问题:**这个任务真的做得出来吗?**
reconcile 判的是一致性,它从头到尾没试过做。所以一个任务可以完美通过 reconcile,
同时是无解、白送、歧义或过定的。

这一步的命门是**信息屏障**:拿着答案键说"能做出来"没有任何信息量。
**屏障是结构性的,不是请求来的**:开跑前先把求解者能拿到的全部东西复制进
`bundle/`(坏文件、`instruction.md`、`assets/`,不含记录了破坏原因的 manifest),
探针只在这个目录里工作。它同时就是任务最终交付的形状,不是脚手架。

日志扫描留作兜底,但规则变了:**判的是"读到了 `bundle/` 外面",不是"字符串里出现了
文件名"**。早先那版按文件名做子串匹配,两个方向都错——
`grep -rn "source.pptx" pptxgym`(读我们自己的代码)被判偷看,
探针在报告里写一句"我没有打开 source.pptx"也被判偷看,
而真去开 `../source.pptx` 的反而看起来干净。**十次探针里有四次是这样被作废的**,
四份有效结论白丢。

它产出的是证据(逐条降级的终态 / 证据 / 不确定项、泄漏清单、实测步数),
不是改好的文件。

### 被打回怎么办

**五道闸门共用一个修复回路,不是五套机制。**`reconcile` 判 `needs_rework`、
`solvable` 判非 `solvable`、`scored` 的 plan 被拒、`hardened` 被攻破、
`packaged` 的一致性检查报 `fail` —— **流水线不会停,也不会假装没看见**。
两个 agent 闸门自己写 `rework`;后三个是确定性的,退回的一律是 `recipe`
(floor 压不下去、攻击拿得到分、指令和文件对不上,说的都是**破坏本身**选错了,
不是破坏得不够好)。`repair`(orchestrator agent)改那份上游产物,Python 把受影响的
下游阶段作废并重跑。最多 3 次,之后标 `needs_human` 停在那里。
**下命令的那份判决会跟着退休**——`solvability.json` / `plan.json` / `attacks.json` /
`consistency.json` 都会被归档后删掉,否则下一轮又会读到同一条抱怨,
在修好的 deck 上一路修到 `MAX_REPAIRS`。

**通过的记号会自己作废,两个方向。**每个阶段记下它读过的东西的内容哈希
(`state.json` 的 `_in`)。上游产物一变——手工重跑、修好的执行器、改过的配方——
下游的 ✓ 立刻变成 `≈ stale` 并重跑,而且**沿着链条传导**:配方一改,
`degraded` 一路到 `packaged` 全部陈旧。哈希按内容,重跑出同样的字节不会造成假作废。

另一个方向哈希看不见:**闸门说"不行"通常不改动任何文件**,于是它下面每一个 ✓
都原封不动。`deck0008` 就正好停在这个状态——reconcile 判了 `needs_rework`,
`solvable` 还挂着上一轮的 ✓,于是整条确定性尾巴可以照常给一个已经被判回的任务
打分、攻击、打包。**没被撤回的判决不等于仍然成立**:现在一个未通过的上游会把
它下面所有记号一起拉成 `stale`。

回路有三道防洗白的锁:

- **orchestrator 不许写 `task.json`** —— 判决是 reconcile 的,改它就是自己发通行证
- **每次重跑前先归档**到 `attempts/<stage>-NN/`,产物和日志都留着 ——
  否则"修好了"和"把判决洗白了"事后分不出来
- skill 强制要求在 `repair.md` 里写一段**"为什么这不是把任务改小"**;
  写不出来通常就说明正在改小它

### `solvable` 之后:三个确定性阶段

以前这三步是**某个人脑子里的一套手工顺序**——三个 deck 就是这么出成任务的。
脑子里的顺序不能续跑、上游一变它不会自己作废,而且最要命的是**它不会拒绝**。
现在它们是阶段,判据可执行,判"不行"就退回 `recipe`。

| 阶段 | 做什么 | 什么时候说不 |
|---|---|---|
| `scored` | 从 `delta.json` 推出 `plan.json`,一处改动一个 component | 原稿不是 1.000、坏文件不是 0.000、某个 component 的 floor 超过 0.15、有降级没人给分 |
| `hardened` | 跑 [attack battery](attack-report.md):14 个作弊 + 6 个**合法变体** | 任何作弊超过阈值,或任何合法解拿不到分,或某个适用的攻击**构造不出来**(没开过火的闸门不算闸门) |
| `packaged` | `consistency` 机械检查 + `emit` 写出可运行任务 | `consistency` 报 `fail`(指令和文件互相矛盾)。`warn` 只记录,不拦 |

`scored` 的两个已知点不需要任何 agent:原稿必然是满分,交给求解者的坏文件必然是零分。
**任何一个不成立,要改的都是配方,不是容差**——理由在 [REWARD.md](REWARD.md),
那里记着量出来的渲染器漂移和字体差异,以及为什么容差正是 reward hacking 的攻击面。

`hardened` 的 `gt_roundtrip` 要真开一次 WPS 窗口,所以没有 WPS 的机器**不能**
hardened 一个任务,只能明说自己没做(`--no-wps`),而那会按"未验证的闸门"退回。

`packaged` 里的 `consistency` 之前**不在任何一条代码路径上**。上一批四条被人读过的
轨迹里有两条死在它能查出来的缺陷上,而 reconcile 两次都放行了。

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
