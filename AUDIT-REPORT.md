**仓库检查报告 — 2026 年 10 月 1 日**

按照 ponytail 的 full 模式检查了源代码、测试、词表与词典、打包配置及 CI 工作流。确认并修复了三类代码问题，删除一个冗余占位文件。修复复用现有函数和 Python 标准库，三个源文件净增加 3 行。

| 问题 | 修复前的影响 | 已完成的修复 |
| --- | --- | --- |
| 同日最新词频为空时继续使用旧结果 | 后一次抓榜的有效标题全部被过滤时，不保存词频 CSV；累计和趋势仍读取当天较早的非空快照。 | 即使词频为空也原子写入只有表头的快照，正确取代同日旧结果；当天词云仍不生成。 |
| 未闭合引号的 CSV 被当成有效数据 | 默认 CSV 解析器接受截断的引号字段；聚合可能计入损坏文件的部分词频，停用词分析也可能用坏数据覆盖上轮结果。 | 词频读取、原始榜单读取和人工决定表读取统一启用 strict=True。聚合跳过整份坏文件并警告；停用词分析报错并保留已有分析结果。 |
| 历史词频 CSV 缺少字段时静默消失 | 缺少「词」或「词频」列时读取结果为空，没有提示，累计数据少了一份快照却难以发现。 | 验证两列均存在，字段不完整时按坏文件处理并在警告中列出路径。合法的只有表头的空快照仍可读取。 |

对应实现位于 [cli.py](src/bilibili_ranker/cli.py)、[storage.py](src/bilibili_ranker/storage.py) 和 [stopword_optimizer.py](src/bilibili_ranker/stopword_optimizer.py)。README 已补充空词频快照的行为说明。

回归验证覆盖同日重跑的累计及趋势结果、未闭合引号、缺失两种字段、坏文件不贡献部分计数、坏榜单不覆盖上一轮输出，以及损坏的人工决定表。新增或扩展的测试在修复前确实失败，修复后全部通过。

冗余文件方面，已删除仓库根目录的 `.gitkeep`：根目录已有受版本控制的文件，这个占位文件没有用途。未发现需要删除的运行时模块或依赖。`py.typed` 是类型标记，词表、用户词典与测试夹具均有实际用途；虚拟环境、构建目录和工具缓存属于已忽略的本地产物。

另处理了一个本地缓存问题：源码的趋势词频阈值为 2，旧的 `cli.cpython-312.pyc` 却执行阈值 1，造成原有趋势测试失败。已清理项目源代码和测试的字节码缓存并重新生成，原有 138 个测试随后全部通过。pytest 默认临时目录的权限报错通过指定仓库内的 `--basetemp` 解决，没有为环境问题修改业务逻辑。

| 验证项目 | 最终结果 |
| --- | --- |
| 完整测试集 | 143 passed，0 failed，0 skipped |
| 测试覆盖率 | 97.59%，通过仓库设置的 90% 门槛 |
| Ruff 检查 | 通过 |
| Ruff 格式检查 | 23 个 Python 文件通过 |
| mypy | 11 个源文件通过，按配置检查 Python 3.10 类型兼容性 |
| Wheel 构建 | 使用本机已有的 setuptools 84.0.0 成功构建 |
| Wheel 内容 | 逐一确认 Python 源文件与工作区一致，包含 py.typed、用户词典和三份词表，无字节码缓存 |
| Wheel 运行检查 | 从 Wheel ZIP 导入包，加载内置词典与停用词资源，实际完成分词 |
| Git 差异检查 | git diff --check 通过 |

完整测试可在当前环境重跑：

```powershell
$env:PYTHONUTF8 = '1'
.venv\Scripts\python.exe -m pytest tests -q --tb=short --basetemp=build\audit-verification --cov=bilibili_ranker --cov-report=term-missing --cov-fail-under=90
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
```

验证环境为 Windows、Python 3.12.14。网络集成测试使用模拟响应和本地 HTTP 服务，词云测试实际生成 PNG。本次未访问 B 站线上接口，其他操作系统与 Python 版本的 CI 矩阵也未在本机运行。
