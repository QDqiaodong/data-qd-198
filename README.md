# data-qd-198 · 平面折纸层叠与来源

用精确半平面裁剪、反射与整叠翻转，追踪二维纸片层序、正反面以及查询点的原纸坐标；另提供原纸标注归档与窗口重建、训练/评测清单发布的离线入口。

## 安装与运行

Python >=3.11，实际验证 CPython 3.12.14 + uv 0.12.19。运行依赖仅标准库；构建固定 setuptools 80.9.0、wheel 0.45.1。每个项目独立虚拟环境，公共下载缓存可以复用。

在本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/paper-layers --source fixtures/input.json --output run/demo.json
```

以上安装命令已在本地公共缓存存在时真实执行。首次使用且缓存为空时须先准备 Python 和锁定构建包，或在允许联网的安装环境移除对应 `--offline`；运行与测试不需要网络。`.venv` 互不共享，未纳入Git。

样例4×2纸张对折后2层，带重数材料面积8；点(1,1)顶层背面来自原纸(3,1)，点(3,1)在纸外。

演示标准输出摘要：

```json
{"layers": 2, "material_area": "8", "queries": 3}
```

完整证据写入 `run/demo.json`。数值输入为契约约定的十进制字符串或整数分数对，输出的精确有理数采用整数或 `n/d` 字符串，避免浮点舍入。

## 标注归档与窗口重建

版面标注要交给下游时必须还原成原纸编号、正反面与纸面区域。两个离线入口在 `paper_layers.archive`：

```python
from paper_layers.archive import build_archive, dumps, loads, rebuild_window

data = {
    'layout': {'sheets': [{'id': 'S', 'width': '4', 'height': '2'},
                          {'id': 'C', 'width': '5', 'height': '2'}],
               'folds': [{'id': 'half', 'a': '1', 'b': '0', 'c': '2'}]},
    'batches': [
        {'id': 'L', 'priority': 1,
         'marks': [{'id': 'l1', 'code': 'qc-l', 'rect': ['-1', '0', '2', '2']}]},
        {'id': 'H', 'priority': 9,
         'marks': [{'id': 'h1', 'code': 'qc-h', 'rect': ['-0.5', '0.5', '1', '1.5']}]},
    ],
}
open('handoff.json', 'w', encoding='utf-8').write(dumps(build_archive(data)))

# 新进程里只需这个 JSON 文件：
archive = loads(open('handoff.json', encoding='utf-8').read())
result = rebuild_window(archive, {'sheet': 'S', 'face': 'back',
                                  'rect': ['2', '0', '4', '2'],
                                  'ignore_batch': 'H'})
```

归档是自包含普通 JSON（格式/版本、原纸 id 与范围、批次优先级、原纸坐标区域），单对象上限 8,000,000 字节的紧凑 UTF-8 JSON；超限直接报错而不截断。`ignore_batch` 只让该批次退出本次重建，包本身不变。窗口结果只含真正被标注覆盖的精确多边形，不含空白或外接矩形。字段、判优、字节限额和错误类型详见 [数据契约](docs/contract.md)。

## 训练/评测清单发布

版面回归样本按采集件与版面结果防泄漏分组后整组分流。`paper_layers.manifest` 的离线入口 `build_manifest(data)` 生成两条流水线可直接加载的完整样本 ID 清单，不改变既有接口与命令行：

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
    'previous': None,   # 或上期返回的 {'train': ..., 'evaluation': ...}
    'quota': {'evaluation_total': 1, 'evaluation_field': 0},
})
# result == {'train': ['s1'], 'evaluation': ['s2']}
```

- 分组：采集件 ID 相同（非空）或版面结果等价即关联，关联可传递。版面等价比较已有折叠计算的完整有序碎片结果（原纸按序尺寸、每块碎片的原纸序号、朝上面、精确多边形、回原纸的仿射映射），忽略纸号、步骤号与步骤路径，不用浮点容差；等价只约束分流，从不删重样本。
- 发布：组内旧归属决定整组去向；同时含两种旧归属抛 `PreviousAssignmentConflictError`。其余整组分配使评测总数与其中现场样本数恰好命中配额，无解抛 `QuotaUnsatisfiableError`，绝不交付部分清单或移动旧样本。多解时取按 ID 升序的评测列表字典序最小者，打乱输入数组顺序不影响结果。
- 错误：无效输入（缺/多字段、非法类型或 ID、重复身份、旧清单未知 ID、无效版面、配额越界）抛普通 `ValueError`；两种业务失败是 `ManifestError` 子类（仍是 `ValueError`），与无效输入可区分。
- 资源：`tests/manifest_resource_job.py` 在 192 MiB 地址空间上限内跑 8,192 组、24,576 条样本的发布验收（本地实测峰值约 67 MiB）。输入上限 40,000 条样本、评测配额 2,048（现场 1,024）。

分组、等价与发布的完整规则见 [数据契约](docs/contract.md)。

## 版面回归证据源仓与接收端

参考版面常晚于采集观测到达。可持久化的源仓把逐条观测归属到生效版本，接收端只持证据副本，仅凭普通 JSON 增量包离线推进；不新增 CLI、界面或报表，既有版面计算、原纸标注、清单与旧入口行为不变。两者都是本地单写者，正常关闭后可重开继续（标准库 sqlite，无新依赖）。

```python
from paper_layers.repository import SourceRepository
from paper_layers.receiver import Receiver
from paper_layers.evidence import package_dumps, package_loads

repo = SourceRepository.create('run/source', 'dataset-c')
repo.ingest({'batch': 'b1', 'references': [...], 'observations': [...]})  # 返回提交号
package = repo.package(0, repo.head())          # after/through 区间的净行变化包

receiver = Receiver.create('run/receiver', 'dataset-c')
receiver.apply(package_loads(package_dumps(package)))   # 普通 JSON 往返后更新
receiver.position(), receiver.rows()
# 重开：SourceRepository.open('run/source')、Receiver.open('run/receiver')
```

- 录入批次恰有 `batch, references, observations`，后两个数组合计 1..1,000 条；整批原子生效，坏批次不占提交号。同批次 ID 同内容重试返回原提交号，改内容拒绝；对象字段排列不影响重试身份，数组次序属于内容。
- 参考声明含 `case, revision, effective_from, layout, probes`；同用例内版本 ID 与生效起点各自唯一，迟到更正可在已有起点间插入新版本。参考命中在录入时经现有 `process` 预先物化，保留全部命中及顺序（含折痕多重命中），只取 `layer, sheet, face, source_point`，不比较步骤路径、不替换纸号、不用浮点容差。
- 观测按“不晚于 `observed_at` 的最大生效起点”归属，且只在该版本找探针；找不到版本或探针就不产生证据行，不退回旧版本。迟到参考改变受影响时间段内的归属，不改变更早提交号的历史。证据行含观测全部字段、`revision, expected_hits, matches`，版本本身即追溯依据。
- 增量包带 `format/version/dataset/after/through/changes`，每个改变的 ID 升序一次、附完整 `before/after`（无行为 `null`），两端一致的 ID 不输出，空区间也返回。接收端只收同数据集且起点等于当前位置的下一段，先核对全部 `before` 再整包事务应用（空包也推进）；同包重发只得到同一次成功，旧包/跳段/交叠与前置行不符均可区分拒绝且现场不多不少一条。

源仓、接收端、增量包的字段、身份、错误分类（`BatchConflictError`/`RepositoryRangeError` 与 `ReceiverInputError`/`ReceiverPositionError`/`ReceiverConflictError`）、重启与 JSON 往返方法见 [数据契约](docs/contract.md)。

## 复验测点清单与离线判读

同一批待复验纸件可能来自几个已知版面版本，现场没有可信版本标签。`paper_layers.recheck` 的两个离线入口选出抗缺站与误读的测点清单、产出自包含交接件，并让判读方仅凭交接件 JSON 离线认出版本；不新增 CLI 或界面，既有入口行为不变。

```python
from paper_layers.recheck import (build_handoff, interpret_handoff,
                                  handoff_dumps, handoff_loads)

handoff = build_handoff({
    'versions': [{'id': 'U', 'layout': {'sheets': [...], 'folds': []}}, ...],  # 2..12 个
    'probes': [{'id': 'b1', 'station': 'B', 'point': ['2', '1'], 'cost': 2}, ...],  # 1..22 条、3..8 站
    'required': ['a0'],          # 不可取消的已校准探针，可为空
})
open('handoff.json', 'w', encoding='utf-8').write(handoff_dumps(handoff))

# 现场只有这份 JSON：一个站停工、其余站点至多一个测点读错时仍能认出版本
result = interpret_handoff(handoff_loads(open('handoff.json', encoding='utf-8').read()),
                           {'stopped': 'A', 'readings': [{'probe': 'b1', 'hits': [...]}]})
# {'status': 'determined', 'version': 'U', 'remeasure': 'c1'}
# {'status': 'ambiguous', 'versions': ['U', 'V']}  /  {'status': 'unexplainable'}
```

- 标准答案用既有 `process` 逐版本计算，保留完整有序命中列表（含折痕多重命中、空列表），每条只取 `layer, sheet, face, source_point`。清单包含全部必选探针，且对任意两个版本、任意一个站点，移除该站全部探针后仍至少有三条答案不同的探针。可行时依次取总成本最低、探针数最少、升序 ID 列表字典序最小者；输入排列不影响交付。两版本在全部测点不可区分也不合并，无可行清单抛 `InfeasibleManifestError`。
- 交接件是带 `format/version` 的自包含普通 JSON：入选探针（站点与精确坐标）发给测量方，版本 ID 与去重共享的标准答案表供判读方；单件紧凑 UTF-8 JSON 不超过 250,000 字节，超限抛 `HandoffSizeError`，不降格、不截断。
- 判读只比较实际收到的测点（缺读不同于收到空列表），保留至多一次错读的全部版本：恰好一个返回确定版本与需复测的错读探针（完全一致为 `null`），多个返回升序候选列表，没有则报告无法解释。坏交接件、未知格式版本、非法引用与无效读数抛 `HandoffInputError`，与无解及大小限制可区分；合法的错读是证据不是坏输入。

选测约束、交接件字段、判读规则与错误分类的完整约定见 [数据契约](docs/contract.md)。

## 折叠层叠件的粘接界面

制样组按折叠后的层叠状态粘接纸件。`paper_layers.bonds` 的离线入口 `build_bonds(data)` 接收既有版面（`sheets, folds`）与 0..1,024 条粘接规范，返回完整的相邻层界面清单、每条界面两侧分别在两张原纸上的精确区域，以及逐项满足情况；不新增 CLI、界面或物理折纸模拟，既有 `process`、点查询、标注归档、清单与证据入口行为不变。

```python
from paper_layers.bonds import build_bonds

result = build_bonds({
    'layout': {'sheets': [{'id': 'A', 'width': '4', 'height': '2'},
                          {'id': 'B', 'width': '4', 'height': '2'}],
               'folds': [{'id': 'half', 'a': '1', 'b': '0', 'c': '2'}]},
    'bonds': [{'id': 'ab8', 'side_a': {'sheet': 'A', 'face': 'front'},
               'side_b': {'sheet': 'B', 'face': 'back'}, 'min_area': '8'}],
})
# 三个相邻层界面，面积均为 4；其中 A.front/B.back 出现在两个不同层位，合计 8，满足 ab8
```

- 每个版面位置取覆盖它的碎片按最终层序排列，只有局部层栈里彼此相邻的两片才构成接触；全局不相邻的两片可在中间碎片不覆盖处直接接触，被中间层遮住的投影交集不计。边/点相接不算界面，投影相同但深度不同的界面分别交付，接触不连通时拆成互不重叠的简单多边形。
- 每条界面保留两片的最终层号（零起始，局部相邻不保证连续）、下侧纸号与朝上面、上侧纸号与朝上面（上层取其当前朝上面之反面）、投影接触多边形、两侧各自映回原纸的精确多边形及面积；坐标一律约分整数或 `n/d` 字符串，不用外接矩形。
- 粘接规范按无序材料面匹配、跨层位累加可用面积；`available_area >= min_area` 即满足。返回全部界面与全部判定（含未满足），空规范仍返回真实界面清单；坏输入整体抛 `ValueError`，不留部分结果。`bonds` 数组改序不改变结果。

界面构造、输出字段与错误的完整约定见 [数据契约](docs/contract.md)。

## 折叠工艺修订的精确影响区

`paper_layers.revision.compare_revisions(data)` 比较同一投影平面中的 `reference` 与 `candidate` 两版版面，用精确多边形 overlay 对每个正面积位置构造底到顶材料栈；即使外轮廓和可见顶层相同，隐藏层顺序、朝上面或投影到原纸的仿射映射变化也会归为 `changed`。入口也以 `build_revision` / `build_revision_report` 名称从 `paper_layers` 导出。

```python
from paper_layers.revision import compare_revisions, RevisionComplexityError

report = compare_revisions({
    'reference': {'sheets': [{'id': 'A', 'width': '4', 'height': '2'},
                             {'id': 'B', 'width': '4', 'height': '2'}],
                  'folds': []},
    'candidate': {'sheets': [{'id': 'B', 'width': '4', 'height': '2'},
                             {'id': 'A', 'width': '4', 'height': '2'}],
                  'folds': []},
})
report['changed_area']   # 8；整片 [0,0,4,2] 是一个 changed 凸多边形
```

- 每个影响区给出 `category`（`added` / `removed` / `changed`）、凸简单 `polygon`、投影面积，以及 `reference_stack` / `candidate_stack` 完整底到顶层栈；每层含原纸 `sheet`、当前朝上 `face` 和六个精确仿射系数 `affine=[m00,m01,m10,m11,tx,ty]`。
- 材料身份不含折叠步骤 ID、路径文字或全局层号；步骤改名结果不变。所有坐标、系数、面积均为约分整数或 `n/d` 字符串。
- 返回多边形内部互不重叠，恰好覆盖全部影响区；四类面积之和等于两版材料投影并集面积，而不是带层重数的 `material_area`。
- 坏输入整体抛普通 `ValueError`；精确分区超过 32,768 个凸多边形时抛可区分的 `RevisionComplexityError`，不返回局部或近似报告。

可直接运行 `python examples/revision_example.py`（源码树未安装时用 `PYTHONPATH=src python examples/revision_example.py`）。完整 JSON 字段、仿射口径、稳定性和复核用法见 [数据契约](docs/contract.md)。

## 已执行验证

依赖一致性检查、134 项业务测试（含上述归档/重建验收样本、清单发布的正常/冲突/无解/稳定排序场景、192 MiB 资源验收、证据源仓/接收端的迟到版本归属、删除与插回、空包推进、前置行不符整包回滚、批次重试身份、真实 JSON 往返与重开场景、复验清单的抗缺站选测、站点重指派、不可区分版本无解、1024 层交接件大小限制内往返、缺站判读，粘接界面的双纸折叠三界面/跨层位面组合/三纸窄中间层透见/零面积折痕/分离纸件/空规范/改序稳定与坏输入整拒场景，以及修订影响区的换序隐藏栈/对折反射与移除区/非对称折叠增改删/空步骤改名/凸多边形互不重叠与面积恒等/稳定性/复杂度错误区分场景）及命令行端到端演示通过（本地 CPython 3.11.2 直接运行 `PYTHONPATH=src python -m unittest discover -s tests`）。另核重复目标、符号链接目标和重复JSON键都返回2，既有输出字节不变，非法输入不发布报告。输出路径必须未存在，重新演示请选新文件名。

源码入口为 `src/paper_layers/service.py`、`src/paper_layers/archive.py`、`src/paper_layers/bonds.py`、`src/paper_layers/revision.py`、`src/paper_layers/manifest.py` 与 `src/paper_layers/recheck.py`，测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。


## 当前边界

理想全叠平折，不判断机械可折性、厚度或碰撞。折痕边界包含所有命中碎片；材料面积不是投影并集。

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。
