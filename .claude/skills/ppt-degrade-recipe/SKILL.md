---
name: ppt-degrade-recipe
description: Turn a PPT task proposal into an executable degradation recipe — pick the shape paths, choose the ops, run it, look at the render, fix what is wrong. Use after ppt-task-proposal, when a proposal has to become a real broken file.
---

# 把提案写成可执行的配方

提案写的是大白话:"把六张成员卡片打散"、"删掉第 4 页的图表,标题留着"。
你的活儿是把它变成一份**能跑的配方**,并且**跑完真的看一眼**结果对不对。

这一步和提案是**两层,不能混**。提案层不考虑实现难度——那是刻意的,
免得好想法被工具能力削掉。到了你这里,实现难度才第一次变成问题。

---

## 输入

| 文件 | 是什么 |
|---|---|
| `proposal.json` | 要实现的东西。看 `tasks[0].degradations`,每条的 `what_breaks` 就是规格 |
| `digest.json` | 每页的形状清单,**`path` 就是配方里的地址** |
| `renders/p-NN.png` | 渲染图 |
| `source.pptx` | 原始 deck,也是 ground truth,**永远不要写它** |

## 输出

`recipe.json`,加上**你亲眼确认过**降级效果符合提案。

---

## 第一条纪律:先看图,再定 path

digest 里的 `path 3` 是什么,**只有对着渲染图才认得出**。
光看 JSON 选 path,选错的概率很高,而且错了不报错——只是删了别的东西。

推荐顺序:

1. 读 `proposal.json` 的每条 `what_breaks`,知道要破坏什么
2. 打开对应页的渲染图**看**
3. 从 digest 里按位置、尺寸、文字、图片哈希把形状对上号
4. 写配方,跑,**再渲染一次看结果**
5. 不对就改 path 重来

第 4 步不是可选的。

---

## 配方格式

```json
{"name": "<提案里的 task name>", "seed": 41,
 "slides": {
   "7":  [{"op": "delete", "deg": "d1", "paths": ["3", "13"],
           "_why": "三个里程碑的照片"}],
   "12": [{"op": "scatter", "deg": "d2", "paths": ["4", "6"],
           "amplitude_in": 1.2, "_why": "..."}]
 },
 "smartart": [{"slide": 19, "deg": "d4", "drop_text": ["Ingest"], "_why": "只删第三列"}],
 "chart":    [{"slide": 5, "deg": "d1", "drop_name": ["Data Size"], "_why": "少一条 series"}],
 "reorder_slides": {"deg": "d3", "swap": [[3, 4]]},
 "clear_notes": [{"deg": "d3", "slides": [7, 8]}],
 "layout": [{"deg": "d2", "layout": "Title and Content", "delete_paths": ["1"]}]}
```

- `slides` 的键是 **1-based 页号**,和渲染图、和 `shapes` 命令一致
- **每一步都要写 `deg`**,填 `proposal.json` 里那条降级的 `id`(`d1`/`d2`…)
- **每一步都要写 `_why`**,写你选这些 path 的依据(比对了哪张渲染图、哪个哈希)
- `seed` 让 `scatter` 可复现

---

## `deg`:一条改动属于哪条降级

`deg` 是**指令和文件之间唯一可机器验证的连线**。执行器把它盖在这一步产出的
**每一条** delta 记录上,所以 `delta.json` 里任何一处改动都能说出"这是指令的哪一句
要求的"。奖励阶段两个方向都要问,少哪个方向都不行:

- **每一条被判分的改动,都必须对应到指令真的要求过的降级** ——
  否则评分器在判一件没人被要求做的事
- **每一条降级,都必须至少产出一条可判分的改动** ——
  否则指令要求了一件不给分的活儿

`check_recipe` 就照这两条卡:`deg` 缺了、或者填了提案里没有的 id,直接判 `failed`;
提案里有哪条降级**没有任何一步认领**,同样判 `failed`,报错里点名是哪个 id。

**`_why` 里写一句"这是 d1"顶不了这个字段。**头十个 deck 的配方碰巧全都把 id 写在
`_why` 开头,那是习惯不是判据:下一个人写"把 d2/d3 的泄漏一起封掉"就匹配到两条,
写"把那一行补回来"就一条都匹配不到,两种都是靠猜。

**一步只填一个 `deg`。**一步同时服务两条降级(典型是一个 `strip_animation`
把两处删除的动画泄漏一起封了),就拆成两步,各自认领一条 —— 不要在一个字段里塞两个 id。

---

## 算子表

### 形状级(写在 `slides` 里)

| op | 参数 | 用途 |
|---|---|---|
| `delete` | `paths` | 删除形状;图片/图表的 part 和关系一并清掉 |
| `scatter` | `paths`, `amplitude_in`(默认0.9) | 随机推离原位 |
| `move` | `paths`, `dx_in`, `dy_in` | 定向平移 |
| `resize` | `paths`, `factor`, `factor_y`, `keep_center` | 缩放 |
| `swap` | `pairs`: `[["3","4"]]` | 交换两个形状的位置 |
| `rotate` | `paths`, `deg` | 旋转 |
| `zorder` | `paths`, `to`: `"back"`/`"front"` | 改叠放次序 |
| `ungroup` | `paths` | 打散组合,子元素留在绝对位置 |
| `clear_text` | `paths`, `keep_first_paragraph` | 清空文字,形状保留 |
| `set_text` | `paths`, `text` | 替换文字 |
| `set_font` | `paths`, `font`, `size_pt`, `bold`, `italic`, `underline`, `color` | **整个形状**的所有 run |
| `text_runs` | `paths`, `paragraphs`:[序号] 或 `match`:[子串], `delete`:true 或同上样式参数 | **部分**段落 |
| `strip_effects` | `paths`, `flatten_gradient` | 去阴影/发光/立体/渐变 |
| `recolor` | `paths`, `to`: `"#RRGGBB"` | 改填充色 |
| `outline` | `paths`, `mode`:`"remove"`/`"set"`, `color`, `width_pt` | 改/去描边 |
| `crop` | `paths`, `mode`:`"reset"` 或 `l`/`t`/`r`/`b`(百分比) | 图片裁剪 |
| `table_drop_rows` / `table_drop_cols` | `paths`, `rows`/`cols`:[序号] | **删**行列 |
| `clear_table_cells` | `paths`, `rows`, `cols` | **清空**单元格(不删) |
| `detach_connector` | `paths`, `nudge_in` | 连接线脱靶 |
| `anim_drop_steps` | `steps`: [1-based 步号] | 删掉部分构建步 |
| `strip_animation` / `strip_transition` | — | 整页去动画/转场 |
| `blank_slide` | `keep_paths` | 清空整页,保留指定形状 |

### 复合对象内部(写在顶层)

| 键 | 参数 | 用途 |
|---|---|---|
| `smartart` | `slide`, `drop_text`:[子串] 或 `drop_id`, `graphic` | **删 SmartArt 里的某几个节点**,其余保持原位 |
| `chart` | `slide`, `drop_name`/`drop_index`, `strip`:[`legend`/`title`/`data_labels`/`gridlines`/`axis_titles`] | **删 series 或图表配件** |

### 整个 deck

| 键 | 参数 |
|---|---|
| `reorder_slides` | `{"swap": [[3,4],[7,9]]}` |
| `delete_slides` | `[12, 15]` |
| `clear_notes` | `[{"slides":[7,8]}]` |
| `layout` | `[{"layout":"版式名","delete_paths":["1"]}]` —— 会影响所有用该版式的页 |

**提案要的是"局部编辑"时,先找上面这两张表有没有对应的键,再考虑整块删除。**
`smartart` 和 `chart` 就是为此存在的:整块删会把幸存元素这个锚点也一起毁掉,
把"照着补齐"变成"从零重建",难度和题意都变了。

---

## 命令

```bash
# 看某几页的形状表(path / 位置 / 尺寸 / z序 / 字体 / 图片哈希 / 文字)
python -m pptxgym.tools shapes <deck-dir> 7 12 19

# 看 SmartArt 或图表内部有哪些节点/series
python -m pptxgym.tools smartart <deck-dir> --slide 19
python -m pptxgym.tools chart    <deck-dir> --slide 5

# 试跑配方 + 完整性闸门(写进 trial/,不提交、不改 pipeline 状态)
python -m pptxgym.tools trial <deck-dir>

# 渲染受影响的页做对照(原稿 vs 坏文件)
python -m pptxgym.tools pair <deck-dir> 7 12
```

`trial` 必须输出 `gate=ok`。报 ANSWER LEAK 或 DEAD RELS 说明删得不干净。

**不要跑 `pptxgym degrade`。**那是编排层提交产物的命令,你的活儿是把配方写对;
提交与否由流水线自己判。你跑它也会被 deck 锁拒掉。

---

## 位置类降级的幅度下限

`digest.json` 的 `deck_summary.renderer_drift` 记着**这个 deck 光是被软件打开再
保存就会漂多少**。它现在**分渲染器记**,并用 `governs` 标明哪一个说了算:

> **任务在 WPS 里被解、被评分,所以只有 `renderer_drift.wps` 能约束位置类降级。
> `renderer_drift.libreoffice` 是语料脆弱度信号,不是容差,永远不要拿它定幅度。**

**软件自己造成的位移,和 agent 造成的位移长得一模一样** —— 但那必须是**评分那个
软件**造成的位移。10 个 deck 实测:**WPS 打开再保存,一个形状都不动**;
LibreOffice 动了 7.6%–61.5%,几乎全是文本框和表格按字体度量重排。按 LibreOffice
的 p90 定幅度会得到 0.13–0.85 英寸的下限,**那是代理渲染器的噪声,不是这个 deck
的性质**。

**① `scatter` / `move` 的幅度下限:**

```
amplitude_in ≥ max(0.8, 4 × renderer_drift.wps.drift_in.p90_in)
```

- **`wps.changed_frac` 为 0**(目前 10 个 deck 全是这样):`drift_in` 是空的,
  **别去读那个不存在的 `p90_in`** —— 下限退化成常数 0.8in。这些 deck 上位置类
  降级没有渲染器噪声,该出就出。
- **`wps.changed_frac` 不为 0**:这才是真约束。p90 是 0.57in 的 deck,幅度得
  ≥2.3in。**那一页放不下 2.3in 的位移,就说明这一页不该出位置题** —— 换个目标,
  或者换成删除/重建类的降级。
- **`governs` 是 null**(这个 deck 没测过 WPS):**不要拿 LibreOffice 的数字顶替。**
  把位移做得大而明显(≥1.5in 是稳妥的起点),并在 `_why` 里写一句"WPS 漂移未测"。
  补测:`python3 -m pptxgym.wps_roundtrip <deck>/source.pptx`。

**② 优先挪不会漂的东西。**`renderer_drift.wps.kinds_that_move` 列出这个 deck
**在 WPS 下**会漂的类型;为空就是谁都不漂。`libreoffice.kinds_that_move` 里有什么,
**不是**回避某个形状类型的理由。

位置类降级一般优先挑**图片、卡片、图示**当目标。跟着图走的说明文字可以一起挪,
但它不该是被判分的那个对象 —— 在 `_why` 里写清楚哪些是主目标。

---

## 三条硬规矩

**1. 路径是位置编号,一次索引到底。**
`delete` 会让后面的形状整体错位。执行器在**一页的所有步骤开始前索引一次**,
所以你写的 path 全部按**原始 digest 的编号**,不要自己去推"删完之后变成几号"。

**2. 做不到就如实写,不要假装做到。**
`_why` 里写清楚近似了什么、为什么。例如整块删了一个本该局部编辑的 SmartArt,
就要写明"锚点没了,难度从补齐变成重建,指令需要相应改写"。
**漏报比做不到严重得多**——做不到是工具缺口,漏报是数据里混进了错误标注。

**3. 不要碰 `hard_target`。**
digest 里标了 `hard_target` 的形状(OLE 对象、自定义贝塞尔几何)GUI 里做不出来,
提案通常明说了它们是上下文不是目标。删了就是无解任务。

---

## 常见对应关系

| 提案怎么写 | 通常怎么做 |
|---|---|
| "某几样东西不见了" | `delete` |
| "被打散/错位了" | `scatter`(+ 个别 `resize` 制造尺寸不一致) |
| "整块内容被挖空" | `delete` 一组,或 `blank_slide` 带 `keep_paths` |
| "某一列/某一档没了" | `smartart` 或 `chart`,**不是** `delete` |
| "强调被抹掉了" | `text_runs` 带 `bold:false` **和** `underline:false` |
| "样式和别页不一致了" | `set_font` / `recolor` / `strip_effects` |
| "构建步骤丢了几步" | `anim_drop_steps` |
| "顺序乱了" | `reorder_slides` |

去粗体时**一定要一起去下划线**:只去粗体会留下 `u="sng"`,
正好把原本被强调的位置标出来,等于把答案递给 agent。

---

## 交付前自查

1. 提案里每条降级都有至少一步认领(`deg`)了吗?每一步的 `deg` 都是提案里真有的 id 吗?
   两个方向都是 `check_recipe` 的硬判据,不过是先自己查一遍还是被打回来的区别。
2. `trial` 输出 `gate=ok` 了吗?
3. **渲染图看过了吗?**坏掉的样子和 `what_breaks` 描述的一致吗?
4. 该留的锚点还在吗?(幸存的同类元素、参照页、周边上下文)
5. 有没有误伤 `hard_target`?
