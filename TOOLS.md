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
pptxgym status                    # 阶段表(>24 个 deck 转汇总;--all 看全表)
```

`--workers` = 同时几个 **agent** 阶段(吃 API);`--cpu-workers` = 同时几个
**渲染** 阶段(吃 soffice,默认 cores/4)。两个池子分开,槽位按阶段申领、用完即还。
`status` 末尾会报:现在谁在跑、谁在等修复、谁被搁置、work/ 占了多少磁盘。

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

## 字体:渲染图什么时候不能当证据

上面第 1 条坑说"先看图"。**这一节说的是图什么时候在骗你。**

渲染器碰到一个本机没有任何字体覆盖的码点,画的是 `.notdef` —— 一个空心方框。
它不报错、不写日志,后面整条链没有一个环节读得懂字形:
**提案对着方框写、参照图是方框、solvability 探针对着方框判"这题能不能做"。**
全部闸门都会过,出来的是一批理直气壮的空题。语料 `Forceless/Zenodo10K` 是一万份
国际会议投稿,中日韩、阿拉伯、西里尔、泰文都在里面,所以这不是小概率事件。

```bash
python -m pptxgym.fonts work/deck0001/source.pptx      # 一行结论
python -m pptxgym.fonts work/deck00*/source.pptx --json
```

| verdict | 含义 |
|---|---|
| `ok` | 每个字符都有字形 |
| `incidental` | 缺的不到 1%,且没有单页塌掉(公式里两个希腊字母就是这一档) |
| `degraded` / `unrenderable` | 有整页是方框 —— 看 `unusable_slides`,**那些页的渲染图不是证据** |
| `unknown` | 没有 fc-list 也没有 fontTools,**问不出来就不算过**(和 `undetermined` 同理) |

**`fc-list :lang=zh` 有输出 ≠ 中文能渲染。** 覆盖是按码点算的,不是按语言。
写这段时这台机器上 `:lang=zh` 和 `:lang=ja` 各匹配一个字体,而且是真的能画
(DroidSansFallbackFull 带 Han 和假名);同一个字体的谚文只有字母不带音节,
所以韩文全是方框,而 `:lang=ko` 干脆一条都不匹配 —— 语言探针三个答案里对两个,
纯属运气。本机实测缺:**谚文音节、泰文、天城文、BMP 外的 emoji**;
另外 deck0001 里有两个 U+F0E0,Wingdings 的信封图标,Linux 上不可能有
(WPS 启动时那句 `missing fonts Symbol` 说的就是这个)。

### 镜像必须装的字体

跑在 HF Jobs 的 Docker 镜像里,下面这些是 **Debian/Ubuntu 包名**,一个都不能省:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
      fontconfig \
      fonts-noto-core fonts-noto-extra fonts-noto-ui-core \
      fonts-noto-cjk fonts-noto-cjk-extra \
      fonts-noto-color-emoji fonts-noto-mono \
      fonts-dejavu-core fonts-dejavu-extra \
      fonts-liberation fonts-liberation-sans-narrow \
      fonts-crosextra-carlito fonts-crosextra-caladea \
 && fc-cache -f && rm -rf /var/lib/apt/lists/*
```

- `fonts-noto-core` / `-extra` 覆盖希腊、西里尔、阿拉伯、希伯来、泰文、天城文
  和其余大部分文种;**CJK 不在里面**,必须另外装 `fonts-noto-cjk`
  (`-extra` 是全字重,SmartArt 和标题常用非常规字重,建议一起装)。
- `fonts-liberation`(Arial / Times / Courier)、`fonts-liberation-sans-narrow`
  (Arial Narrow)、`carlito`(Calibri)、`caladea`(Cambria)装的不是"更多文种",
  是**度量兼容**的替身。deck 里写的字体名基本都是这几个,替身度量对不上,
  文本就会重排 —— 这正是 REWARD.md 2.4 量到的 0.6 英寸(见那一节)。
- `fc-cache -f` 不能漏,装完不刷新缓存 fontconfig 看不见。
- Wingdings / Symbol 那类 PUA 图标没有自由替代品,装什么都补不上,
  这类 deck 会一直被标出来 —— 这是正确行为,不要去关掉它。

**两端都要装,而且要装同一套:**
LibreOffice 在**渲染时**用(`inspect` 出的图就是提案的全部证据),
WPS 在**测量时**用(`wps_roundtrip` 的 0.0% 是按当前字体算出来的,
换一套字体就换一个数)。镜像和评测 VM 字体集不一致,量出来的数就搬不过去。

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
