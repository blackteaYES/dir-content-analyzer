from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_EXCLUDES = {".git", ".venv", "__pycache__", "node_modules"}
TEXT_EXTENSIONS = {
    ".cfg",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".py",
    ".rst",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class FileInfo:
    path: Path
    relative_path: str
    size: int
    modified_at: float

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower() or "无扩展名"


@dataclass(frozen=True)
class FileSummary:
    info: FileInfo
    sample: str | None = None


def iter_files(root: Path, excludes: set[str]):
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in excludes]
        current_path = Path(current_root)
        for file_name in file_names:
            path = current_path / file_name
            if any(part in excludes for part in path.parts):
                continue
            yield path


def collect_file_info(root: Path, excludes: set[str]) -> list[FileInfo]:
    files: list[FileInfo] = []
    for path in iter_files(root, excludes):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        try:
            relative_path = str(path.relative_to(root))
        except ValueError:
            relative_path = str(path)
        files.append(
            FileInfo(
                path=path,
                relative_path=relative_path,
                size=stat.st_size,
                modified_at=stat.st_mtime,
            )
        )
    return sorted(files, key=lambda item: item.size, reverse=True)


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def print_large_files(files: list[FileInfo], top: int, min_size_mb: float) -> None:
    min_size = int(min_size_mb * 1024 * 1024)
    matches = [file for file in files if file.size >= min_size][:top]

    if not matches:
        print(f"未找到大于等于 {min_size_mb:g} MB 的文件。")
        return

    print(f"找到 {len(matches)} 个大文件：")
    print(f"{'大小':>12}  {'修改时间':19}  路径")
    print("-" * 80)
    for file in matches:
        print(f"{format_size(file.size):>12}  {format_time(file.modified_at):19}  {file.relative_path}")


def read_text_sample(path: Path, sample_bytes: int) -> str | None:
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return None
    try:
        data = path.read_bytes()[:sample_bytes]
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return None


def build_summaries(files: list[FileInfo], sample_content: bool, sample_bytes: int) -> list[FileSummary]:
    summaries: list[FileSummary] = []
    for file in files:
        sample = read_text_sample(file.path, sample_bytes) if sample_content else None
        summaries.append(FileSummary(info=file, sample=sample))
    return summaries


def build_classification_prompt(root: Path, summaries: list[FileSummary], sample_content: bool) -> str:
    lines = [
        "请分析下面的本地目录文件清单，并用中文输出文件归类结果。",
        "要求：",
        "1. 按用途或类型归类，例如源码、配置、文档、数据、日志、构建产物、依赖目录等。",
        "2. 指出占用空间较大的文件或类别。",
        "3. 给出可以优先清理或进一步检查的建议。",
        "4. 不要假设未出现在清单中的文件。",
        f"分析目录：{root}",
        f"是否包含文本内容样本：{'是' if sample_content else '否'}",
        "",
        "文件清单：",
    ]

    for index, summary in enumerate(summaries, start=1):
        info = summary.info
        lines.extend(
            [
                f"{index}. 路径：{info.relative_path}",
                f"   扩展名：{info.suffix}",
                f"   大小：{format_size(info.size)}",
                f"   修改时间：{format_time(info.modified_at)}",
            ]
        )
        if summary.sample:
            lines.append("   内容样本：")
            lines.append(summary.sample[:2000])
        lines.append("")

    return "\n".join(lines)


def run_classification(
    root: Path,
    files: list[FileInfo],
    model_name: str | None,
    sample_content: bool,
    sample_bytes: int,
) -> str:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("缺少 OPENAI_API_KEY 环境变量，无法调用 OpenAI 兼容对话模型。")
    if not model_name:
        raise RuntimeError("缺少模型名称，请在 .env/环境变量中配置 OPENAI_MODEL，或通过 --model 传入。")

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("缺少 LangChain 依赖，请先运行：uv sync") from exc

    summaries = build_summaries(files, sample_content, sample_bytes)
    prompt = build_classification_prompt(root, summaries, sample_content)
    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = ChatOpenAI(model=model_name, base_url=base_url, api_key=os.getenv("OPENAI_API_KEY"), temperature=0)
    response = model.invoke(
        [
            {"role": "system", "content": "你是一个本地目录文件分析助手，擅长根据文件元数据和可选内容样本进行归类。"},
            {"role": "user", "content": prompt},
        ]
    )
    return str(response.content)


def build_delete_recommendation_prompt(root: Path, summaries: list[FileSummary], sample_content: bool) -> str:
    lines = [
        "请分析下面的本地目录文件清单，推荐可以优先考虑删除的文件。",
        "要求：",
        "1. 只从文件清单中选择文件，不要编造路径。",
        "2. 仅推荐明显可能是临时文件、日志、构建产物、缓存、重复导出物或体积异常且可进一步确认的文件。",
        "3. 对源码、配置、文档、依赖声明、环境配置等文件要谨慎，除非有明确理由，否则不要推荐删除。",
        "4. 只输出 JSON 数组，不要输出 Markdown、解释或代码块。",
        "5. JSON 每一项必须包含 path、reason、risk、confidence 四个字符串字段。",
        "6. risk 只能是 低、中、高；confidence 只能是 低、中、高。",
        f"分析目录：{root}",
        f"是否包含文本内容样本：{'是' if sample_content else '否'}",
        "",
        "文件清单：",
    ]

    for index, summary in enumerate(summaries, start=1):
        info = summary.info
        lines.extend(
            [
                f"{index}. 路径：{info.relative_path}",
                f"   扩展名：{info.suffix}",
                f"   大小：{format_size(info.size)}",
                f"   修改时间：{format_time(info.modified_at)}",
            ]
        )
        if summary.sample:
            lines.append("   内容样本：")
            lines.append(summary.sample[:2000])
        lines.append("")

    return "\n".join(lines)


def parse_json_array(content: str) -> list[dict[str, object]]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("模型未返回 JSON 数组。")
    return [item for item in data if isinstance(item, dict)]


def run_delete_recommendations(
    root: Path,
    files: list[FileInfo],
    model_name: str | None,
    sample_content: bool,
    sample_bytes: int,
) -> list[dict[str, str]]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("缺少 OPENAI_API_KEY 环境变量，无法调用 OpenAI 兼容对话模型。")
    if not model_name:
        raise RuntimeError("缺少模型名称，请在 .env/环境变量中配置 OPENAI_MODEL。")

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("缺少 LangChain 依赖，请先运行：uv sync") from exc

    valid_paths = {file.relative_path for file in files}
    summaries = build_summaries(files, sample_content, sample_bytes)
    prompt = build_delete_recommendation_prompt(root, summaries, sample_content)
    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = ChatOpenAI(model=model_name, base_url=base_url, api_key=os.getenv("OPENAI_API_KEY"), temperature=0)
    response = model.invoke(
        [
            {"role": "system", "content": "你是一个谨慎的本地文件清理建议助手。你只给建议，不执行删除。"},
            {"role": "user", "content": prompt},
        ]
    )

    try:
        rows = parse_json_array(str(response.content))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"模型删除建议不是有效 JSON：{exc}") from exc

    recommendations = []
    for row in rows:
        path = str(row.get("path", "")).strip()
        if path not in valid_paths:
            continue
        risk = str(row.get("risk", "")).strip()
        confidence = str(row.get("confidence", "")).strip()
        recommendations.append(
            {
                "path": path,
                "reason": str(row.get("reason", "")).strip(),
                "risk": risk if risk in {"低", "中", "高"} else "中",
                "confidence": confidence if confidence in {"低", "中", "高"} else "中",
            }
        )
    return recommendations


def classify_files(root: Path, files: list[FileInfo], args: argparse.Namespace) -> None:
    selected_files = files[: args.top]
    model_name = args.model or os.getenv("OPENAI_MODEL")
    try:
        result = run_classification(root, selected_files, model_name, args.sample_content, args.sample_bytes)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基于 LangChain 的本地目录内容分析器。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_scan_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("path", type=Path, help="要分析的本地目录路径。")
        command_parser.add_argument("--top", type=int, default=20, help="最多输出或分析的文件数量。")
        command_parser.add_argument(
            "--exclude",
            action="append",
            default=[],
            help="要排除的目录名，可重复传入。默认排除 .git、.venv、__pycache__、node_modules。",
        )

    large_files = subparsers.add_parser("large-files", help="按大小查询目录中的大文件。")
    add_scan_args(large_files)
    large_files.add_argument("--min-size-mb", type=float, default=10, help="大文件阈值，单位 MB。")

    classify = subparsers.add_parser("classify", help="使用 LangChain 对话模型对文件进行归类分析。")
    add_scan_args(classify)
    classify.add_argument("--model", help="OpenAI 兼容对话模型名称；未传入时读取 OPENAI_MODEL。")
    classify.add_argument("--sample-content", action="store_true", help="读取文本文件内容样本并发送给模型。")
    classify.add_argument("--sample-bytes", type=int, default=2000, help="每个文本文件最多读取的样本字节数。")

    return parser.parse_args()


def main() -> None:
    load_dotenv(override=True, verbose=True)
    args = parse_args()
    root = args.path.expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"路径不存在：{root}")
    if not root.is_dir():
        raise SystemExit(f"路径不是目录：{root}")

    excludes = DEFAULT_EXCLUDES | set(args.exclude)
    files = collect_file_info(root, excludes)

    if args.command == "large-files":
        print_large_files(files, args.top, args.min_size_mb)
    elif args.command == "classify":
        classify_files(root, files, args)


if __name__ == "__main__":
    main()
