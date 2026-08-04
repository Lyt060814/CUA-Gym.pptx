# 前作:cua-rl-scaling 的 skill 库,哪些能搬进来

读的是 `/home/yitongli/XLANG/cua-rl-scaling/.claude/skills/`,外加
`desktop_env/task_base.py`(为了把 harness 契约写具体)。
**没读** `pptx-tasks/scaling/pipeline/ops.py`(另一个 agent 在审)。
所有引用都写成 `文件:行号`。

这份文件回答的是同一个问题的四个面:harness 到底要什么、`ppt-pair-authoring`
的方法我们已经做到多少、每个 skill 在"评分器是生成出来的"这个前提下还剩什么、
以及**这个库有几处假设已经被我们量出来的数推翻了**。

前置约束:**提交前不开 VM**。所以库里任何依赖 live rollout 的东西
(smoke gate、rollout gate、difficulty 从 rollout 分数反推)都不在射程内——
但它们**留下的文件**仍然要产出,这一点见第五节最后一条。

---

## 一、Harness 契约(具体到能生成)

### 1.1 目录形状

三个 skill 给的是同一个布局,只有一处分歧(见 1.6):

```text
evaluation_examples/tasks-<owner>/task_<id>/
  task.py            # BaseTask 子类;文件底部 TASK_CLASS = <class>
  metadata.json
  README.md
  assets/            # 上传进 VM、agent 可见
  tests/
    test_task.py
    spec-gate.md  implementation-gate.md  smoke-gate.md  rollout-gate.md
    qa-summary.json
    assets/          # 证据 + 仅测试用的 fixture(含 GT)
```

出处:`task-implementer/SKILL.md:16-30`、`task-spec-creator/SKILL.md:33-46`、
`ppt-pair-authoring/SKILL.md:33-44`。
`task.py` 是**唯一**的可执行任务定义,JSON task config 不再支持
(`task-implementer/SKILL.md:32-33`)。

### 1.2 harness 调什么、按什么顺序

`desktop_env/task_base.py:74-85`:

1. `setup(self, setup_controller, use_proxy: bool = False) -> None`
   —— 环境 reset 后调一次,负责上传文件、启动应用。
2. agent 跑。
3. `evaluate(self, env) -> float | dict`
   —— 返回 float(legacy)或 dict;**返回 dict 时 runner 会把整个 payload 落到
   `result.json`,同时写 legacy 的 `result.txt`**(`task_base.py:78-84`)。

`TASK_CLASS = <class>` 必须在模块底部,loader 靠它找类
(`task-implementer/SKILL.md:76`,`task-tester/SKILL.md:17-19`)。
`BaseTask` 本身是 `dict` 的子类,类属性通过 `_fields()` 列表被拷进实例
(`task_base.py:11-37`),所以 `id / instruction / platform / related_apps /
source / trajectory / proxy` 这些写成类属性即可。

### 1.3 `evaluate()` 的返回形状

```python
return {"score": round(total, 4), "partial_scores": partial_scores}
```

每个 partial 是 `{"score": float, "weight": float, "description": str}`,
**weight 之和必须是 1.0**;`score` 是加权和,round 到 4 位。
(`ppt-pair-authoring/references/authoring-guide.md:212-218`,
参考实现 `assets/pptx_helpers.py:229-242` 的 `assemble_score`。)

硬性数值契约,两条,`task-implementer/SKILL.md:83-87`:

> 6. Score the unchanged post-setup state with no agent action as exactly `0.0`.
>    Do not award partial credit for setup-provided files, default app state, or
>    artifacts that exist before the agent starts working.
> 7. Score the ground-truth/completed reference state as exactly `1.0`.

### 1.4 assets 怎么进 VM

本地路径,不许远程 URL(`task-implementer/SKILL.md:80-82`):

```python
setup_controller._upload_file_setup([
    {"local_path": str(ASSETS_DIR / "template.xlsx"),
     "path": "/home/user/Desktop/template.xlsx"},
])
```

(`task-implementer/SKILL.md:145-155`,模板 `ppt-pair-authoring/assets/task_template.py:66-69`。)
取回文件用 `getters.get_vm_file(env, {"path": ..., "dest": "task_<ID>_result.pptx"})`
(`task-implementer/SKILL.md:166-170`)。

### 1.5 评分前必须先强制保存(这条改变 `setup()`)

`ppt-pair-authoring/SKILL.md:109-117`,展开在 `authoring-guide.md:263-291`:

> The save is the evaluator's responsibility, not the agent's. […] If the
> evaluator pulls the file without first forcing a save, it reads the bytes that
> were on disk before the agent's edits and scores the untouched baseline — a
> false negative that looks exactly like an agent that did nothing. This is the
> most consequential PPT-specific evaluator bug.

三条路径,实现全在 `ppt-pair-authoring/assets/persist_deck.py`:

| 应用 | 做法 | setup 侧的强制要求 |
|---|---|---|
| LibreOffice Impress (Linux) | UNO socket `doc.store()` → `pkill -f soffice` → sleep 3 → `rm .~lock.<name>#` | `setup_controller.launch(["soffice","--norestore", "--accept=socket,host=localhost,port=2002;urp;", path])`。**不能用 `_open_setup`**,它走 xdg-open,注入不了 `--accept`(`persist_deck.py:29-33`) |
| WPS (Linux) | 懒装 xdotool/wmctrl/pyautogui → `xdotool key ctrl+s`,失败回落 wmctrl+pyautogui → `pkill -f wpp/wps` | `setup_controller.launch(["wpp", path])`,同样不用 `_open_setup`(`persist_deck.py:88-95`) |
| Windows | COM `ActivePresentation.Save()` + `taskkill` | `persist_deck.py:128-142` |

**这是一个 evaluate 时的需求反向约束了 setup 的写法**,打包阶段必须一起生成,
不能分开考虑。两个 Linux helper 都不抛异常,返回状态 token 供日志用。

### 1.6 `metadata.json`

`task-spec-creator/SKILL.md:193-207`:

```json
{
  "id": "<id>",
  "instruction": "...",
  "domain": "...",
  "platform": "linux",
  "uses_user_simulator": false,
  "tags": ["..."],
  "related_apps": ["..."],
  "task_path": "task.py",
  "review": {"status": "pending"}
}
```

可选字段:`owner` / `source` / `difficulty` / `expected_artifacts` / `notes`。
`review.status` 初始 `pending`,由 `task-reviewer` 改成 `pass` / `fail`
(`task-spec-creator/SKILL.md:209-212`,`task-reviewer/SKILL.md:172-217`)。

**库内分歧,必须选一边:** GT deck 放哪里。
`ppt-pair-authoring/SKILL.md:38` 和 `scripts/calibrate.py:79-87` 用
`assets/test/gt_*.pptx`;但 `task-spec-creator/SKILL.md:49-50` 明写
"belong under `tests/assets/`, not `assets/test/`",`:102` 再说一遍
"Do not use `assets/test/` for new tasks";`task-tester/SKILL.md:104` 把
`assets/test` 标成 `legacy_test_assets_dir`(for old tasks only)。
**结论:`ppt-pair-authoring` 在这一点上是旧的。生成时一律用 `tests/assets/`,
并让校准脚本收显式路径,不要沿用它的目录猜测逻辑。**

### 1.7 `tests/test_task.py` 的 runner 契约

runner 是 `task-tester/scripts/run_task_tests.py`:导入任务模块 → 导入测试文件 →
**按源码顺序**跑所有顶层 `test_*` 函数 → 抓异常 → 写 JSON/markdown 结果。
不校验 case 类型、期望分数、partial 数学、结果 schema;**函数不抛异常就算过**
(`task-tester/SKILL.md:57-62`)。

runner 可以按名字注入这些 kwarg(`task-tester/SKILL.md:97-106`):
`task_module` / `task_path` / `task_dir` / `tests_dir` / `assets_dir` /
`test_assets_dir` / `legacy_test_assets_dir` / `work_dir`。

**这就是我们的探针电池该产出的形状**:一个 `tests/test_task.py`,里面是几个
平铺的 `test_*` 函数,各自准备自己的 fake env 和 mock。见第四节 3。

---

## 二、`ppt-pair-authoring` 的方法,以及我们已经做到多少

它的流程是 8 步定序(`SKILL.md:53-165`)。逐条对我们:

| 它的步骤 | 我们对应的东西 | 差在哪 |
|---|---|---|
| **2.1 audit the pair** —— 三层 diff(markitdown → soffice+pdftoppm → python-pptx)把 input/target 的差异**还原**出来 | **不需要**。`delta.json` 是**因**不是果:每处改动连同改动前的值都是我们写下去的 | 它在做考古,我们有出生证明。它这一步的全部产物,我们在 `degrade` 时就已经拥有 |
| **2.2 instruction: goal, not procedure** + `lint_instruction.py` | `ppt-task-proposal` skill 写指令;没有 linter | **差一个 linter**,而且我们现在的指令大概率过不了它。见第五节 6 |
| **2.3 supporting assets must look real** + `realism_check.py` | `materialise` 阶段(参考图、打码参考图、CSV、关键帧) | 我们目前产 PNG/CSV,它的检查针对 PDF/docx。**规则本身对我们是休眠的,但一旦 materialise 开始产备忘录就立刻生效** |
| **2.4 evaluator: hard-gate first, 独立 partial** | 还没有(这次要建的第一样) | **全部要抄**,见第四节 |
| **2.5 force-save before reading** | **完全没有**。`grep -riE "force-save\|persist_deck\|get_vm_file" *.md pptxgym/` 在本仓库只命中 REWARD.md:49 一句无关的 Ctrl+S | **这是最大的缺口**,见第六节 |
| **2.6 calibrate(GT=1.0,input≈0)+ 抗 GT-overfit 的 variant 测试** | REWARD.md §五 的五探针里 `scripted_restore` / `input_floor` / `equivalent_repr` 就是这三件事 | **概念上已有,构造方法没有**。它给了具体配方:reorder shapes / paraphrase / re-encode an image(`SKILL.md:124-129`) |
| **2.7 feasibility check(只看 agent 能看到的东西,派子 agent,永不看 GT)** | `solvable` 阶段 + `bundle/` 结构性信息屏障 | **我们更强**。它靠"派一个子 agent,叮嘱它别看 GT";我们靠目录隔离,并且已经知道按文件名做子串匹配的日志扫描会作废 40% 的探针(README:47-52) |
| **2.8 QA gates + submission** | `reconcile` / `solvable` 的 verdict + `attempts/` 归档 | 它的 5 个 gate 里 spec/implementation 可生成,smoke/rollout 要 VM |

**一句话:它的前半程(2.1)我们不需要,后半程(2.4/2.5)我们完全没有,
中间(2.2/2.3/2.6/2.7)我们有等价物但缺它的可执行下限。**

### 它做了而我们没做、且**应该**采纳的

1. **强制保存,以及它对 `setup()` 的反向约束。**(1.5 节)
2. **hard gate 必须窄。** 只覆盖"打不开"和"页数不对";其余一律进加权 partial
   (`pitfalls.md:48-51`)。对一个**从 `delta.json` 生成**的评分器,这条是结构性的:
   delta 有 N 条,天真的生成器会把它们全做成门,于是任何一处没修好就是 0 分。
   **N 条 delta → N 个 partial,不是 N 个 gate。**
3. **抗 GT-overfit 的三种变体构造**(reorder / paraphrase / re-encode)。
4. **返回 dict 的确切形状**(1.3 节)——这不是设计选择,是 harness 的接口。

### 它做了而我们不该采纳的(因为它假设作者是人)

- 三层 audit(2.1)——我们有 delta。
- 一切"证据图必须配解释性散文、不许摆图廊、不许在每个文件里重复同一组截图"的规矩
  (`task-spec-creator/SKILL.md:106-129`、`task-qa-backfill/SKILL.md:47-71`)——
  这是写给**人类审阅者**看的排版纪律,一千个任务不会有人逐个读。
  它的**判断**(证据必须真、不许占位图)要保留,它的**排版程序**不要。
- `task-spec-creator` 开头的 AskUserQuestion 问答(`SKILL.md:12-27`)。
- `pptx_helpers.py` 里的**内容定位**族:`find_idx_with_all`、
  `find_slide_by_standalone_texts`、`find_row_by_label`
  (`pptx_helpers.py:76-127`)。这些存在的理由是"人手写评分器时不知道形状在哪,
  只能按文字找"。我们的 delta 带 `path` / `shape_id` / `name` / `box`。
  **但它们背后的两条判断要留**:group 要递归(`iter_shapes_recursive:30-38`)、
  表格 cell 不是 slide shape(`find_table:103-109`)——因为比对器读的是
  **求解者存回来的文件**,那份文件不是我们写的。

---

## 三、逐 skill 判决

| skill | 判决 | 理由 |
|---|---|---|
| **ppt-pair-authoring**(整体) | **adapt** | 方法论骨架就是我们缺的那三段;但它的目录用旧的 `assets/test/`(1.6),前半程(audit)对我们冗余,QA 段依赖 VM |
| ├ `assets/persist_deck.py` | **原样 reuse** | 逐字抄。skill 自己说 "copy verbatim — they encode bug fixes, local edits reintroduce the bugs"(`SKILL.md:50-51`)。我们要的是 WPS 那条(`:96-125`) |
| ├ `assets/task_template.py` | **adapt** | 改成打包阶段的**发射模板**:结构照抄(UNO/wpp launch → force-save → 窄门 → `assemble_score`),`WEIGHTS` / `DESCRIPTIONS` / partial 函数体由 delta + 比对器注册表填 |
| ├ `assets/pptx_helpers.py` | **部分 obsolete** | reuse:`build_fail_all` / `assemble_score`(:214-242)、`iter_shapes_recursive`(:30-38)、`word_bounded` / `whitespace_flexible`(:187-201)。obsolete:内容定位族(我们有 path)。**存疑**:`check_image_present` 的 `dim_tol=0.05`(:145),见第五节 1 |
| ├ `scripts/audit_pair.py` | **obsolete** | delta.json 让它没有输入端的用处。只在"想独立复核坏文件确实坏成那样"时当事后校验 |
| ├ `scripts/lint_instruction.py` | **adapt** | 规则表(:31-61)可直接跑;但它会在我们现有指令上大量误报/真报,得先决定要不要服从(第五节 6) |
| ├ `scripts/realism_check.py` | **原样 reuse(休眠)** | 阈值明确:Producer 里出现 12 个代码库名之一、embedded font ≤ 1、文件 < 20000 字节,三者任一即 fail(:27-31, :81-86)。materialise 开始产 PDF 备忘录的那天直接接上 |
| ├ `scripts/calibrate.py` | **adapt** | 它的 stub 手法(`_stub_desktop_env:32-55` 把 `get_vm_file` 换成本地 fixture、把 persist helper 中和掉)正是我们探针电池要的;但目录猜测(:77-87)要换成显式路径,判据要收紧(第四节 3) |
| **task-implementer** | **adapt** | `task.py` 七条要求(:74-87)**就是** harness 契约,照着生成。asset 规则(:128-138)、上传/取回范式(:140-184)照用。**证据/截图段(:219-278)在无 VM 下全部 obsolete**。`:156` 的 `_open_setup(path=...)` 示例**对我们是错的**(第五节 3) |
| **task-spec-creator** | **判断 reuse,程序 obsolete** | 保留:`metadata.json` 字段表(:193-207)、README 必需章节与 partial-score 表(:214-247)、spec-gate 必答项(:145-164)。丢弃:AskUserQuestion 开场、证据图排版纪律。它的 spec-gate 清单其实是我们 `reconcile` + `solvable` 已经在答的题,可以当**输出映射表**:reconcile 的 verdict → spec-gate 的 alignment 行,solvable 的 verdict → environment sufficiency + feasibility 行 |
| **task-tester** | **reusable with changes** | **runner 契约(:57-62, :97-106)是我们探针电池的落地形状**——平铺 `test_*`、注入 kwarg、不抛就算过。它强制的三个 case(:141-151:do-nothing 恰好 0.0、GT 恰好 1.0、partial 单列)和我们五探针里三个一一对应。**采纳 runner,生成测试** |
| **task-reviewer** | **判断 reuse,程序 obsolete** | checklist(:98-152)是"人类审阅者会挑什么毛病"的最好成文版,把它变成打包阶段的验收断言。特别是 `:119` "The evaluator rewards the requested user outcome rather than an incidental file, no-op path, hidden answer leak, or brittle formatting artifact"。**difficulty 阈值(:66-77)依赖 rollout 分数+步数,我们不跑 → 只能用 proposal 的 `difficulty`/`est_steps`,并在 metadata 里如实标明来源** |
| **task-qa-backfill** | **obsolete(一条纪律除外)** | 它是给存量任务补证据的一次性工具。唯一要带走的是 `:120-127` 和 `:186-188`:审计不许顺手修任务;gate 不合格就写明 blocked 原因,**不许把占位证据标成 passed**。这条我们的 `reconcile`/`solvable` 已经用"orchestrator 不许写 `task.json`"实现了,属于**互相印证,不需要引入** |
| **ppt-pair-sourcing** | **obsolete(一条除外)** | 语料已解决。唯一要带走的是第 3 关的 **coding floor**(`:120-123`):"reject a pair where a content-blind script that never renders the slide gets full reward",以及边界 "**manipulate, don't construct**"。这**不是**采源属性,是**评分器属性**——见第四节 5 |
| analyze-traj / request-rollout / task-rollout-gate / remote-vm / website-use / distractor-gen | **obsolete** | 全部要 live rollout 或与 PPT 无关。唯一残留:`task-rollout-gate` 的存在是 `tests/rollout-gate.md` 这个文件必须被产出的原因——打包阶段要发一份标 `pending` 的 |

---

## 四、值得留下的评分质量规则(带数字)

### 1. 结构:窄门 + 独立加权 partial

`references/pitfalls.md:48-51`:

> **Over-broad hard gate zeroes correct work.** A locked-region gate that covers
> more than the structural invariants will zero a rollout that did substantial
> correct work but incidentally touched the region. Keep the gate to "won't open"
> and "wrong slide count"; everything else is a weighted partial.

`authoring-guide.md:245-247` 给了它的经验依据:
"tasks with broad gates produce 0.0 at the step cap, while tasks with narrow
gates bank partial credit for the same quality of work."

硬门失败时要 `build_fail_all`:所有 partial 置 0 **但保留 weight 和
description,并把失败原因追加到每条 description 上**,这样日志里仍看得到分解,
而不是一个光秃秃的 0(`pptx_helpers.py:214-226`)。

### 2. Model-eval 的四个数

`ppt-pair-authoring/SKILL.md:106-107`:

> **Model-eval ≤ 0.15**, strict YES/NO, `temperature=0`, `max_tokens=5`,
> try/except → False.

理由(`authoring-guide.md:249-254`):只在"本质是视觉的、没有任何结构检查能覆盖"
时才用,并且权重压在 0.15 以下让规则项主导梯度;任何异常返回 False,
评分器绝不能因为模型超时而崩。

### 3. 校准判据 —— 库内两条互相矛盾,取严的那条

- `ppt-pair-authoring/SKILL.md:121-123`:GT **exactly 1.0, every partial 1.0**;
  未动的 input 在其**记录在案的 near-zero baseline**。
- `scripts/calibrate.py:135-141`:GT ≠ 1.0 → FAIL;input > **0.3** → 只是 WARN。
- `task-implementer/SKILL.md:83-85`:no-agent-action **exactly `0.0`**,
  且明确不许给 setup 产物、默认应用状态、agent 开工前就存在的东西任何 partial credit。

**0.3 的 WARN 和 exactly 0.0 差着 0.3。取 `task-implementer` 的 exactly 0.0**;
REWARD.md §四的 floor normalization 正是一个 delta 驱动的评分器达到它的机制
(减掉坏文件自身得分)。库里没有 floor normalization 这个概念,
**这一条上 REWARD.md 比 skill 库强**。

### 4. 等价容错的具体范式(`pptx_helpers.py`)

| 情形 | 做法 | 行号 |
|---|---|---|
| 图片可能被存盘时重编码(PNG→JPEG) | 先比 blob md5;失败再用 Pillow 比尺寸,`dim_tol=0.05` | `:144-175`(**这条对我们存疑,见五 1**) |
| 日期/页脚常是 `<a:fld>` 字段而非文本 | **二值判**:新值出现 且 旧值消失。不数个数 | `:204-207`,`pitfalls.md:22-25` |
| ≤2 字母的关键词 | 词边界正则 `\bXX\b`(`"AI" in text` 会命中 "rain"/"available") | `:187-195`,`pitfalls.md:27-29` |
| 源串里有双空格 | 空白弹性正则(`"Getting  Help"`) | `:198-201`,`pitfalls.md:31-33` |
| group 里的文字 | 递归 `GroupShape` | `:30-38` |
| 表格 cell | 走 table API,不遍历 `slide.shapes` | `:103-109`,`pitfalls.md:17-20` |

### 5. 反 hacking:库里有的三条

1. **窄门**(上面 1)。
2. **`task.py` 运行时绝不读 `assets/test/`**(`SKILL.md:107`,
   `authoring-guide.md:256-258`)——"an evaluator that reads the GT at runtime is
   leaking the answer into the VM"。
   我们的对应版本在 REWARD.md §七:评分器**可以**读 `delta.json`
   (floor normalization 依赖它),屏障改成"比对器按**算子语义**写,不许看具体配方"。
3. **coding floor**(`ppt-pair-sourcing/SKILL.md:120-123`):
   > reject a pair where a content-blind script that never renders the slide gets
   > full reward (e.g. replace every date)

   **这是第六个探针的规格。** 我们现有五探针里 `blind_solver` 判的是"看不到参照
   还能不能做出来"(答案泄漏);coding floor 判的是"**根本不看渲染图、纯脚本
   启发式**能不能拿满分"。两者不同,后者我们没有。
   同一条还给了任务边界:**manipulate, don't construct** ——
   编辑页上已有的富内容,不要求从零造 SmartArt/morph/公式。

### 6. 抗 GT-overfit 的变体构造

`ppt-pair-authoring/SKILL.md:124-129` / `pitfalls.md:53-57`:

> Run it against 2-3 variant decks that are correct but differ from GT (reorder
> shapes, paraphrase where allowed, re-encode an image); a correct variant scoring
> low means the rubric keys on GT implementation detail, not intent.

这就是 `equivalent_repr` 探针的**可执行配方**,直接抄。

### 7. 库里没有、REWARD.md 有的

floor normalization、对抗电池(`noop`≈0 / `wrong_params` 低)、
"放宽某个容差之后如果 `noop` 分数涨了,那个容差就是错的"、
"防 hacking 优先于覆盖等价解"。**这四条是我们对库的净增量,不要被库的
'加宽容差'反射稀释掉。**

---

## 五、矛盾:这个库假设的、被我们测量推翻的

### 1. "存盘会重编码图片" → `dim_tol=0.05` 这条 5% 的免费带

`pitfalls.md:35-37` 与 `pptx_helpers.py:144-175` 让图片比对在 md5 失败后
回落到 **±5% 的尺寸匹配**。它的依据是 **LibreOffice** 会把 PNG 重压成 JPEG。

我们量到的是:**WPS 打开再保存,10/10 个 deck 动了 0.0% 的形状**
(REWARD.md §2.1)。任务是在 WPS 里被解、被评分的;LibreOffice 那 33% 是
**代理自己的行为,不是环境的行为**(§2.2)。按 REWARD.md §三① ——
"任何比浮点噪声更宽的容差,都要有 WPS 上的实测证据撑着,LO 的 p90 不算证据" ——
**这条 5% 的带宽在我们的环境里是未经证明的送分**。

但**不能就此把它换成"只比 md5"**:REWARD.md §2.1 同时记着,WPS 把 deck0001
的包从 4.04MB 重写成 3.81MB,**81 个 part 的字节不一样,`customXml/` 整个被丢掉**。
**媒体 blob 的 md5 在 WPS 存盘后是否稳定,我们没测过。**
所以 `check_image_present` 的**两层都未经证明**,不是只有第二层。
→ **待办:对 10 个 deck 的 `ppt/media/*` 做一次 WPS roundtrip 前后的 md5 比对。
在那之前,图片类比对项不要进评分。**

### 2. "环境差异 = 存盘时的规范化差异" —— 少了一整个机制

`pitfalls.md:7-11` 和 `authoring-guide.md:310-316` 把本地与镜像的差异全部
归因于 "save-time normalization"(theme color `schemeClr` vs `srgbClr`),
并把唯一的防线放在 **smoke run on the real image**。

我们量到的是另一个机制:**字体名解析到哪张脸**。同一段文字、同一个 4 英寸宽
18pt 开了 autofit 的框,只改 run 上的字体名,存回来的高度在
1.898 / 1.598 / **1.298** 英寸之间跳 —— **一个字没变,高度差 0.600 英寸**;
缺字形的情形另有 0.150 英寸的中心位移(REWARD.md §2.4)。
0.600 英寸是 `POS_TOL = 0.01in` 的 **60 倍**。
关键是 Arial Narrow 那一行:**本机没有这个文件,却解析到一张度量不同的脸** ——
也就是说**光比两端的字体文件清单是不够的**,要对语料里出现的每个字体名跑
`fc-match` 比**解析结果**(REWARD.md §2.4 第 2 步)。

这个库里**没有任何一条 pitfall 提到字体**。而它对付环境漂移的唯一手段
(smoke run)在"提交前不开 VM"下不可用。
→ **替代防线不是更宽的容差,是 REWARD.md §三② + §三③:
autofit 文本框的尺寸从评分项里移除(不是放宽),位置类一律判关系不判绝对坐标。**
第二条对字体差异天然免疫,绝对坐标不是。

### 3. `_open_setup` —— 两个 skill 直接打架

`task-implementer/SKILL.md:156` 把
`setup_controller._open_setup(path="/home/user/Desktop/template.xlsx")`
写成上传+打开的**标准范式**。
`persist_deck.py:29-33` 和 `:88-90` 说,对任何需要强制保存的 GUI 文档任务,
`_open_setup` **必须不用**:它走 xdg-open,注入不了 `--accept`(LibreOffice),
在通用 AMI 上还可能把 `.pptx` 回落给 LibreOffice 而不是 WPS。

→ **生成 `setup()` 时不要照 `task-implementer` 的例子。**
我们的路径是 `setup_controller.launch(["wpp", DECK_VM_PATH])`。

### 4. GT 放 `assets/test/` 还是 `tests/assets/`

见 1.6。`ppt-pair-authoring` 在旧的一边。**选 `tests/assets/`。**
连带影响:`calibrate.py:77-87` 的目录猜测逻辑要整个丢掉。

### 5. QA gate 的通过条件在无 VM 下不可满足

`task-spec-creator/SKILL.md:179-180`:`overall_status` 是 `pass`
**只有当 spec / implementation / smoke 全过**;`smoke` 定义为
"runs on a real AWS image"(`authoring-guide.md:361-369`)。
`task-reviewer/SKILL.md:66-77` 的 `difficulty` 三档阈值
(score ≤0.3 → hard;≤0.5 且 steps ≤150 → medium;≤0.7 且 steps ≤200 → medium;
≤1.0 且 steps ≤200 → easy)全部读 rollout 结果。

**在"提交前不开 VM"下,`overall_status` 永远不可能是 `pass`。**
这不是可以绕开的,是要显式承认的:按库自己的纪律
(`task-qa-backfill/SKILL.md:120-127`、`:186-188`)——
**gate 不合格就写明 blocked 原因和补测计划,绝不许把占位证据标成 passed**。
→ 打包阶段发 `smoke-gate.md` / `rollout-gate.md` 时,
状态写 `blocked`,`blocked_reason` 写"pre-submission VM boot out of scope by
decision; capture plan: <…>",`difficulty` 标明来源是 proposal 的 `est_steps`
而不是 rollout。**不要为了让校验脚本变绿而撒谎。**

### 6. 指令 linter 会在我们现在的指令上大面积开火

`lint_instruction.py:31-61` 的规则里,"positional slide reference"
(`\bslides?\s+\d+`)和 "verbatim value" 类会在 deck0001 的 `task.json`
指令上连续命中:"Slide 4 has lost…"、"Slides 6 and 8"、"On slide 13"、
以及 `y-axis labelled 'Production (kt)'`、`La–Nd / Sm–Tb / Dy–Lu` 这类
**只有指令里才有的确定性来源**。

这不是 linter 坏了,是**两种设计的正面冲突**:
- 库的假设:具体值搬进一份 agent 要读的备忘录,指令只留"目标 + 去哪找"
  (`authoring-guide.md:114-168`);
- 我们的现状:`solvable` 探针要求每处降级的终态**可判定**,而参考图是打码的,
  于是几何和数值被写回了指令。

**两条路,必须选,并写进 `ppt-task-proposal` skill:**
(a) 采纳备忘录范式 —— `materialise` 多产一份"同事的批注/交接说明"资产,
把定位与数值搬进去,指令收回到目标层;`realism_check.py` 正好用上。
(b) 明确拒绝 linter 的定位类规则,并说明理由(打码参考图已经承担了"必须去看
环境"的功能,再多一层间接只是增加步数)。

**不选就是默认走 (b),而且是无意识地走。** 库在这一点上的**判断**是对的
(具体值应该从环境里读,不是从 prompt 里读),值得保留;
它的**正则**未必要照单全收。

---

## 六、三个待建阶段,各自该抄哪一段

| 阶段 | 抄什么 | 出处 |
|---|---|---|
| **评分器(delta → 比对器注册表)** | 返回 dict 形状、`assemble_score` / `build_fail_all`、**窄门**、model-eval ≤0.15、group 递归 + table API、日期二值判、词边界/空白弹性正则 | `authoring-guide.md:212-258`、`pptx_helpers.py` 全文、`pitfalls.md:48-51` |
| **探针电池** | `task-tester` 的 runner 契约(平铺 `test_*` + 注入 kwarg)、`calibrate.py` 的 stub 手法、GT-overfit 三变体配方、**新增 coding-floor 探针** | `task-tester/SKILL.md:57-62,97-106,141-151`、`calibrate.py:32-101`、`SKILL.md:124-129`、`ppt-pair-sourcing/SKILL.md:120-123` |
| **打包** | 目录布局(GT 用 `tests/assets/`)、`metadata.json` 字段、README 必需章节、`task.py` 七条、`_upload_file_setup`、**`launch` + `persist_deck` 逐字抄**、gate 文件按 `blocked` 如实发 | `task-implementer/SKILL.md:16-30,74-87,140-184`、`task-spec-creator/SKILL.md:193-247`、`persist_deck.py` 全文、`task-qa-backfill/SKILL.md:120-127` |

**最该先写的一行代码**:`persist_open_wps_deck` —— 因为它同时决定了
`setup()` 怎么写,而且**我们规划的五个探针一个都测不出它有没有被漏掉**
(五个探针全在文件字节上跑,没有一个经过 GUI 存盘那一步)。
漏了它,评分器读到的是 agent 动手**之前**的字节,分数是坏文件的分数,
看起来和"agent 什么都没做"一模一样。
