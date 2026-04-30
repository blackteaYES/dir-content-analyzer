from __future__ import annotations

import ctypes
import os
import tkinter as tk
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import markdown
import streamlit as st
from dotenv import load_dotenv
from send2trash import send2trash

from main import (
    DEFAULT_EXCLUDES,
    FileInfo,
    collect_file_info,
    format_size,
    format_time,
    run_classification,
    run_delete_recommendations,
)

load_dotenv(override=True)

PRESETS = {
    "快速扫描": {"top": 20, "min_size_mb": 10.0, "sample_content": False, "sample_bytes": 2000},
    "深度分析": {"top": 100, "min_size_mb": 1.0, "sample_content": False, "sample_bytes": 2000},
    "AI 精准归类": {"top": 50, "min_size_mb": 0.0, "sample_content": True, "sample_bytes": 4000},
}

def get_downloads_dir() -> Path:
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None, 0, buffer)
        if result == 0:
            downloads = Path(buffer.value).parent / "Downloads"
            if downloads.exists():
                return downloads
    return Path.home() / "Downloads"


COMMON_PATHS = {
    "下载": str(get_downloads_dir()),
}


def init_state() -> None:
    defaults = PRESETS["快速扫描"]
    st.session_state.setdefault("path_text", ".")
    st.session_state.setdefault("scan_source", "目录路径")
    st.session_state.setdefault("preset", "快速扫描")
    st.session_state.setdefault("top", defaults["top"])
    st.session_state.setdefault("min_size_mb", defaults["min_size_mb"])
    st.session_state.setdefault("sample_content", defaults["sample_content"])
    st.session_state.setdefault("sample_bytes", defaults["sample_bytes"])
    st.session_state.setdefault("exclude_items", [{"排除目录名": item} for item in sorted(DEFAULT_EXCLUDES)])
    st.session_state.setdefault("display_mode", "展示最大 N 个文件")
    st.session_state.setdefault("display_count", 20)
    st.session_state.setdefault("files", [])
    st.session_state.setdefault("root", Path(".").resolve())
    st.session_state.setdefault("analysis_result", "")
    st.session_state.setdefault("delete_recommendations", [])
    st.session_state.setdefault("model_api_key", os.getenv("OPENAI_API_KEY", ""))
    st.session_state.setdefault("model_base_url", os.getenv("OPENAI_BASE_URL", ""))
    st.session_state.setdefault("model_name", os.getenv("OPENAI_MODEL", ""))


def apply_preset() -> None:
    preset = PRESETS[st.session_state.preset]
    st.session_state.top = preset["top"]
    st.session_state.min_size_mb = preset["min_size_mb"]
    st.session_state.sample_content = preset["sample_content"]
    st.session_state.sample_bytes = preset["sample_bytes"]


def open_folder_dialog() -> None:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected = filedialog.askdirectory(title="选择要扫描的文件夹")
    root.destroy()
    if selected:
        st.session_state.folder_dialog_result = selected
        st.rerun()



def on_exclude_change() -> None:
    changes = st.session_state.exclude_editor
    base = st.session_state.exclude_items

    if "added_rows" in changes:
        for row in changes["added_rows"]:
            value = row.get("排除目录名")
            if value and str(value).strip() and str(value).lower() != "nan":
                base.append({"排除目录名": str(value).strip()})

    if "edited_rows" in changes:
        for row_idx, row_changes in changes["edited_rows"].items():
            idx = int(row_idx)
            if 0 <= idx < len(base):
                new_value = row_changes.get("排除目录名")
                if new_value is not None and str(new_value).strip() and str(new_value).lower() != "nan":
                    base[idx] = {"排除目录名": str(new_value).strip()}

    if "deleted_rows" in changes:
        for row_idx in sorted(changes["deleted_rows"], reverse=True):
            idx = int(row_idx)
            if 0 <= idx < len(base):
                base.pop(idx)


def get_excludes() -> set[str]:
    return {row["排除目录名"] for row in st.session_state.exclude_items if row.get("排除目录名")}



def build_file_rows(files, total_size: int) -> list[dict[str, object]]:
    return [
        {
            "路径": file.relative_path,
            "大小": format_size(file.size),
            "字节数": file.size,
            "占比": f"{file.size / total_size * 100:.2f}%" if total_size else "0.00%",
            "修改时间": format_time(file.modified_at),
            "扩展名": file.suffix,
        }
        for file in files
    ]


def build_deletable_rows(files, total_size: int) -> list[dict[str, object]]:
    rows = build_file_rows(files, total_size)
    for row in rows:
        row["删除"] = False
    return rows


def build_extension_rows(files) -> list[dict[str, object]]:
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "size": 0})
    for file in files:
        stats[file.suffix]["count"] += 1
        stats[file.suffix]["size"] += file.size

    rows = []
    for suffix, item in sorted(stats.items(), key=lambda entry: entry[1]["size"], reverse=True):
        count = item["count"]
        size = item["size"]
        rows.append(
            {
                "扩展名": suffix,
                "文件数量": count,
                "总大小": format_size(size),
                "平均大小": format_size(size // count if count else 0),
                "字节数": size,
            }
        )
    return rows


def apply_model_config() -> None:
    if st.session_state.model_api_key:
        os.environ["OPENAI_API_KEY"] = st.session_state.model_api_key
    if st.session_state.model_base_url:
        os.environ["OPENAI_BASE_URL"] = st.session_state.model_base_url
    elif "OPENAI_BASE_URL" in os.environ:
        os.environ.pop("OPENAI_BASE_URL")
    if st.session_state.model_name:
        os.environ["OPENAI_MODEL"] = st.session_state.model_name


def test_model_config() -> str:
    apply_model_config()
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("缺少 LangChain 依赖，请先运行：uv sync") from exc

    model = ChatOpenAI(
        model=st.session_state.model_name,
        base_url=st.session_state.model_base_url or None,
        api_key=st.session_state.model_api_key,
        temperature=0,
    )
    response = model.invoke([{"role": "user", "content": "请只回复 OK"}])
    return str(response.content)


def build_analysis_html(result: str, root: Path) -> str:
    body = markdown.markdown(result, extensions=["extra", "tables"])
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AI 文件归类分析报告</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.7; margin: 0; color: #222; background: #f6f8fa; }}
.report {{ max-width: 960px; margin: 32px auto; padding: 40px; background: #fff; box-shadow: 0 1px 8px rgba(0, 0, 0, 0.08); }}
h1, h2, h3 {{ color: #111; }}
code, pre {{ background: #f6f8fa; padding: 2px 4px; border-radius: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px; }}
th {{ background: #f6f8fa; }}
.meta {{ color: #666; font-size: 14px; margin-bottom: 24px; }}
.actions {{ margin-bottom: 24px; }}
.actions button {{ border: 0; border-radius: 6px; padding: 10px 16px; background: #2b6cb0; color: #fff; cursor: pointer; }}
.print-tip {{ padding: 12px; background: #f6f8fa; border-left: 4px solid #2b6cb0; margin-bottom: 24px; }}
@media print {{
    body {{ background: #fff; }}
    .report {{ margin: 0; padding: 0; max-width: none; box-shadow: none; }}
    .actions, .print-tip {{ display: none; }}
}}
</style>
</head>
<body>
<main class="report">
<h1>AI 文件归类分析报告</h1>
<div class="meta">分析目录：{root}<br>生成时间：{generated_at}</div>
<div class="actions"><button onclick="window.print()">打印 / 另存为 PDF</button></div>
<div class="print-tip">此 HTML 文件只包含报告内容。点击上方按钮或按 Ctrl+P 可将本报告另存为 PDF。</div>
{body}
</main>
</body>
</html>"""


def move_to_recycle_bin(paths: list[Path]) -> int:
    moved = 0
    for path in paths:
        if path.exists() and path.is_file():
            send2trash(str(path))
            moved += 1
    return moved


def refresh_files_after_delete(current_root: Path, excludes: set[str]) -> None:
    st.session_state.files = collect_file_info(current_root, excludes)


def render_deletable_file_table(title: str, table_files, total_size: int, current_root: Path, excludes: set[str], key_prefix: str) -> None:
    st.subheader(title)
    if not table_files:
        st.warning("没有可展示的文件。")
        return

    edited_rows = st.data_editor(
        build_deletable_rows(table_files, total_size),
        use_container_width=True,
        hide_index=True,
        disabled=["路径", "大小", "字节数", "占比", "修改时间", "扩展名"],
        key=f"{key_prefix}_delete_editor",
    )
    selected_paths = [current_root / row["路径"] for row in edited_rows if row.get("删除")]
    if not selected_paths:
        return

    st.warning("选中文件将移入系统回收站。如需恢复，请到系统回收站中手动还原。")
    with st.expander("查看待删除文件", expanded=True):
        for path in selected_paths:
            st.code(str(path), language="text")

    with st.form(f"{key_prefix}_delete_form"):
        confirm_text = st.text_input("如确认删除，请输入 DELETE")
        submitted = st.form_submit_button("移入回收站", type="primary")
        if submitted:
            if confirm_text != "DELETE":
                st.error("确认文本不正确，未执行删除。")
            else:
                try:
                    moved = move_to_recycle_bin(selected_paths)
                    st.success(f"已移入系统回收站：{moved} 个文件。如需恢复，请到系统回收站中手动还原。")
                    refresh_files_after_delete(current_root, excludes)
                    st.rerun()
                except Exception as exc:
                    st.error(f"删除失败：{exc}")


def get_display_files(files):
    if st.session_state.display_mode == "展示所有文件":
        return files
    return files[: st.session_state.display_count]


def build_recommended_delete_rows(recommendations: list[dict[str, str]], file_map: dict[str, FileInfo], total_size: int) -> list[dict[str, object]]:
    rows = []
    for recommendation in recommendations:
        file = file_map.get(recommendation.get("path", ""))
        if not file:
            continue
        rows.append(
            {
                "删除": False,
                "路径": file.relative_path,
                "大小": format_size(file.size),
                "字节数": file.size,
                "占比": f"{file.size / total_size * 100:.2f}%" if total_size else "0.00%",
                "扩展名": file.suffix,
                "风险": recommendation.get("risk", "中"),
                "置信度": recommendation.get("confidence", "中"),
                "推荐原因": recommendation.get("reason", ""),
            }
        )
    return rows


def render_recommended_delete_table(
    recommendations: list[dict[str, str]],
    files: list[FileInfo],
    total_size: int,
    current_root: Path,
    excludes: set[str],
) -> None:
    if not recommendations:
        return

    with st.expander("AI 推荐删除文件清单", expanded=False):
        file_map = {file.relative_path: file for file in files}
        rows = build_recommended_delete_rows(recommendations, file_map, total_size)
        if not rows:
            st.info("推荐结果没有匹配到当前扫描文件，未生成删除表格。")
            return

        st.warning("AI 只提供清理建议，不会自动删除。请确认文件确实不再需要后再勾选删除。")
        edited_rows = st.data_editor(
            rows,
            use_container_width=True,
            hide_index=True,
            disabled=["路径", "大小", "字节数", "占比", "扩展名", "风险", "置信度", "推荐原因"],
            key="ai_recommended_delete_editor",
        )
        selected_paths = [current_root / row["路径"] for row in edited_rows if row.get("删除")]
        if not selected_paths:
            return

        st.warning("选中文件将移入系统回收站。如需恢复，请到系统回收站中手动还原。")
        with st.expander("查看 AI 推荐中待删除文件", expanded=True):
            for path in selected_paths:
                st.code(str(path), language="text")

        with st.form("ai_recommended_delete_form"):
            confirm_text = st.text_input("如确认删除，请输入 DELETE", key="ai_recommended_delete_confirm")
            submitted = st.form_submit_button("移入回收站", type="primary")
            if submitted:
                if confirm_text != "DELETE":
                    st.error("确认文本不正确，未执行删除。")
                else:
                    try:
                        moved = move_to_recycle_bin(selected_paths)
                        st.success(f"已移入系统回收站：{moved} 个文件。如需恢复，请到系统回收站中手动还原。")
                        deleted_relative_paths = {row["路径"] for row in edited_rows if row.get("删除")}
                        st.session_state.delete_recommendations = [
                            item for item in st.session_state.delete_recommendations if item.get("path") not in deleted_relative_paths
                        ]
                        refresh_files_after_delete(current_root, excludes)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"删除失败：{exc}")


init_state()

st.set_page_config(page_title="本地目录内容分析器", layout="wide")
st.title("本地目录内容分析器")
st.caption("扫描本地目录、查看大文件，并使用 LangChain 对话模型进行文件归类分析。")
st.info("使用流程：选择本机目录路径 → 扫描分析 → 查看结果 → 按需使用 AI 归类。默认只发送文件元数据。")

with st.sidebar:
    st.header("分析配置")

    with st.expander("扫描设置", expanded=True):
        st.selectbox("参数预设", list(PRESETS), key="preset", on_change=apply_preset)

        if "folder_dialog_result" in st.session_state and st.session_state.folder_dialog_result:
            st.session_state.path_text = st.session_state.folder_dialog_result
            st.session_state.folder_dialog_result = None

        path_input = st.text_input("目录路径", value=st.session_state.path_text, help="可手动输入路径，也可用系统弹窗选择文件夹。")
        if path_input != st.session_state.path_text:
            st.session_state.path_text = path_input

        action_cols = st.columns(2)
        if action_cols[0].button("选择文件夹", use_container_width=True):
            open_folder_dialog()
        if action_cols[1].button("下载目录", use_container_width=True):
            st.session_state.path_text = COMMON_PATHS["下载"]

        current_path = Path(st.session_state.path_text).expanduser().resolve()
        if current_path.exists() and current_path.is_dir():
            st.success(f"目录有效：{current_path}")
        elif current_path.exists():
            st.error("当前路径不是目录。")
        else:
            st.error("当前路径不存在。")

        st.radio("展示范围", ["展示最大 N 个文件", "展示所有文件"], key="display_mode")
        if st.session_state.display_mode == "展示最大 N 个文件":
            st.number_input("展示文件数量", min_value=1, max_value=100000, step=1, key="display_count")
        st.number_input("AI 分析文件数量", min_value=1, max_value=100000, step=1, key="top")
        st.number_input("大文件阈值（MB）", min_value=0.0, max_value=1048576.0, step=0.1, format="%.2f", key="min_size_mb")

    with st.expander("高级扫描参数", expanded=False):
        st.caption("可直接在表格中新增、修改或删除排除目录。")
        st.data_editor(
            st.session_state.exclude_items,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="exclude_editor",
            on_change=on_exclude_change,
        )

    with st.expander("模型设置", expanded=True):
        st.text_input("OPENAI_API_KEY", key="model_api_key", type="password")
        st.text_input("OPENAI_BASE_URL", key="model_base_url")
        st.text_input("模型名称", key="model_name")
        st.caption("API Key、Base URL、模型名称为必填参数，请完整配置后再测试或调用模型。")
        st.checkbox("读取文本内容样本", key="sample_content", help="勾选后会读取常见文本文件的有限内容样本并发送给模型。")
        st.number_input("每个文本文件样本字节数", min_value=100, max_value=1000000, step=100, key="sample_bytes")
        test_enabled = bool(st.session_state.model_api_key and st.session_state.model_base_url and st.session_state.model_name)
        if st.button("测试模型", use_container_width=True, disabled=not test_enabled):
            try:
                with st.spinner("正在测试模型配置..."):
                    reply = test_model_config()
                st.success(f"模型测试成功：{reply}")
            except Exception as exc:
                st.error(f"模型测试失败：{exc}")


    scan_clicked = st.button("扫描分析", type="primary", use_container_width=True)

root = Path(st.session_state.path_text).expanduser().resolve()
excludes = get_excludes()

if scan_clicked:
    st.session_state.analysis_result = ""
    st.session_state.delete_recommendations = []
    if not root.exists():
        st.error(f"路径不存在：{root}")
    elif not root.is_dir():
        st.error(f"路径不是目录：{root}")
    else:
        with st.spinner("正在扫描目录..."):
            st.session_state.files = collect_file_info(root, excludes)
            st.session_state.root = root
            st.session_state.scan_source = "目录路径"
        st.success(f"扫描完成：{len(st.session_state.files)} 个文件。")

files = st.session_state.files
current_root = st.session_state.root

if not files:
    st.warning("还没有扫描结果。请先在左侧选择目录，然后点击\"扫描分析\"。")
    st.stop()

total_size = sum(file.size for file in files)
max_file = files[0] if files else None
display_files = get_display_files(files)
large_files_all = [file for file in files if file.size >= int(st.session_state.min_size_mb * 1024 * 1024)]
large_files = get_display_files(large_files_all)
selected_files = files[: st.session_state.top]
extension_rows = build_extension_rows(files)

selected_section = st.radio("结果视图", ["概览", "大文件", "文件类型", "AI 归类"], horizontal=True, key="active_section")

if selected_section == "概览":
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("文件总数", len(files))
    col2.metric("总大小", format_size(total_size))
    col3.metric("最大文件", format_size(max_file.size if max_file else 0))
    col4.metric("文件类型数", len(extension_rows))

    st.subheader("当前分析来源")
    st.write(st.session_state.scan_source)
    st.code(str(current_root), language="text")

    render_deletable_file_table("文件列表", display_files, total_size, current_root, excludes, "overview")

elif selected_section == "大文件":
    if st.session_state.min_size_mb == 0:
        st.caption(f"当前阈值为 0 MB，展示按大小排序后的 {len(large_files)} 个文件。")
    else:
        st.caption(f"展示大于等于 {st.session_state.min_size_mb:g} MB 的 {len(large_files)} 个文件。")
    render_deletable_file_table("大文件列表", large_files, total_size, current_root, excludes, "large")

elif selected_section == "文件类型":
    st.caption("按扩展名统计文件数量和空间占用。")
    st.dataframe(extension_rows, use_container_width=True, hide_index=True)

elif selected_section == "AI 归类":
    st.subheader("AI 归类分析")
    st.write(f"将分析按大小排序后的前 {len(selected_files)} 个文件。")

    if st.session_state.sample_content:
        st.warning(f"已开启文本内容样本：每个文本文件最多读取 {st.session_state.sample_bytes} 字节并发送给模型。")
    else:
        st.info("当前只发送文件路径、扩展名、大小和修改时间，不发送文件内容。")

    with st.expander("预览将发送给模型的文件清单", expanded=False):
        st.dataframe(build_file_rows(selected_files, total_size), use_container_width=True, hide_index=True)

    action_col, recommend_col = st.columns([2, 2])
    with action_col:
        analyze_clicked = st.button("使用模型归类分析", type="primary", use_container_width=True)
    with recommend_col:
        recommend_clicked = st.button("生成推荐删除文件清单", use_container_width=True)

    if analyze_clicked:
        apply_model_config()
        if not st.session_state.model_name:
            st.error("缺少模型名称，请在模型设置中输入模型名称。")
        else:
            try:
                with st.spinner("正在调用模型分析..."):
                    st.session_state.analysis_result = run_classification(
                        current_root,
                        selected_files,
                        st.session_state.model_name,
                        st.session_state.sample_content,
                        int(st.session_state.sample_bytes),
                    )
                    st.session_state.delete_recommendations = run_delete_recommendations(
                        current_root,
                        selected_files,
                        st.session_state.model_name,
                        st.session_state.sample_content,
                        int(st.session_state.sample_bytes),
                    )
            except Exception as exc:
                st.error(f"模型分析失败：{exc}")

    if recommend_clicked:
        apply_model_config()
        if not st.session_state.model_name:
            st.error("缺少模型名称，请在模型设置中输入模型名称。")
        else:
            try:
                with st.spinner("正在生成推荐删除文件清单..."):
                    st.session_state.delete_recommendations = run_delete_recommendations(
                        current_root,
                        selected_files,
                        st.session_state.model_name,
                        st.session_state.sample_content,
                        int(st.session_state.sample_bytes),
                    )
            except Exception as exc:
                st.error(f"生成推荐删除文件清单失败：{exc}")

    if st.session_state.analysis_result:
        with st.expander("AI 分析结果", expanded=True):
            st.markdown(st.session_state.analysis_result)
            html = build_analysis_html(st.session_state.analysis_result, current_root)
            st.download_button("下载 HTML 报告", data=html.encode("utf-8"), file_name="file-analysis-report.html", mime="text/html")
            st.caption("下载的是独立 HTML 报告文件，打开后点击打印 / 另存为 PDF 只会打印报告内容。")
            if st.button("清空分析结果", use_container_width=True):
                st.session_state.analysis_result = ""
                st.session_state.delete_recommendations = []
                st.rerun()

    render_recommended_delete_table(st.session_state.delete_recommendations, files, total_size, current_root, excludes)
