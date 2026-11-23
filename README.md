# data-qd-198 · 平面折纸层叠与来源

用精确半平面裁剪、反射与整叠翻转，追踪二维纸片层序、正反面以及查询点的原纸坐标；另提供原纸标注归档与窗口重建的离线入口。

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

## 已执行验证

依赖一致性检查、26项业务测试（含上述归档/重建验收样本）及命令行端到端演示通过（本地 CPython 3.11.2 直接运行 `PYTHONPATH=src python -m unittest discover -s tests`）。另核重复目标、符号链接目标和重复JSON键都返回2，既有输出字节不变，非法输入不发布报告。输出路径必须未存在，重新演示请选新文件名。

源码入口为 `src/paper_layers/service.py` 与 `src/paper_layers/archive.py`，测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。


## 当前边界

理想全叠平折，不判断机械可折性、厚度或碰撞。折痕边界包含所有命中碎片；材料面积不是投影并集。

本仓提供当前文档描述的业务基线；尚未实现的演进能力见契约中的集成说明，不承诺固定数量的后续任务。
