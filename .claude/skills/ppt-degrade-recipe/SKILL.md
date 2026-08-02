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
   "7":  [{"op": "delete", "paths": ["3", "13"], "_why": "d1 — 三个里程碑的照片"}],
   "12": [{"op": "scatter", "paths": ["4", "6"], "amplitude_in": 1.2, "_why": "d2 — ..."}]
 },
 "smartart": [{"slide": 19, "drop_text": ["Ingest"], "_why": "d4 — 只删第三列"}],
 "chart":    [{"slide": 5, "drop_name": ["Data Size"], "_why": "d1 — 少一条 series"}],
 "reorder_slides": {"swap": [[3, 4]]},
 "clear_notes": [{"slides": [7, 8]}],
 "layout": [{"layout": "Title and Content", "delete_paths": ["1"]}]}
```

- `slides` 的键是 **1-based 页号**,和渲染图、和 `shapes` 命令一致
- **每一步都要写 `_why`**,指明对应提案里的哪条降级(`d1`/`d2`…)
- `seed` 让 `scatter` 可复现

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

1. 提案里每条降级都实现了吗?没实现的写进 `_why` 了吗?
2. `trial` 输出 `gate=ok` 了吗?
3. **渲染图看过了吗?**坏掉的样子和 `what_breaks` 描述的一致吗?
4. 该留的锚点还在吗?(幸存的同类元素、参照页、周边上下文)
5. 有没有误伤 `hard_target`?
