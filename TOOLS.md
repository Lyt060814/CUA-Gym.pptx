# TOOLS — 给 agent 看的惯用法

不是 API 文档。每条命令 `--help` 永远和代码同步,这里只写**调用顺序、
失败了意味着什么、哪些坑不要踩**。

---

## 一个 deck 目录长什么样

```
work/deck0001/
  meta.json          来源、页数、页面尺寸
  source.pptx        原始 deck —— 也是 ground truth,任何阶段都不许写它
  digest.json        结构摘要(给人看,带缩进)
  digest.min.json    同样内容,紧凑,给 agent 读(约一半 token)
  renders/p-NN.png   每页一张
  proposal.json      这个 deck 该出什么任务          ← agent
  recipe.json        具体怎么弄坏                    ← agent
  input.pptx         弄坏之后的文件
  delta.json         每一处改动,连同改动前的值
  state.json         每个阶段的状态
```

**阶段之间只通过文件交接。**没有任何东西存在对话里,所以任何一步都能单独重跑,
也能人工接管其中一步再交回给 pipeline。

---

## 阶段

```
ingested → inspected → proposed → recipe → degraded
            确定性       agent      agent    确定性
```

```bash
pptxgym ingest corpus/            # 登记源 deck
pptxgym inspect                   # digest + 渲染图
pptxgym propose --deck deck0001   # 起 headless agent
pptxgym recipe  --deck deck0001   # 起 headless agent
pptxgym degrade --deck deck0001   # 执行配方 + 完整性闸门
pptxgym run --workers 6           # 全部,跳过已完成的
pptxgym status                    # 阶段表
```

`degraded` 之后的阶段(奖励函数、验证、打包)**故意还没有**。代码在别处存在,
但没有经过批量验证——没跑过的阶段不该出现在一条要给别人用的流水线里。

---

## 检查工具

写配方时会用到:

```bash
python -m pptxgym.tools shapes   work/deck0001 7 12 19   # 形状表,path 从这里来
python -m pptxgym.tools smartart work/deck0001 --slide 19
python -m pptxgym.tools chart    work/deck0001 --slide 5
python -m pptxgym.tools pair     work/deck0001 7 12      # 原稿 vs 坏文件,逐页
```

`shapes` 每行是:`path / kind / 归一化位置 / 英寸尺寸 / z序 / 字体 / 图片哈希 / 标记 / 文字`。
行首 `·` 是 decorative(小图标、连接线),`>>` 是复合对象,`~~` 是连接线拓扑,
`##` 是重复组。**`path` 就是配方里的地址。**

---

## 三个必须知道的坑

**1. 光看 JSON 选不对 path。**
形状的 `path 3` 是什么,只有对着渲染图才认得出。选错不报错,只是删了别的东西。
**先看图。**

**2. 路径是位置编号,删除会让后面全部错位。**
执行器在**一页的所有步骤开始前索引一次**,所以配方里所有 path 都按
**原始 digest 的编号**写,不要自己推算删除之后的编号。

**3. 删形状必须连关系一起删,否则答案还在包里。**
执行器已经会做,但如果你手写工具要知道:只把元素从 spTree 摘掉,
图片的位图和 SmartArt 的 `data*.xml`(含每个节点的文字)还活着,
`unzip` 就能读到。`degrade` 的闸门会抓这个,报 `ANSWER LEAK` 或 `DEAD RELS`。

---

## 闸门说了什么

`pptxgym degrade` 输出 `gate=ok` 才算过。会拒的情况:

| 报告 | 含义 |
|---|---|
| `r:id ... -> missing part` | 引用悬空,文件在不同软件里表现会不一样 |
| `no content type for part` | 同上 |
| `shape id N used 2x` | 同一页出现重复 shape id |
| `ANSWER LEAK` | 删掉的东西的数据还在包里 |
| `DEAD RELS` | 某页还指着自己不画的 part |

**在原始 deck 上跑一次闸门再下结论。**有些"问题"是源文件自带的——
OLE 的 `mc:Fallback` 备用图 id 恒为 0、SmartArt 的 `diagramDrawing` 不通过
`r:id` 引用,这两类已经在闸门里排除掉了,但遇到新的误报要先验证 gt。

---

## hard_target

digest 里带 `hard_target` 的形状 **GUI 里做不出来**:

- `ole` —— 内嵌的 Excel / Origin / Prism / 公式对象
- `custom_geometry` —— 手绘贝塞尔路径(`redrawable` 为 false 时尤其)

它们是**上下文,不是目标**。删了任务就无解。提案里通常已经明说,配方阶段不要越界。

---

## 复合对象要局部编辑,不要整块删

| 对象 | 局部编辑的入口 |
|---|---|
| SmartArt | 配方顶层 `smartart`:`{"slide":19,"drop_text":["Ingest"]}` |
| 图表 | 配方顶层 `chart`:`{"slide":5,"drop_name":["Data Size"]}` 或 `strip` |
| 表格 | `table_drop_rows` / `table_drop_cols`(删)vs `clear_table_cells`(清空) |
| 文字 | `text_runs`(部分段落)vs `set_font`(整个形状) |
| 动画 | `anim_drop_steps`(部分构建步)vs `strip_animation`(整页) |

整块删会把**幸存元素这个锚点**一起毁掉,把"照着补齐"变成"从零重建"——
难度和题意都变了。提案要局部,就用局部的入口。

---

## 汇报纪律

做不到的事情**如实写进 `_why`**,并说明它的代价。

> "整块删了本该局部编辑的 SmartArt —— 幸存列这个样式锚点没了,
> 难度从补齐变成重建,指令需要相应改写"

**漏报比做不到严重得多。**做不到是已知的工具缺口,可以补;
漏报是数据里混进了标注错误的样本,后面没人查得出来。
