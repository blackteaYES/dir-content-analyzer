# CLAUDE.md

此文件为 Claude Code（claude.ai/code）在本仓库中工作时提供指导。

## 项目概览

`dir-content-analyzer` 是一个 Python 项目，用于分析本地目录内容。入口文件是 `main.py`，提供两个 CLI 子命令：

- `large-files`：扫描目录并按大小查询大文件。
- `classify`：使用 LangChain 对话模型对文件清单进行归类分析。

## 常用命令

查询当前目录中的大文件：

```bash
python main.py large-files . --top 20 --min-size-mb 10
```

使用 OpenAI 兼容对话模型进行文件归类分析：

```bash
python main.py classify . --top 20
```

开启文本内容抽样后再归类：

```bash
python main.py classify . --top 20 --model gpt-4.1-mini --sample-content --sample-bytes 2000
```

如果本项目使用 `uv`，可通过项目环境运行：

```bash
uv run python main.py large-files . --top 20 --min-size-mb 10
uv run python main.py classify . --top 20
```

如果要使用浏览器图形界面：

```bash
uv run streamlit run streamlit_app.py
```

HTML 报告可通过浏览器打印为 PDF：下载 HTML 后用浏览器打开，按 `Ctrl+P` 并选择“另存为 PDF”。

同步项目依赖：

```bash
uv sync
```

使用标准 Python 工具以 editable 模式安装项目：

```bash
python -m pip install -e .
```

`classify` 子命令默认使用 OpenAI 兼容模型接口，需要配置：

- `OPENAI_API_KEY`：必需。
- `OPENAI_BASE_URL`：可选，用于 OpenAI 兼容服务。
- `OPENAI_MODEL`：默认模型名称；也可通过 `--model` 临时覆盖。

当前 `pyproject.toml` 中没有配置测试运行器、lint 工具、formatter 或构建后端。在添加相应依赖或配置之前，不要假设 `pytest`、`ruff` 或包构建命令可用。

## 架构

- `main.py`：CLI 解析、目录扫描、大文件查询、文本样本读取、LangChain 归类分析和 AI 推荐删除逻辑。
- `streamlit_app.py`：浏览器图形界面，复用 `main.py` 的核心函数，提供参数预设、系统弹窗选择文件夹、排除目录编辑、模型配置测试、概览/大文件/文件类型/AI 归类视图、HTML 导出和移入回收站功能。

扫描行为：
- `large-files` 只读取文件元数据，不读取文件内容。
- `classify` 默认只发送路径、扩展名、大小和修改时间。开启 `--sample-content` 时，对 `TEXT_EXTENSIONS` 定义的文本扩展名（`.py/.txt/.md/.json/.yaml/.toml/.cfg/.log` 等）读取最多 `--sample-bytes` 字节内容样本。
- 默认排除目录：`.git`、`.venv`、`__pycache__`、`node_modules`，可通过 `--exclude` 追加。

删除安全：
- AI 推荐删除只推荐扫描结果中真实存在的相对路径，不自动删除。
- 用户需在界面勾选文件并输入 `DELETE` 确认，才会调用 `send2trash` 移入系统回收站。

Streamlit 导入的 `main.py` 导出：`DEFAULT_EXCLUDES`、`FileInfo`、`collect_file_info`、`format_size`、`format_time`、`run_classification`、`run_delete_recommendations`。修改这些导出需同步检查 Streamlit 调用。
