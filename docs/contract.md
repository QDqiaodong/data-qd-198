# 平面层叠模型与标注归档

## 平面层叠基线

教学制图者输入 sheets（1..10张从原点开始的正宽高矩形，列表由底到顶）、folds（0..10）、queries（0..100）。所有尺寸与坐标为精确十进制字符串。不同数组内部 id 唯一；输出精确有理数。

折线 a*x+b*y=c，a、b 不可同时零。每一层均按正负半平面裁成凸多边形，正侧整叠翻到负侧上方；静止片顺序保留，移动片反转原有上下顺序，每次翻折反转正反面。该操作明确定义为全叠理想平折，不判断真实折纸可折性或机械碰撞。允许整叠翻转和空操作。每个碎片用原纸 id 与逐步 stay/move 路径追踪，超过4096片拒绝。

material_area 是所有多边形面积的带重数之和，逐折必须等于原纸总面积，不是投影并集面积。查询包含闭边界，折痕上可能同时命中两个同源碎片；顺序仍按模型的图层顺序，top 指最后命中项，不能把边界重数当真实厚度。逆序应用反射恢复各命中点的原纸坐标。

不支持局部指定纸层、厚度、曲面、撕裂或自动规划。后续冲孔与展开轨迹可接入已有原纸坐标/碎片路径，纸面印记可随同一反射链变换。样例全自制。CLI严格字段/重复键检查，输出只创建不覆盖，失败返回2。

## 标注归档与窗口重建

标注先落在**平面版面**（折好后的投影位置）上，交付下游时必须换成**原纸 id、正反面和纸面坐标**。`paper_layers.archive` 提供两个离线入口，沿用上面的层序、反射链与十进制坐标规则，不新增物理模型、交互功能或命令行交付。

### 生成原纸标注包

```python
from paper_layers.archive import build_archive, dumps

data = {
    'layout': {
        'sheets': [{'id': 'S', 'width': '4', 'height': '2'},
                   {'id': 'C', 'width': '5', 'height': '2'}],
        'folds': [{'id': 'half', 'a': '1', 'b': '0', 'c': '2'}],
    },
    'batches': [
        {'id': 'L', 'priority': 1, 'marks': [
            {'id': 'l1', 'code': 'qc-l', 'rect': ['-1', '0', '2', '2']}]},
        {'id': 'H', 'priority': 9, 'marks': [
            {'id': 'h1', 'code': 'qc-h', 'rect': ['-0.5', '0.5', '1', '1.5']}]},
    ],
}
archive = build_archive(data)          # 普通 dict
package_text = dumps(archive)          # 紧凑 UTF-8 JSON 文本
open('handoff.json', 'w', encoding='utf-8').write(package_text)
```

- `layout` 只含旧格式 `sheets` 和 `folds`（字段、顺序、1..10 张与 0..10 道折的限额不变）；不再接受 `queries`，也不需要旧点查询。
- `batches` 为 0..64 个批次，合计 0..4096 条标注，两者都可为空。批次含 `id, priority, marks`；批号在批次间唯一，`priority` 是 0..1,000,000,000 的整数，越大越优先。标注含 `id, code, rect`，标注号在批次内唯一，`code` 是下游要用的质检代码。所有标识沿用基线 ID 语法 `[A-Za-z0-9_:-]{1,64}`。
- `rect` 为 `[xmin, ymin, xmax, ymax]`，四项均为基线十进制字符串，长宽必须大于零，允许超出纸面。缺字段、多字段、重复身份、坏布局或坏矩形都抛 `ValueError`。
- 归属规则：每条矩形独立处理。版面内只归它所在位置**最上面的碎片**，与该碎片上已有的其它标注无关；纸外部分丢弃。一条矩形横跨甲、乙两块碎片的版面位置时，两段分别沿各自的反射链回到原纸，只登记到各自的纸号和朝上的那个面。被压在下层的碎片永远收不到标注；后来撤销某个批次也不会揭开它们。仅在边或点相接不产生区域。原点查询在闭折痕上的多重命中规则保持原样。
- 归档按原纸坐标保存每条标注完整的可见面积（只被层叠几何裁剪，不被别的标注裁剪），因此之后可以撤掉任意一个批次而不丢下层的标注。最终优先关系只在重建时计算。

### 归档包格式（版本 1）

`build_archive` 返回、`dumps` 写出的就是下面这个普通 JSON 对象，没有外部引用、压缩或二进制旁路（下例 `regions` 只列出一条，实际会包含上面两个批次的全部可见区域）：

```json
{
  "format": "paper-annotation-archive",
  "version": 1,
  "sheets": [{"id": "S", "width": 4, "height": 2}, {"id": "C", "width": 5, "height": 2}],
  "batches": [{"id": "H", "priority": 9}, {"id": "L", "priority": 1}],
  "regions": [
    {"sheet": "S", "face": "back", "batch": "H", "mark": "h1", "code": "qc-h",
     "polygon": [[4, "1/2"], [4, "3/2"], [3, "3/2"], [3, "1/2"]]}
  ]
}
```

- `format`/`version` 标识格式；`sheets` 给出原纸 id 与范围；`batches` 给出批号与优先级。
- `regions` 是原纸空间内的简单多边形（逆时针、无重复/共线顶点），坐标是约分后的整数或 `整数/正分母` 字符串，绝不使用浮点。同一条标注在同一纸面可能拆成多块。
- 交接单对象上限 **8,000,000 字节**，按紧凑 JSON（逗号冒号分隔、无外围空白）的完整 UTF-8 字节数计。`dumps` 超限会直接抛 `ValueError`，不会截断或写出一份貌似成功的包。归档只在签名范围内保存交付必需的数据，不塞入外部数据或编码/压缩的二进制。

### 重建一个原纸窗口

```python
from paper_layers.archive import loads, rebuild_window, serialize_window

archive = loads(open('handoff.json', encoding='utf-8').read())
result = rebuild_window(archive, {
    'sheet': 'S', 'face': 'back',
    'rect': ['2', '0', '4', '2'],
    'ignore_batch': 'H',
})
text = serialize_window(result)
```

`loads` 只做反序列化和重复键/有限数检查，不需要底稿、原始矩形或重新折纸。请求字段恰好是 `sheet, face, rect, ignore_batch`：纸号必须存在于包中；`face` 为 `front` 或 `back`；`rect` 用基线十进制语法且必须整个落在该纸范围内；`ignore_batch` 为 `null` 或包中存在的批号。指定批号时，只有这一个批次的所有标注退出本次重建，归档内容本身不变。未知格式版本、无效请求字段或未知批号都抛 `ValueError`。

窗口结果：

```json
{"sheet": "S", "face": "back", "rect": [2, 0, 4, 2], "ignore_batch": "H",
 "regions": [{"batch": "L", "mark": "l1", "code": "qc-l",
              "polygon": [[2, 0], [4, 0], [4, 2], [2, 2]]}]}
```

- 每个位置取尚参加标注中 `priority` 最高者；同优先级取 `(批号, 标注号)` 字典序最小者。
- 只返回窗口内真正有标注覆盖的正面积区域，每个区域带胜出者的 `batch`、`mark`、`code`。空白处不输出，绝不拿外接矩形填充。
- 同一胜出者的覆盖可以拆成多个简单多边形；输出多边形内部互不重叠，合起来恰好覆盖窗口内所有应交付的正面积区域，边界可以共享。残缺的零面积接触不产生记录。
- 坐标同样用约分整数或分数字符串。相同输入结果稳定；批次或标注数组改序不改变区域含义和胜出身份。

### 错误识别

生成与重建的所有业务错误都是 `ValueError`，消息说明是哪一类：字段/额外字段、批次或标注越界、重复身份、非法 ID、非正矩形、超出纸面的请求、未知纸号/面/批号、不支持的格式或版本、超限包。`loads` 还拒绝重复 JSON 键、`NaN`/`Infinity` 和非对象根节点。CLI 与 `process` 的既有行为不变。

## 训练/评测清单发布

版面回归样本按采集件与版面结果防泄漏分组，再整组分到训练侧或评测侧。`paper_layers.manifest` 的离线入口 `build_manifest(data)` 生成两条流水线可直接加载的完整样本 ID 清单；不改变既有版面计算、查询、标注接口与命令行。

```python
from paper_layers.manifest import (
    build_manifest, ManifestError,
    PreviousAssignmentConflictError, QuotaUnsatisfiableError)

result = build_manifest({
    'samples': [
        {'id': 's1', 'origin': 'o1', 'channel': 'field',
         'layout': {'sheets': [{'id': 'P', 'width': '4', 'height': '2'}],
                    'folds': [{'id': 'f', 'a': '1', 'b': '0', 'c': '2'}]}},
        {'id': 's2', 'origin': None, 'channel': 'lab',
         'layout': {'sheets': [{'id': 'P', 'width': '3', 'height': '2'}],
                    'folds': []}},
    ],
    'previous': None,
    'quota': {'evaluation_total': 1, 'evaluation_field': 0},
})
# result == {'train': ['s1'], 'evaluation': ['s2']}
```

### 输入

`data` 恰含 `samples, previous, quota` 三个字段，输入只读不改写。

- `samples` 为 0..40,000 条记录，每条恰含 `id, origin, channel, layout`。样本 `id` 唯一，与采集件 ID 都沿用基线 ID 语法 `[A-Za-z0-9_:-]{1,64}`（两个命名空间相互独立）。`origin` 是采集件 ID 或 `null`（未提供来源，不构成关联）。`channel` 为 `field`（现场）或 `lab`（实验室）。`layout` 是既有 `sheets, folds` 格式（1..10 张、0..10 道折等限额不变），不含点查询。
- `previous` 为 `null`（首次发布）或恰含 `train, evaluation` 两个数组，列出本数据集曾向对应方发布的样本 ID；可以直接传入上期结果。数组内及数组间不得重复，且每个 ID 必须仍在当前 `samples` 中。样本 ID 从不重新分配；当前输入可以修订采集件关联，但旧清单中的归属不可更改。
- `quota` 恰含 `evaluation_total, evaluation_field` 两个非负整数（布尔值不算）：前者 0..2,048，后者 0..1,024 且不得大于前者。配额按样本数计算，不按采集件或版面种类数计算。

### 分组（防泄漏）

两条记录满足任一条件即关联：采集件 ID 相同且非空；或版面结果等价。关联可传递，先用当前全部样本求出完整的不可拆分组，再处理发布归属。同一采集件的变体即使版面不同也同组；版面别名是分流约束，不是删重——不同样本 ID 全部保留。

版面等价比较的是已有折叠计算产出的**完整**有序碎片结果（底到顶），不是输入文本、总面积、最顶层轮廓或少数查询命中：原纸按 `sheets` 输入序号对应，数量及按序尺寸必须相等；碎片逐项对应，所在原纸序号、朝上面、精确覆盖多边形、从版面坐标到该原纸坐标的仿射映射都必须相等。比较忽略纸号、步骤号与步骤路径；同一多边形不因起始顶点、绕行方向或边上多出共线点而不同。不合并不同碎片，不使用浮点容差。

### 发布规则

- 组内含有之前发到训练侧的样本，整组进训练；含有之前发到评测侧的样本，整组进评测。
- 一组同时含两种既有归属：抛出 `PreviousAssignmentConflictError`，即使放宽配额也不交付。
- 其余整组分配，使最终评测总数及其中现场样本数同时恰好命中配额。无解（包括固定评测样本已经超过配额）时抛出 `QuotaUnsatisfiableError`，不交付部分清单、近似配额，也不移动旧样本。
- 多解时选**按样本 ID 升序排序后的评测 ID 列表字典序最小**的一份，训练侧取其补集。打乱 `samples` 或旧清单数组的顺序不影响结果；`sheets` 与 `folds` 的业务顺序仍有意义。

### 输出与错误契约

成功结果恰含 `train, evaluation` 两个升序 ID 数组，所有输入样本各出现一次。错误分两类，可以互相区分：

- 无效输入抛普通 `ValueError`：缺字段、多字段、非法类型或 ID、重复身份、旧清单中的未知 ID、无效版面、配额越界等。
- 业务失败抛 `ManifestError` 的子类（同时也是 `ValueError`）：`PreviousAssignmentConflictError`（既有归属冲突）与 `QuotaUnsatisfiableError`（配额不可满足）。`except ValueError` 仍能一网打尽，`except ManifestError` 只命中业务失败。

### 资源验收

每期发布在没有临时盘的 192 MiB 离线作业进程中排程。`tests/manifest_resource_job.py` 是可复现验收：独立进程在生成夹具前把地址空间上限设为 192 MiB（Linux `RLIMIT_AS`），不借子进程或磁盘文件中转计算。夹具首次发布，含 8,192 个采集件组：按最小样本 ID 排序依次是 2,730 个 A 类组（3 条、2 条现场）、2,731 个 B 类组（2 条、1 条现场）、2,731 个 C 类组（4 条、1 条现场）；样本 ID 用字母前缀、定宽组号和组内编号按组连续排序；所有版面互不相同（单纸无步骤，高 1，宽从 1 递增）。配额 2,048 条评测、其中 1,024 条现场，必须恰好选中各类前 408、4、204 个组，训练侧为余集。本地 CPython 3.11 实测峰值约 67 MiB、约 7 秒；40,000 条全独立样本的上限场景约 107 MiB、约 22 秒。

## 版面回归证据源仓与接收端

采集站与参考版面维护组分别送数据，且参考版本经常晚到。`paper_layers.repository` 的源仓与 `paper_layers.receiver` 的接收端把逐条观测归属到正确的参考版本，并以普通 JSON 增量包离线交付；不新增 CLI、界面或报表，既有 `process`、归档、清单与命令行行为不变。两者都是本地单写者、可持久化、正常关闭后用 `open` 重开继续（标准库 sqlite，WAL 单文件加 `-wal/-shm`，无外部依赖）。

### 源仓录入与归属

```python
from paper_layers.repository import SourceRepository

repo = SourceRepository.create('run/source', 'dataset-c')   # dataset 不可变
seq = repo.ingest({'batch': 'b1', 'references': [...], 'observations': [...]})
# 重开：SourceRepository.open('run/source')，dataset/head/全部历史仍在
```

- 入仓批次恰含 `batch, references, observations`；两个数组合计 1..1,000 条。所有标识沿用基线 ID 语法 `[A-Za-z0-9_:-]{1,64}`；时间（`effective_from`、`observed_at`、提交区间端点）是非负整数，布尔值不算整数。提交号由源仓从 1 连续分配，与现场序号是两个独立概念。
- 参考声明恰含 `case, revision, effective_from, layout, probes`。同一用例内 `revision` 与 `effective_from` 分别唯一，录入后不可覆盖；迟到更正只能以新的 `effective_from` 在已有起点之间插入新版本。不同用例可复用版本 ID。`layout` 是旧 `sheets, folds` 格式，`probes` 是旧 `queries` 格式（`{id, point}`）的探针列表，可为空。
- 参考命中在录入时调用现有 `process` 一次预先物化，保留其**全部命中及顺序**（含折痕上的多重命中），每条只取 `layer, sheet, face, source_point`；不比较步骤路径、不替换纸号、不使用浮点容差。因此读包不会重新执行任何历史版面，也不需要原始输入文件。
- 观测恰含 `id, case, observed_at, probe, hits`，观测 ID 在整个数据集中唯一。每个命中恰有上述四字段：层号为非负整数、列表按层号严格升序、面为 `front`/`back`、来源点是两个输出口径的约分整数或分数字符串（`"2"`、`"-1/2"`，不接受 `"2/4"` 或十进制小数）。空命中列表合法。命中与参考不符、纸号不在参考版面中都是业务证据而非坏批次。
- 归属只看截至某提交号已录入的参考与观测：先在同用例内取 `effective_from <= observed_at` 的最大起点，再只在该版本寻找观测指定探针。找不到版本或探针时保留观测但不产生证据行，**绝不退回旧版本找探针**。物化行以观测 ID 为键，恰含观测全部字段加 `revision, expected_hits, matches`；`matches` 是逐项精确比较的布尔值。行中的版本本身即追溯依据，即使两版参考命中完全相同，换版也是一次变化。迟到参考只改变受影响时间段内的归属，不改变更早提交号可见的历史。
- 整批原子生效：结构、版面、身份冲突或内容不合法时整批失败，不留参考、观测，也不占提交号。相同批次 ID 以同一内容重试（对象字段排列不影响身份，数组次序属于内容）返回原提交号；同 ID 改内容抛 `BatchConflictError`。不同批次即使行内容重复也不得复用已有唯一身份（用例内版本、用例内生效起点、全局观测 ID）。

### 增量包

`repo.package(after, through)` 返回普通 dict（`0 <= after < through <= repo.head()`，0 表示空仓），结构固定为：

```json
{"format": "paper-evidence-package", "version": 1, "dataset": "dataset-c",
 "after": 1, "through": 2,
 "changes": [{"id": "o25", "before": {完整行或 null}, "after": {完整行或 null}}]}
```

- 包标明数据集与两个提交号；每个改变的 ID 升序出现一次，附两端完整 `before`/`after` 行，无行的一端为 `null`。两端一致（含区间内变化后又恢复）的 ID 不输出；空区间也返回带空 `changes` 的完整包。同一区间在再次录入、源仓重开后返回相同内容。
- 区间非法抛 `RepositoryRangeError`；录入侧身份冲突抛 `BatchConflictError`，两者都是 `RepositoryError`（也是 `ValueError`），结构/内容不合法抛普通 `ValueError`。
- `paper_layers.evidence.package_dumps/package_loads` 提供紧凑 UTF-8 普通 JSON 往返；`loads` 拒绝重复 JSON 键、`NaN/Infinity` 与非对象根节点。

### 接收端

接收端从指定数据集的第 0 号空副本开始，只持有证据行副本（无原始参考、布局、独立观测存储或联网）：

```python
from paper_layers.receiver import (Receiver, ReceiverInputError,
    ReceiverPositionError, ReceiverConflictError)
from paper_layers.evidence import package_dumps, package_loads

receiver = Receiver.create('run/receiver', 'dataset-c')
result = receiver.apply(package_loads(package_dumps(repo.package(0, 1))))
receiver.position()          # 当前提交位置
receiver.rows()              # 全部完整行，按 ID 升序
receiver.row('o25')          # 单行或 None；'o25' in receiver 判存在
receiver.close()
receiver = Receiver.open('run/receiver')   # 重启后位置与行原样保留
```

- 只接收同数据集且 `after == receiver.position()` 的下一段；应用前逐个核对 `before` 与本地完整行相同（`null` 表示本地无此 ID）。整包在一个事务内按 `after` 行插入、替换或删除，并把位置改为 `through`；空变化列表同样推进位置。
- 重复收到与上次成功应用内容完全相同的整包，返回原成功结果（含原位置），不重做、不推进；其余旧包、跳段、交叠包按区间/位置错误拒绝。包结构、键与行身份、单条变更形状或前置行不合法时拒绝整包，所有行与位置不变。
- 三类可区分错误（均为 `ReceiverError`，也都是 `ValueError`）：`ReceiverInputError`（坏输入/身份/行形状）、`ReceiverPositionError`（数据集不符、区间或位置错误）、`ReceiverConflictError`（形状合法的 `before` 行与本地完整行不符）。
- 成功结果恰含 `dataset, after, through, changes, position`，其中 `changes` 是本包变更条数、`position == through`。

### 重启与普通 JSON 往返方法

两个存储都只依赖各自目录里的 sqlite 文件：先 `close()`（或用 `with`）正常关闭，新进程里对同目录调用 `SourceRepository.open` / `Receiver.open` 即可继续；重开后重取历史区间得到内容相同的包，接收端重发上次成功的包只得到同一次成功结果。跨设备交接不复制存储目录，而是用 `package_dumps` 把包写成紧凑 JSON 文本、在对端用 `package_loads` 读回（可用任意普通 JSON 通道，如文件或消息体），接收端仅凭包即可更新行与位置。

## 复验测点清单与离线判读

同一批待复验纸件可能来自几个已知版面版本中的任意一个，现场没有可信版本标签。`paper_layers.recheck` 的离线入口 `build_handoff(data)` 选出一份抗缺站与误读的测点清单并产出自包含交接件；判读方仅凭交接件 JSON、一个停工站点和实际读数用 `interpret_handoff(handoff, request)` 离线认出版本。既有版面计算、标注归档、清单发布与证据仓接口不变。

```python
from paper_layers.recheck import (
    build_handoff, interpret_handoff, handoff_dumps, handoff_loads,
    RecheckError, InfeasibleManifestError, HandoffSizeError, HandoffInputError)

handoff = build_handoff({
    'versions': [{'id': 'U', 'layout': {'sheets': [...], 'folds': []}}, ...],
    'probes': [{'id': 'b1', 'station': 'B', 'point': ['2', '1'], 'cost': 2}, ...],
    'required': ['a0'],
})
open('handoff.json', 'w', encoding='utf-8').write(handoff_dumps(handoff))

# 现场新进程里只有这份 JSON，没有布局也不能重跑折叠：
result = interpret_handoff(handoff_loads(open('handoff.json', encoding='utf-8').read()),
                           {'stopped': 'A',
                            'readings': [{'probe': 'b1', 'hits': [...]}]})
```

### 选测输入与标准答案

`data` 恰含 `versions, probes, required` 三个字段，输入只读不改写。

- `versions` 为 2..12 条 `{id, layout}`，版本 ID 唯一，`layout` 是既有 `sheets, folds` 格式（限额不变）。
- `probes` 为 1..22 条 `{id, station, point, cost}`，探针 ID 唯一；站点 ID 可复用，不同站点总数 3..8；`point` 沿用基线查询坐标语法；`cost` 是 1..1,000,000 的整数（布尔值不算）。所有标识沿用基线 ID 语法。位置相同但 ID 不同的是不同的实际测量，可能由不同站点完成，选测与判读都按 ID 区分。
- `required` 为 0..22 个不可取消的已校准探针 ID，不得重复或引用未知 ID，可为空。

每个测点的标准答案用既有 `process` 计算一次，保留**完整有序命中列表**（含折痕多重命中），每条只取 `layer, sheet, face, source_point`，不带步骤路径；空列表是正常读数。两个版本只有在该测点的列表逐项精确相等时才算答案相同。

### 选测规则

清单以探针为单位选择，包含全部必选探针。可行性约束：对任意两个不同候选版本、任意一个站点，把该站全部探针移除后，清单上仍须有**至少三条**两版本标准答案不同的探针。只在所有站在线时验证区分能力会低估所需探针数。即使两个版本在全部候选测点上都不可区分也不合并——此时无可行清单，抛 `InfeasibleManifestError`，不交付部分方案。

可行时按字典序取最优：总成本最低，其次探针数最少，再其次升序探针 ID 列表字典序最小。改变 `versions`、`probes`、`required` 的排列不改变交付内容。

### 交接件格式（版本 1）

交接件是带格式标识的自包含普通 JSON 对象，不依赖原布局、外部文件或压缩二进制：

```json
{"format": "paper-recheck-handoff", "version": 1,
 "stations": ["A", "B", "C"],
 "probes": [{"id": "b1", "point": ["2", "1"], "station": "B"}],
 "answers": [[{"layer": 0, "sheet": "P", "face": "front", "source_point": ["2", "1"]}]],
 "versions": [{"id": "U", "expected": {"b1": 0}}]}
```

- `stations` 记录全部原始候选站点（升序），包括未入选任何测点的站；停工校验以此为准。
- `probes` 是升序的入选清单，每条恰含 `id, station, point`；坐标规范化为输出口径的约分整数或分数字符串，可直接发给测量方。
- `answers` 是去重后的标准答案表（普通 JSON 结构共享重复内容）；`versions` 按版本 ID 升序，每个 `expected` 恰好把每个入选探针指到表中的一条答案。
- 单件完整 UTF-8 紧凑 JSON（逗号冒号分隔、无外围空白）不超过 **250,000 字节**。超限抛 `HandoffSizeError`，不替换成次优清单、不截断标准答案、不只交测量清单。`handoff_dumps` 对手工构造的超限对象同样报错；`handoff_loads` 拒绝超限文本、重复 JSON 键、`NaN`/`Infinity` 与非对象根节点。

### 现场判读

`interpret_handoff(handoff, request)` 的 `request` 恰含 `stopped, readings`：

- `stopped` 为 `null` 或交接件 `stations` 中记录的任一原始候选站点，即使该站未入选任何测点。
- `readings` 为 0..22 条 `{probe, hits}`，每条对应一个**入选且未停工**的探针，每个探针至多一条；`hits` 沿用证据仓观测命中格式（四字段、层号严格升序、输出口径来源点），纸号不必出现在任何候选版面中。未收到读数的探针不参与判断（缺读不同于收到空列表）。

判读只比较实际收到的测点：某版本在某测点的整个命中列表不同，记该测点一次错读。保留所有至多一次错读的版本：恰好一个返回 `{'status': 'determined', 'version': id, 'remeasure': 错读探针或 None}`；多个返回 `{'status': 'ambiguous', 'versions': [升序 ID]}`，不按成本、版本名或最少错读强行选一个；没有则返回 `{'status': 'unexplainable'}`。即使有额外缺读也按此规则判读。两个输入对象都不会被修改。

### 错误识别

- 生成侧无效输入（缺/多字段、非法类型或 ID、重复身份、未知必选 ID、无效版面或坐标、成本或站点数越界）抛普通 `ValueError`。
- 业务失败抛 `RecheckError` 子类（仍是 `ValueError`）：`InfeasibleManifestError`（无可行清单）与 `HandoffSizeError`（交接件超限），与无效输入可区分。
- 判读侧坏输入——无效交接件、未知格式或版本、非法引用（未知探针、缺失探针、答案下标越界、未知站点）、未知停工站点、对未入选或已停工探针的读数、重复读数、无效命中——抛 `HandoffInputError`（也是 `RecheckError`），与无解及大小限制可区分。格式合法但内容不符的错读是现场证据，不是坏输入。


