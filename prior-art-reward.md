# 奖励阶段的现成件盘点 —— 能搬什么,搬进来会断在哪

REWARD.md 第六节说「现成的代码在别处存在,搬之前要对齐」。这份文件是那次对齐。

三个文件在 `/home/yitongli/XLANG/pptx-tasks/scaling/pipeline/`:
`ops.py`(17 个算子 + 比对器)、`evaluator.py`(注册表驱动打分)、
`verify.py`(4 探针 + 8 例对抗电池)。本仓库的 `pptxgym/degrade_exec.py` 有 **25 个**。

**一句话结论:两套注册表的交集是空集,25 个算子里今天能被打分的是 0 个。**
`audit_delta` 对 `work/deck0001/delta.json` 实跑,23 条记录 23 条被拒
(`op='delete' 没有已注册的比对器`)。这不是名字对不上那么简单——见第三节。

---

## 一、算子映射表

`degrade_exec.REGISTRY` 的 25 个,逐个对 `ops.REGISTRY` 的 17 个。
判据是**语义**:同名不算数,damage 的是不是同一样东西才算数。

| # | `degrade_exec` 算子 | `ops.py` 对应 | 关系 | 说明 |
|---|---|---|---|---|
| 1 | `delete` | **无** | ✗ 完全没有 | 只有 `chart_delete` 删东西,且只删原生图表。删图片 / 文本框 / 表格 / 组 / SmartArt 一律无比对器。**这是十个 deck 里 153/271 = 56% 的降级** |
| 2 | `delete_slide` | **无(且冲突)** | ✗ 反向不兼容 | `evaluator` 有硬闸 `slide_count`:页数不等直接返回 0 分。删页任务在现有评分器下恒为 0 |
| 3 | `scatter` | `move_shape` / `_geom_compare` | ≈ 语义等价 | 都是「随机推离原位,还原到原坐标」。比对器逻辑可用,记录形状要转换 |
| 4 | `move` | `move_shape` / `_geom_compare` | ≈ 语义等价 | 同上,只是位移是确定的 |
| 5 | `resize` | `resize_shape` / `_geom_compare` | ≈ 语义等价 | 同一个 `_geom_compare`;`ops.py` 的三个几何算子共用一个比对器 |
| 6 | `rotate` | `rotate_shape` / `_geom_compare` | ≈ 语义等价 | `was_deg` 是度,`expected.rot` 也是度,单位一致 |
| 7 | `swap` | 无同名,`_geom_compare` 可用 | ~ 部分重叠 | 交换两个形状的位置。`ops.py` 没有「配对」概念,但还原判据就是「各回各的中心」,`_geom_compare` 的位置项正好是它。**但 `_geom_compare` 里位置只占 0.6,rot/size 各 0.2 白送**——swap 不动 rot/size,所以什么都不做拿 0.4 |
| 8 | `clear_text` | `clear_text` / `_clear_text_compare` | ≈ 同名且语义等价 | 都是清空 `a:t`。`ops.py` 版第一个 run 留 `…`,`degrade_exec` 版全清(或留首段)。比对器是文本相似度,两边都适用 |
| 9 | `set_text` | 无同名,`_clear_text_compare` 可用 | ~ 部分重叠 | damage 不同(改错 vs 抹掉),但还原目标相同:原文回来。喂 `was_text` 即可 |
| 10 | `set_font` | **无** | ✗ 完全没有 | `ops.py` 没有任何 run 级文本样式比对器。`color_mutate` 读的是形状填充,不是 run 填充。**这是 43/271 = 16% 的降级** |
| 11 | `strip_effects` | `strip_outershdw` / `strip_glow` / `strip_reflection` / `strip_softedge` / `flatten_3d` / `gradient_to_solid` | ~ 1:6 扇入 | 一个算子一次干掉六类东西。六个比对器逐个都在,但一条 delta 记录要拆成六个打分单元。**`effectRef`(主题效果索引)和 `effectDag` 两边都没有比对器**——而 `strip_effects` 会改 `effectRef` |
| 12 | `recolor` | `color_mutate` / `_color_compare` | ~ 部分重叠 | 纯色→纯色时等价(ΔE 判据)。但 `recolor` 会把 `gradFill`/`blipFill`/`pattFill` 一起干掉;原来是渐变的话,`_color_compare` 读 `fill.color` 得到 `None` → `delta_e(None,·)=100` → **完美还原也是 0 分**。得按原填充类型分派到 `_grad_compare`,而记录里原类型只以 XML 字符串存在 |
| 13 | `outline` | `line_remove`(mode=remove)/ `line_reset` + `line_color_mutate`(mode=set) | ~ 部分重叠 | 覆盖得不错。但 `ops.py` 要的是解析好的 line 字典 `{w,dash,color,head,tail}`,`degrade_exec` 存的是 `was_ln_xml` 字符串(**截断在 600 字符**)。`lnRef` 主题索引没有比对器 |
| 14 | `zorder` | **无** | ✗ 完全没有 | census 有 `z` / `top_z` 字段,事实在,比对器没有 |
| 15 | `ungroup` | **无(且不可测)** | ✗ 结构性缺口 | `evaluate.leaf_shapes` 明确过滤掉 `kind == "group"`,`match_slides` 只匹配叶子。**重新编组和没编组在现有评分器眼里字节级相同**。这跟 `operator-audit.md` 发现的「渲染差 0.000」是同一个问题的另一面 |
| 16 | `clear_table_cells` | **无** | ✗ 完全没有 | census `read_table` 已经存了 `rows[].cells`(每格文本截断 80 字符,**最多 24 行**)和 `merges`。事实齐了,比对器是零 |
| 17 | `table_drop_rows` | **无** | ✗ 完全没有 | 同上 |
| 18 | `table_drop_cols` | **无** | ✗ 完全没有 | 同上。注意 `n_cols` 来自 `tblGrid`,正好是 `operator-audit.md` 里那个 merge 算术错误会体现的地方 |
| 19 | `text_runs` | **无** | ✗ 完全没有 | 段落级重排样式 / 删段。`text_style.shape_text` 已经给出逐 run 的**继承解析后**格式(封顶 12 段 × 8 run),比对器没有。删段能被 `_clear_text_compare` 的相似度间接抓到,重排样式完全抓不到 |
| 20 | `detach_connector` | **无** | ✗ 完全没有 | census 有 `st_cxn` / `end_cxn`,事实在,比对器没有 |
| 21 | `crop` | `reset_crop` / `_crop_compare` | ≈ 同一件事,**但比对器是坏的** | 见第二节。而且 `degrade_exec` 还支持 `mode=set`(改成错的裁剪),`ops.py` 的 apply 只会清空 |
| 22 | `anim_drop_steps` | **无** | ✗ 完全没有 | census 完全不记动画。`anim_steps.py` 两边都有,但没有任何比对器读它 |
| 23 | `strip_animation` | **无** | ✗ 完全没有 | |
| 24 | `strip_transition` | **无** | ✗ 完全没有 | |
| 25 | `blank_slide` | **无** | ✗ 完全没有 | 就是批量 `delete` |
| + | `smartart_drop_nodes`(`run()` 直接写进 delta) | **无** | ✗ 完全没有 | |
| + | `chart_edit`(同上) | `_chart_compare` 的**函数体**可用 | ~ 部分重叠 | `_chart_compare` 是 `ops.py` 里写得最好的一个(要求必须是原生图表、数值 2% 相对容差)。但它要 `expected.{plot,title,series}`,而 `charts.rewrite` 的报告里没有完整 spec;而且 `evaluator` 见到 `family=="chart"` 就走 `_nearest_chart` 重建槽位匹配,`chart_edit` 的框还在,该走 path 匹配 |

`run()` 还会写四个**顶层**记录:`deleted_slides`、`reorder_slides`、`cleared_notes`、
`layout_edits`。`evaluator.evaluate` 只遍历 `delta["slides"]`,这四类**完全看不见**——
不是分低,是根本不进入打分。

### 反向:`ops.py` 里没有对应降级算子的比对器

只有一个真孤儿:

- **`round_to_rect`**(圆角变方角)—— `degrade_exec` 没有任何算子改 `prstGeom`。
- `chart_delete` 算半个孤儿:`delete` 删掉图表帧时语义对得上,但**没有任何
  `degrade_exec` 算子记录图表的 series 规格**,所以 `expected` 永远填不出来,
  比对器有等于没有。

其余 15 个都能在 25 个里找到语义来源(六个 strip 类挤在一个 `strip_effects` 里)。

### 按实际用量加权

十个 deck 的 `work/*/delta.json`,271 条记录:

| 算子 | 条数 | 有比对器? |
|---|---|---|
| `delete` | 153 | ✗ |
| `set_font` | 43 | ✗ |
| `outline` | 28 | ~(需适配) |
| `move` | 24 | ≈ |
| `scatter` | 8 | ≈ |
| `resize` | 7 | ≈ |
| `strip_animation` | 3 | ✗ |
| `smartart_drop_nodes` | 3 | ✗ |
| `clear_table_cells` | 2 | ✗ |

**已经产出的十个任务里,比对器逻辑覆盖 67/271 = 24.7% 的降级;
占 56% 的删除类一个都不覆盖。**

---

## 二、17 个比对器各自到底在做什么

读什么、什么容差、返回什么、给不给部分分。数字是 `ops.DEFAULT_TOL` 的实值。

```
DEFAULT_TOL = {dir_deg: 15.0, blur_pct: 0.35, delta_e: 25.0, width_pct: 0.30,
               angle_deg: 20.0, pos_emu: 109728 (=0.12in), rot_deg: 7.0,
               size_ratio: 0.12}
```

所有比对器签名 `compare(entry, rec, tol, r_gt, r_res) -> (float, str)`,
`rec` 是**结果文件**的 census 记录,`r_gt` / `r_res` 是两边的 `ThemeResolver`。

| 比对器 | 读 | 容差 | 部分分 |
|---|---|---|---|
| `strip_outershdw` | `rec.style.effects.outerShdw` | 方向 15°、模糊 `pct_close(±35%, 下限 25400 EMU)`、颜色 ΔE≤25 | **有**:存在 0.3 + 方向 0.3 + 模糊 0.15 + 颜色 0.25 |
| `strip_glow` / `strip_reflection` / `strip_softedge` | 同上,各自的 key | 半径 ±35% | **有**:存在 0.7 + 半径 0.3 |
| `flatten_3d` | `rec.style.sp3d` / `scene3d` | 无(布尔) | **有**:恢复的节点数 / 期望节点数 |
| `gradient_to_solid` | `rec.style.fill.stops` | 端点色 ΔE≤25(**正反两向取小**,允许色标顺序颠倒)、角度 20° | **有**:是渐变 0.3 + 颜色 0.5 + 角度 0.2。`expected.angle` 为 None 时角度分白送 |
| `line_reset` | `rec.style.line` | 宽度 ±30%(下限 6350 EMU=0.5pt)、颜色 ΔE≤25 | **有**:虚线 0.3 + 宽度 0.2 + 颜色 0.3 + 箭头 0.2。**原本没箭头就白送 0.2;原色解析不出来(`e_rgb is None`)也白送 0.3** |
| `line_remove` | 同上 | 同上 | **有**:宽度 0.4 + 颜色 0.6。比 `line_reset` 严:发丝线也算没还原,没有白送项 |
| `line_color_mutate` | `rec.style.line.color` | ΔE≤25 → 1.0;ΔE≤40 → **0.5**;否则 0 | **有**:三档 |
| `round_to_rect` | `rec.style.prstGeom` | 无。任一 `ROUND_GEOMS`(8 种圆角/切角)都算对 | 无:1.0 / 0.0 |
| `color_mutate` | `rec.style.fill.color`(经 resolver) | ΔE≤25 → 1.0;≤40 → **0.5**;否则 0 | **有**:三档 |
| `move_shape` / `rotate_shape` / `resize_shape` | `rec.cx/cy/w/h/rot` | 位置 **0.12in**;**半档到 0.30in**(2.5×);旋转 7°;尺寸 ±12% | **有**:命中 0.6+0.2(rot)+0.2(size);半档 0.3+0.1+0.1;否则 0 |
| `clear_text` | `rec.text`,NFC 归一 + 折叠空白 + 小写 | `SequenceMatcher.ratio()` ≥0.95 → 1.0;≥0.75 → **0.5**;否则 0 | **有**:三档 |
| `reset_crop` | `rec["srcRect"]` ← **这个键不存在** | 每条边 ±3000(千分之三 %) | **有**:命中边数 / 总边数 |
| `chart_delete` | `rec.chart`(census 从 `numCache` 读) | 数值相对 2%(`_num_close(rel=0.02)`) | **有**:类型 0.2 + 系列数 0.15 + 系列名 0.15×命中率 + 类别 0.10×命中率 + 数值 0.40×命中率。**非原生图表(贴图)恒为 0** |

### `reset_crop` 是坏的,而且从没跑过

census 把裁剪放在 `rec["crop"]["srcRect"]`,而且**把值除以了 1000**
(`{k: int(v)/1000.0 ...}`,即存 12.5 表示 12.5%);
delta 的 `expected.srcRect` 存的是 `dict(src.attrib)`,原始字符串 `"12500"`。
比对器读的是 `rec.get("srcRect")` —— 顶层,不存在。实测:

```
完美还原(census 形状的 rec) -> (0.0, '0/2 裁剪边')
如果 rec 直接带原始 srcRect  -> (1.0, '2/2 裁剪边')
```

**两个 bug 叠在一起:键找错了一层,单位差 1000 倍。**
它没被发现是因为 11 份 ops 风格的 delta 里**一次都没出现过 `reset_crop`**。

### 从没在真任务上跑过的比对器

11 份 ops 风格 delta(4 个 webui 任务 + 5 个 seed5 候选 + 2 份副本)里出现过的算子:
`line_reset`(8)、`line_remove`(6)、`move_shape`(6)、`chart_delete`(4)、
`color_mutate`(4)、`clear_text`(3)、`strip_shadow`(3)、`gradient_to_solid`(3)、
`line_color_mutate`(2)、`round_to_rect`(2)、`rotate_shape`(1)、`resize_shape`(1)。

**从没跑过:`strip_glow`、`strip_reflection`、`strip_softedge`、`flatten_3d`、
`reset_crop`(17 个里的 5 个)。**

还有一条自证:那 3 条记录写的是 `strip_shadow`,而当前注册表里叫
`strip_outershdw`。**同一个仓库的注册表自己就漂过一次,老 delta 今天过不了
`audit_delta`。** 这正是要建立的这条纪律要防的事。

---

## 三、delta 记录契约:两边在哪里对不上(**最要命的一节**)

### 两种记录形状

```jsonc
// build_task.py 写的(ops.py / evaluator.py / audit_delta 期待的)
{"gt": ..., "seed": 0, "tolerance": {...8 项...}, "slides": {"2": [
  {"op": "line_remove", "op_zh": "边框去除", "family": "style",
   "path": "10", "key": "txt:bebafa0258fb5f92#0", "directive": "…",
   "bbox": {"cx": 7128830.5, "cy": 881012.0, "w": 3720733, "h": 1049712},
   "expected": {...解析好的样式字典...}, "removed_xml": "<a:ln …完整…>",
   "floor": 0.0}]}}

// degrade_exec.py 写的
{"gt": ..., "recipe": ..., "input": ..., "dropped_rels": {...}, "slides": {"2": [
  {"path": "10", "op": "outline", "shape_id": "7", "name": "…", "text": "…",
   "kind": "textbox", "mode": "remove",
   "was_ln_xml": "<a:ln …前 600 字符…", "was_style_line": "1",
   "box": [150763, 1637752, 11890473, 3285640]}]}}
```

### 逐条冲突

| # | 字段 | `ops.py` 侧 | `degrade_exec` 侧 | 失败方式 | 严重度 |
|---|---|---|---|---|---|
| 1 | **`expected`** | 每个比对器第一行就读 `entry["expected"]` | **一个算子都不写**。原值散在 12 个不同的键里:`was` / `now` / `was_text` / `now_text` / `was_sizes` / `was_props` / `was_ln_xml` / `was_fill_xml` / `was_xml` / `was_srcRect` / `was_deg` / `was_index` / `was_attachments` / `cleared` / `removed` / `touched` | **静默满分** —— 见下 | **最高** |
| 2 | `bbox` | `{cx, cy, w, h}`,**中心 + 尺寸**,float | `box`:`[x, y, cx, cy]`,**左上角 + 尺寸**,list;deck 级条目(`path="-"`)**根本没有**;`_box()` 可能返回 `None` | `KeyError: 'bbox'`,响的 | 高但可见 |
| 3 | `key` | `audit_delta` 硬性要求;`evaluator` 报告里用 | 不写。有 `shape_id` / `name`,但那不是稳定实例键 | 拒绝发布 | 中 |
| 4 | `tolerance`(顶层) | `evaluator` 写 `delta["tolerance"]`,**不是 `.get`** | 不写 | `KeyError`,响的 | 中 |
| 5 | **`floor`** | `evaluator`:`m.get("floor", 0.0)`,缺省 0 → 不做归一化 | 不写。`compute_floors` 住在 `build_task.py` 里,不在评分链上 | **静默取消 REWARD.md 第四节两道防线之一** | **高** |
| 6 | `removed_xml` | `restore()` 直接 `etree.fromstring(entry["removed_xml"])` | 有,但**截断**:`delete` 4000 字符、`blank_slide` 1500、`outline` 600、`recolor` 每个 fill 800、`strip_effects` 每个 2000 | `XMLSyntaxError`;`verify.scripted_restore` 里被 `except: pass` 吃掉 → 探针分数悄悄变低 | 高 |
| 7 | 文本原值 | `expected.text` 全文 | `was_text[:600]` | 1200 字的形状完美还原,`SequenceMatcher` 比 600 截断 vs 全文 ≈0.67 → **判 0 分** | 高 |
| 8 | `family` | `evaluator` 靠它做几何护栏和图表分支 | 不写 | `op is None` → 0 分 | 中 |
| 9 | `path` 语义 | `build_task.walk_elements`:直接遍历子节点、只认 `SHAPE_TAGS`,**不进 `mc:AlternateContent`** | `index_shapes` 用 `census.shape_children`,**会进 `mc:AlternateContent` 的 Choice/Fallback 分支** | 带 AlternateContent 的 deck 上,同一个 path 指向**不同形状**,静默 | 高(旧管线自己也有) |
| 10 | 页号基准 | `slides` 的键是 0-based | `slides` 的键也是 0-based ✓,但 `smartart_drop_nodes` / `chart_edit` 条目内的 `"slide"` 是 **1-based** | 混用会错一页 | 中 |
| 11 | 删除后的 path | ops 算子基本不删形状,path 在 input 里仍然有效 | 大量 `delete`,input 的位置编号已经变了;delta 记的是**原始**编号(有意为之) | 评分器按 path 在结果文件里找形状会找错 | 高 |

### 最要命的那条:`expected` 缺失是**朝着放水的方向静默失败**

不是抛异常,是给满分。实测(`ops.py` 原样调用,`entry` 用 degrade_exec 的形状、
`rec` 用一个**完全没修过**的形状):

```
strip_outershdw    (1.0, 'restored')
flatten_3d         (1.0, 'n/a')
clear_text         (1.0, 'n/a')
reset_crop         (1.0, 'n/a')
```

四个比对器里都写着 `if not exp: return 1.0`——那个分支的本意是
「没记到期望值就别扣分」,在原管线里从没被触发过,因为 `build_task` 总会写
`expected`。换成 `degrade_exec` 的记录,**这个分支变成默认路径**。

再叠上第 5 条(`floor` 缺失,不做归一化),结果是:
**什么都不做的坏文件,在这四个算子上拿 1.0。**
这正好同时打穿 REWARD.md 第四节的两句话——「任何容差都不能让什么都不做拿到分」
和「floor normalization 归一化后必须是 0」——而且**在任务打错分之前完全不可见**:
`evaluator` 不报错、`gates` 全绿、`raw_score` 是 1.0。

**这就是这次调查的正确答案:不是 `bbox` vs `box`(那个会响),
是 `expected` 缺失 + `floor` 缺失,两个都是静默的,方向都是给分。**

---

## 四、`verify.py` 已经证明了什么,以及 `accept` 怎么判

### 四个探针

| 探针 | 怎么造结果文件 | 阈值 | 4 个任务实测 |
|---|---|---|---|
| `equivalent_repr` | 对 **gt** 做:`schemeClr` → 解析成 `srgbClr`(保留 alpha)、`outerShdw` 属性重排后重建元素 | ≥ **0.85** | 1.0 × 4 |
| `input_floor` | 直接评 `input.pptx` | ≤ **0.30** | 0.0 × 4 |
| `scripted_restore` | 对每条 delta 调 `op.restore(el, m)` 把记录的 XML 塞回去;图表走 `build_chart_from_entry` 重建 | ≥ **0.90** | 1.0, 1.0, 1.0, **0.375** |
| `blind_solver` | 只看坏文件:给每个形状喷一个默认阴影 + 把坐标吸附到推断出的网格线(容差 0.3in) | ≤ **0.40** | 0.0 × 4 |

### 八例对抗电池及其带宽

```
perfect / writer_noise / snippet_restore / srgb_equiv   [0.85, 1.01]
half_restore                                            [0.15, 0.85]
noop                                                    [-0.01, 0.30]
wrong_params                                            [-0.01, 0.50]
paste_hack                                              [-0.01, 0.001]   ← 靠闸门
```

`wrong_params` 是这里最用心的一个:结构全还原、参数全改错(阴影方向翻 180°、
虚线删掉、`srgbClr` 取反、`schemeClr` 解析后取反、图表数值 `v*1.9+7`),
而且**按元素去重**——同一个形状被两条指令点名时,翻两次 180° 会自己抵消。

### `accept` 的判据

```python
accept = all(4 个探针达标) and adversarial_pass_rate >= 0.9 and package_ok
```

`package_ok = pkg_check.check(input).ok and not leaks and not dead_rels`。

注意 `MIN_ADV = 0.9` 配 8 个用例:7/8 = 0.875 < 0.9。
**实际含义是「八个全过」,没有中间地带。**

### 对照 REWARD.md 第五节的五个探针

| REWARD.md | verify.py | 是否等价 |
|---|---|---|
| `equivalent_repr` | ✅ 有 | **不等价,只覆盖 1/3**。REWARD.md 第一节右列点了三类等价:主题色→sRGB(✅ 覆盖)、形状重建后 XML 结构不同(❌ 没测)、裁剪图转 `blipFill`(❌ 没测) |
| **`roundtrip_identity`** | ❌ **没有** | 最接近的是对抗用例 `writer_noise`,但它是 `Presentation(gt).save()` —— **python-pptx 的往返,不是应用的往返**。REWARD.md 要的是「原稿过一遍 WPS / LO 仍然 1.0」,那才是第二节量的那件事。而且它是 case 不是 probe |
| `input_floor` | ✅ 有 | 半等价。阈值是 **≤0.30**,REWARD.md 说的是 **0.0**。更要紧的是:`build_task.compute_floors` 已经把每条的 floor 减掉了,所以这个探针**部分是同义反复**——它验证的是 `compute_floors` 跑过了,不是容差本身安全 |
| `scripted_restore` | ✅ 有 | 等价,而且真的会咬:4 个任务里有 1 个测出 0.375,`accept=False` 被拦下 |
| `blind_solver` | ✅ 有 | 等价。0.0 × 4 |

**五个里有四个,缺的正好是 REWARD.md 说「建议第一个写」的那个。**

### `verify.py` 没在证明、但 REWARD.md 明确要求的三件事

1. **几何容差比 REWARD.md 的既定标准宽 12 倍。**
   `pos_emu = 0.12in`,半档到 `0.30in`;REWARD.md 第三节①说默认容差就是浮点噪声档
   (`POS_TOL = 0.01in`),比这更宽要有 WPS 实测证据撑着。0.12in 是 12×,0.30in 是 30×。
   而且 `evaluator` 的 `untouched_slides` 闸门用的是 `untouched_slide_ok(tol_frac=0.92)`
   + 中心 **0.15in** —— 等于对未降级页面默认放行 8% 的形状和 0.15in 的漂移。
2. **`_geom_compare` 对每个形状都判 w/h(`size_ratio=0.12`),包括开了 autofit 的文本框。**
   REWARD.md 第三节②说这类尺寸**必须从评分项里移除**,不是放宽。第 2.4 节量到的
   0.600in 字体差异正好落在这里。
3. **`_clear_text_compare` 直接比文本,没有 `APP_FILLED` 豁免。**
   REWARD.md 第七节第一条踩了两次的那个坑(日期 / 页码 / 页眉页脚的文本是应用生成的),
   在 `roundtrip.py` 里修好了,在 `ops.py` 里**不存在**。`clear_text.applies_to` 也没有
   排除 placeholder(只有 `color_mutate` 检查了 `semantic == "content"`)。

---

## 五、逐文件裁决

前提:本项目的既定原则是「没跑过的阶段不该出现在一条要给别人用的流水线里」。
所以每一条都要说清楚**跑过没有、跑过几个任务**。

### `ops.py` —— **重写注册表结构,挑着搬比对器函数体**

**证据:** 11 份 ops 风格 delta,4 个任务过了 `verify`。17 个比对器里
**12 个在真任务上出现过,5 个从没跑过**,其中 `reset_crop` 实测是坏的。
没有任何单元测试(`seed1_mvp/` 只有 `test_census.py`)。

**为什么不能原样搬:**
- 注册表的键(算子名)和本仓库 25 个**零交集**,25 个里 14 个连语义对应物都没有;
- `Op` 数据类把 `applies_to` / `apply` / `restore` / `cost` / `min_area_in2` /
  `describe` 和 `compare` 绑在一起。本仓库的 apply 侧是 `degrade_exec`,已经跑过
  十个 deck 并被 `operator-audit.md` 逐个修过;**把 apply 一起搬进来等于回退**。
  奖励阶段只需要 `compare`,注册表应该是「算子名 → 比对器」的**纯映射**;
- `restore` 值得单独保留一份——`scripted_restore` 探针靠它——但它得改成读
  `degrade_exec` 的记录键,而且必须先解决截断(第三节第 6 条)。

**能直接搬的函数体(改掉读 `expected` 的那一行就行):**
`_geom_compare`、`_clear_text_compare`、`_color_compare`、`_line_compare`、
`_line_gone_compare`、`_line_color_compare`、`_grad_compare`、
`_strip_effect_factory` 里的四个、`_flatten_3d_compare`、`_chart_compare`。
`_chart_compare` 尤其值得搬:「必须是原生图表」+ 数值 2% 相对容差,是唯一一个
把「贴张图」判死的比对器。

**必须新写、且 census 已经把事实备好的:**
表格(`rec.table`:rows/cells/merges/col_widths)、run 级文本样式
(`text_style.shape_text`,含继承解析)、z 序(`rec.z`/`top_z`)、
连接符(`rec.st_cxn`/`end_cxn`)、动画(`anim_steps.py`,census 不记)、
SmartArt(`rec.diagram`)、以及最大的那块:**任意形状的删除**。
**编组还原在现有匹配机制下不可测**(`leaf_shapes` 过滤掉 group),
这不是补个比对器的事,是 `evaluate.match_slides` 的结构问题。

**容差不能照抄。** `DEFAULT_TOL` 是按 LibreOffice 最坏情况定的,REWARD.md 第 2.2 节
已经把那一列数从「容差来源」降级成「语料脆弱度信号」。搬 `DEFAULT_TOL` 进来
就是把一个不参与评分的渲染器的 p90 当标准,REWARD.md 第 2.2 节明确说这是纯送分。

### `evaluator.py` —— **改动后搬**(骨架好,契约和闸门要改)

**证据:** 同上 4 个任务。171 行,没有测试。

**值得留的:** 「delta 记录说是什么算子,注册表给比对器」这个骨架就是 REWARD.md 想要的
形状;`_nearest_chart` 排除幸存图表那段(不排除的话,成对图表的 deck 什么都不做能拿
1/3 单元分)是真踩过坑才写得出来的;`anti_paste` / `no_stray_additions` /
`untouched_slides` 三道闸门方向都对。

**必须改:**
1. `delta["tolerance"]` 改成有缺省;`m["bbox"]` 改成读 `box` 并做中心换算;
   `m["key"]` 只用于报告,不该硬依赖;
2. **floor 归一化必须搬进评分链**。现在 `compute_floors` 在 `build_task.py` 里,
   本仓库没有等价物 —— 不搬进来的话第三节第 5 条那个静默满分就成立;
3. `slide_count` 硬闸和 `delete_slides` / `reorder_slides` 直接冲突,这两个算子跑过;
4. 顶层的 `deleted_slides` / `reorder_slides` / `cleared_notes` / `layout_edits`
   四类记录现在**完全不进入打分**;
5. `untouched_slides` 的 0.15in / 92% 带宽要按 REWARD.md 第三节①重新定;
6. `walk_elements` 的 path 语义要换成 `census.shape_children`(第三节第 9 条)。

### `verify.py` —— **改动后搬,但先补 `roundtrip_identity`**

**证据:** 4 个任务,3 个 `accept=True`,1 个 `accept=False`(`scripted_restore=0.375`)。
这是三个文件里**唯一有过真判决记录的**——它拦下过东西,不是摆设。没有单元测试。

**值得留的:** 四个探针的构造思路、八例带宽表、`wrong_params` 的按元素去重、
`MIN_ADV` 配 8 例等于「全过」这个隐含的严格性。`pkg_check` 那段可以原样用——
两个仓库的 `pkg_check.leak_check` 逻辑一致(diff 50 行,`leak_check` 本体相同)。

**必须改:**
1. **先写 `roundtrip_identity`**,这是 REWARD.md 第五节点名要第一个写的,
   而且本仓库已经有现成料:`pptxgym/roundtrip.py`(`_facts` / `compare` /
   `POS_TOL=0.01in` / `APP_FILLED` 角色归键)和 `pptxgym/wps_roundtrip.py`
   (Xvfb + xdotool 真开真存,十个 deck 0.0%)。**这一块是本仓库领先的,不是要搬的。**
   注意 `writer_noise` 不能顶替它:那是 python-pptx 的往返,不是应用的往返;
2. `equivalent_repr` 要补 REWARD.md 第一节右列另外两类(形状重建后的结构差异、
   裁剪图转 `blipFill`);
3. `scripted_restore` 依赖 `op.restore` 和完整的 `removed_xml`,截断问题不解决
   这个探针只会给出比真实低的分,而且是被 `except: pass` 吃掉的静默低分;
4. `input_floor` 阈值 0.30 该收到 0.0(REWARD.md 原话),但要连带
   floor 归一化一起搬,否则这个探针会变成同义反复。

### `pkg_check.py` —— **不用搬**

本仓库已有,且是更新的那份(408 行 vs 360 行)。`operator-audit.md` 的
「发现 2」记着它的已知缺口:`leak_check` 对 `chart_edit` 和 `smartart_drop_nodes`
返回 `{"applicable": false}`。

### `styles.py` / `render.py` / `smartart.py` —— **不用搬,已经字节相同**

`diff` 为 0。`census.py`(差 56 行)、`charts.py`(差 35 行)、
`anim_steps.py`(差 12 行)本仓库都是更新的一侧。

---

## 六、按 REWARD.md 的框架该怎么排

如果目标是「一份 delta 记录 → 比对器注册表 → 奖励函数」,顺序是:

0. **先补 `expected` 契约**。要么在 `degrade_exec` 每个算子里加一个统一的
   `prior` 字段(不删现有的 `was_*`,只是加一个规范位置),要么写一层
   「算子名 → 从这条记录里把原值挖出来」的适配器。后者更好:
   **不改已经跑过十个 deck 并被逐个审过的执行器**。适配器天然就是
   「比对器按算子语义写,不许看具体配方」(REWARD.md 第七节)的落点。
   同时把截断上限提到能完整还原(或者干脆存哈希 + 完整 XML 到旁边的文件)。
1. **`roundtrip_identity`**,用本仓库的 `wps_roundtrip` + `roundtrip.compare`。
   最便宜,而且按 REWARD.md 的说法,它会直接指出比对器里哪些项目**根本不该存在**
   (autofit 尺寸、placeholder 文本)。
2. **floor 归一化进评分链**,不是留在建任务那一侧。
3. 按用量顺序补比对器:`delete`(56%)→ `set_font`(16%)→ 表格三件套 →
   `text_runs` → 动画 / 转场 → SmartArt。
4. 容差从 0 起(`POS_TOL=0.01in`),每放宽一档都要重跑对抗电池确认
   `noop` 没涨(REWARD.md 第四节)。
5. 编组类(`ungroup`)先别做成独立任务——比对器缺是小事,
   `match_slides` 看不见 group 是结构问题,`operator-audit.md` 的「发现 1」
   已经从可解性那一侧独立指出过同一件事。
