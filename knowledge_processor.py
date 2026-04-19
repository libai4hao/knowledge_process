#!/usr/bin/env python3
"""Knowledge notes normalization tool.

Two standalone functions:
- Format conversion: local `.html/.htm/.pdf` -> local `.md`
- Ad cleaning: markdown cleaning with configurable feature library

Both can be used independently or chained as a pipeline.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import logging
import re
import shutil
import posixpath
import sys
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, List
from urllib.parse import urlparse, unquote

from tools.image import AssetManager

DEFAULT_FEATURE_LIBRARY = {
    "keywords": [
        "广告",
        "推广",
        "赞助",
        "推荐阅读",
        "你可能喜欢",
        "热门文章",
        "猜你喜欢",
        "点击领取",
        "关注公众号",
        "领取资料",
        "福利",
        "知识星球",
        "回复666",
        "扫码",
        "长按二维码",
        "专属福利",
    ],
    "domains": [
        "amazon.",
        "taobao.",
        "jd.",
        "tmall.",
        "alibaba.",
        "aliexpress.",
        "ebay.",
        "mp.weixin.qq.com",
        "s.click.",
        "union-click.",
        "fanli.",
        "promo.",
    ],
    "cta_patterns": [
        r"点击.*?(领取|购买|查看|获取|下载)",
        r"回复.*?(领取|获取|进群|666|面试题)",
        r"立即(领取|购买|查看|抢购)",
        r"长按.*?二维码",
        r"关注.*?(公众号|账号)",
    ],
    "line_patterns": [
        r"^\s*在看点这里.*$",
        r"^\s*点击上方.*?(设为星标|关注).*$",
        r"^\s*作者[:：].*$",
        r"^\s*源码精品专栏\s*$",
        r"^\s*获取方式[:：].*$",
    ],
    "multiline_patterns": [
        r"<!--\s*ad\s*-->[\s\S]*?<!--\s*/ad\s*-->",
    ],
}

HTML_AD_BLOCK_RE = re.compile(r"<!--\s*ad\s*-->[\s\S]*?<!--\s*/ad\s*-->", re.IGNORECASE)
SEPARATOR_RE = re.compile(r"^-{10,}\s*$")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 非贪婪匹配
MD_LINK_RE = re.compile(r"!?\[([^\]]*?)\]\(([^)]+?)\)")


def _parse_simple_yaml_feature_library(raw_text: str) -> dict:
    """Parse a minimal YAML subset for feature library fallback."""
    data: dict[str, dict[str, list[str]]] = {"patterns": {}}
    current_key: str | None = None
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":") and not line.startswith("-"):
            key = line[:-1].strip()
            if key in {"patterns", "keywords", "link_domains", "regex_blocks"}:
                if key != "patterns":
                    data["patterns"].setdefault(key, [])
                    current_key = key
                else:
                    current_key = None
            continue
        if line.startswith("- ") and current_key is not None:
            value = line[2:].strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            data["patterns"][current_key].append(value)
    return data


@dataclass
class ProcessStats:
    scanned_files: int = 0
    converted_files: int = 0
    cleaned_files: int = 0
    skipped_files: int = 0

    def merge(self, other: "ProcessStats"):
        """补全：合并统计数据"""
        self.scanned_files += other.scanned_files
        self.converted_files += other.converted_files
        self.cleaned_files += other.cleaned_files
        self.skipped_files += other.skipped_files


@dataclass
class FeatureLibrary:
    keywords: tuple[str, ...]
    domains: tuple[str, ...]
    cta_patterns: tuple[str, ...]
    line_patterns: tuple[str, ...]
    multiline_patterns: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "FeatureLibrary":
        return cls(
            keywords=tuple(data.get("keywords", [])),
            domains=tuple(data.get("domains", [])),
            cta_patterns=tuple(data.get("cta_patterns", [])),
            line_patterns=tuple(data.get("line_patterns", [])),
            multiline_patterns=tuple(data.get("multiline_patterns", [])),
        )

    @classmethod
    def from_file(cls, file_path: Path | None) -> "FeatureLibrary":
        if file_path is None:
            return cls.from_dict(DEFAULT_FEATURE_LIBRARY)
        raw_text = file_path.read_text(encoding="utf-8")
        suffix = file_path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except Exception as exc:
                logging.getLogger("knowledge_processor").warning(
                    "未安装 pyyaml，使用内置简化 YAML 解析器: %s", exc
                )
                raw = _parse_simple_yaml_feature_library(raw_text)
            else:
                raw = yaml.safe_load(raw_text) or {}
        else:
            raw = json.loads(raw_text)
        merged = DEFAULT_FEATURE_LIBRARY.copy()
        if "patterns" in raw:
            # 合并配置
            for key in merged:
                if key in raw and isinstance(raw[key], list):
                    merged[key] = list(set(merged[key] + raw[key]))
            return cls(
                keywords=tuple(merged["keywords"]),
                domains=tuple(merged["domains"]),
                cta_patterns=tuple(merged["cta_patterns"]),
                line_patterns=tuple(merged["line_patterns"]),
                multiline_patterns=tuple(merged["multiline_patterns"]),
            )
        return cls.from_dict(merged)


@dataclass
class CompiledFeatureLibrary:
    feature_library: FeatureLibrary
    multiline_patterns: tuple[re.Pattern[str], ...]
    line_patterns: tuple[re.Pattern[str], ...]
    cta_patterns: tuple[re.Pattern[str], ...]

    @classmethod
    def from_feature_library(cls, feature_library: FeatureLibrary) -> "CompiledFeatureLibrary":
        return cls(
            feature_library=feature_library,
            multiline_patterns=tuple(
                re.compile(pattern, re.IGNORECASE | re.DOTALL)
                for pattern in feature_library.multiline_patterns
            ),
            line_patterns=tuple(
                re.compile(pattern, re.IGNORECASE) for pattern in feature_library.line_patterns
            ),
            cta_patterns=tuple(
                re.compile(pattern, re.IGNORECASE) for pattern in feature_library.cta_patterns
            ),
        )

# --- HTML/PDF 转换逻辑 ---

class SimpleHTMLToMarkdown(HTMLParser):
    """A small HTML -> markdown converter based on stdlib parser."""

    def __init__(self, asset_mgr: AssetManager, image_rewriter: Callable[[str], str] | None = None) -> None:
        super().__init__()
        self.asset_mgr = asset_mgr
        self.parts: List[str] = []
        self.current_link: str | None = None
        self.current_link_text: List[str] = []
        self.in_pre = False
        self.in_code = False
        self.list_stack: List[str] = []
        self.image_rewriter = image_rewriter

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"h1", "h2", "h3", "h4"}:
            self.parts.append(f"\n{'#' * int(tag[1])} ")
        elif tag == "blockquote":
            self.parts.append("\n\n> ")
        elif tag == "p":
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "ul":
            self.list_stack.append("ul")
        elif tag == "ol":
            self.list_stack.append("ol")
        elif tag == "li":
            if self.list_stack and self.list_stack[-1] == "ol":
                self.parts.append("\n1. ")
            else:
                self.parts.append("\n- ")
        elif tag == "a":
            self.current_link = attrs_dict.get("href") or ""
            self.current_link_text = []
        elif tag == "img":
            src = unquote(attrs_dict.get("src", "")).replace("\\", "/")
            alt = attrs_dict.get("alt") or "image"
            if self.image_rewriter is not None:
                src = self.image_rewriter(src)
            src = src.replace("\\", "/")
            if src and "://" not in src and not src.startswith("/"):
                src = posixpath.normpath(src)
            self.parts.append(f"![{alt}]({src})")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag in {"del", "s"}:
            self.parts.append("~~")
        elif tag == "pre":
            self.in_pre = True
            self.parts.append("\n\n```\n")
        elif tag == "code":
            if not self.in_pre:
                self.parts.append("`")
            self.in_code = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"ul", "ol"} and self.list_stack:
            self.list_stack.pop()
        elif tag == "a" and self.current_link is not None:
            text = "".join(self.current_link_text).strip() or self.current_link
            self.parts.append(f"[{text}]({self.current_link})")
            self.current_link = None
            self.current_link_text = []
        elif tag == "pre":
            self.in_pre = False
            self.parts.append("\n```\n")
        elif tag == "code":
            if not self.in_pre:
                self.parts.append("`")
            self.in_code = False
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag in {"del", "s"}:
            self.parts.append("~~")

    def handle_data(self, data: str) -> None:
        text = unescape(data)
        if not text.strip():
            return
        if self.current_link is not None:
            self.current_link_text.append(text)
        else:
            self.parts.append(text)

    def markdown(self) -> str:
        content = "".join(self.parts)
        content = re.sub(r"\n{3,}", "\n\n", content).strip()
        return content + "\n" if content else ""

def get_target_path(src_path: Path, input_dir: Path, output_dir: Path | None) -> Path:
    """
    计算目标输出路径，确保路径不会无限叠加

    src_path: 源文件路径
    input_dir: 输入目录
    output_dir: 输出目录
    return: 目标输出路径
    """
    if output_dir is None:
        return src_path.with_suffix(".md")

    # 获取相对路径 (例如: src 是 input/subdir/a.html -> 得到 subdir/a.html)
    try:
        relative_path = src_path.relative_to(input_dir)
    except ValueError:
        # 如果 src_path 不在 input_dir 内（虽然不该发生），回退到文件名
        relative_path = Path(src_path.name)

    target_path = output_dir / relative_path
    return target_path.with_suffix(".md")

def convert_html_to_markdown(
    html_content: str, image_rewriter: Callable[[str], str] | None = None
) -> str:
    parser = SimpleHTMLToMarkdown(image_rewriter=image_rewriter)
    parser.feed(html_content)
    return parser.markdown()


def _is_remote_or_data_url(url: str) -> bool:
    return any(url.startswith(s) for s in ["http", "https", "data:"])


def _strip_query_and_fragment(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme not in {"file"}:
        return value
    return parsed.path


def build_html_image_rewriter(
    html_path: Path,
    target_md: Path,
    dry_run: bool,
    asset_mgr: AssetManager,
    logger: logging.Logger,
) -> Callable[[str], str]:
    '''
    构建HTML图片重写器
    html_path: HTML文件路径
    target_md: 目标Markdown文件路径
    dry_run: 是否仅输出将发生的变更，不实际写入
    logger: 日志记录器
    return: 图片重写器
    '''
    copied_map: dict[Path, str] = {}
    # 附件文件夹：始终与目标 MD 文件同级，命名为：文件所在目录/assets
    assets_dir = target_md.parent / "assets"

    def rewrite(src: str) -> str:
        '''
        重写图片路径，如果不是本地图片则原样返回
        :param src:
        :return:
        '''
        if not src or _is_remote_or_data_url(src):
            return src
        # 1. 找到原始图片的物理路径，并检查是否存在
        normalized = _strip_query_and_fragment(src).replace("\\", "/")
        src_path = Path(normalized)
        if not src_path.is_absolute():
            # 这里的 html_path 是原始 HTML 的位置
            src_path = (html_path.parent / src_path).resolve()

        if src_path in copied_map:
            return copied_map[src_path]

        if not src_path.exists() or not src_path.is_file():
            logger.warning("图片附件不存在，保留原路径: %s (from %s)", src, html_path)
            return normalized

        target_name = src_path.name
        destination = assets_dir / target_name

        # TODO 默认使用hash逻辑
        model = 'hash'
        if model == 'add_num':
            '''
            追加序号模式：如果目标目录同名文件已存在则追加序号
            '''
            counter = 1
            while destination.exists() and destination.resolve() != src_path.resolve():
                destination = assets_dir / f"{src_path.stem}_{counter}{src_path.suffix}"
                counter += 1
            # 3. 计算 Markdown 中的相对引用路径
            # 结果类似于 "文件所在目录/assets/image.png"
            relative_ref = posixpath.join(assets_dir.name, destination.name)
        elif model == 'hash':
            '''
            hash模式：如果该 Hash 已存在，直接复用，否则，复制文件。如果同名但内容不同则追加序号
            '''
            relative_ref = asset_mgr.migrate_file(src_path)
        copied_map[src_path] = relative_ref
        if dry_run:
            return relative_ref

        assets_dir.mkdir(parents=True, exist_ok=True)

        try:
            if not destination.exists():
                shutil.copy2(src_path, destination)

                logger.info("复制图片附件: %s -> %s", src_path, destination)
        except Exception as e:
            logger.error("迁移附件失败: %s, 错误: %s", src_path, e)
        return relative_ref

    return rewrite


def _safe_print(message: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    text = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(text)


def convert_pdf_to_markdown(pdf_path: Path, asset_mgr: AssetManager) -> str:
    """
    提取 PDF 文本并尝试导出嵌入图片

    pdf_path: PDF文件路径
    asset_mgr: 图片管理器
    return: 转换后的Markdown文本

    """
    try:
        from pypdf import PdfReader  # type: ignore
        # 核心修改：静默 pypdf 内部的日志输出
        logging.getLogger("pypdf").setLevel(logging.ERROR)

        reader = PdfReader(str(pdf_path))
        pages_content = []
        img_count = 0

        for i, page in enumerate(reader.pages):
            # 1. 提取文本
            text = page.extract_text() or ""
            pages_content.append(text)

            # 2. 提取图片 (pypdf 3.0+)
            for img_file in page.images:
                img_count += 1
                # 将内存图片临时写入，通过 AssetManager 统一哈希管理
                temp_name = f"pdf_extract_{pdf_path.stem}_p{i}_{img_count}_{img_file.name}"
                with open(temp_name, "wb") as f:
                    f.write(img_file.data)

                p_temp = Path(temp_name)
                rel_path = asset_mgr.migrate_file(p_temp)
                pages_content.append(f"\n![pdf_img_{img_count}]({rel_path})\n")
                p_temp.unlink()  # 删除临时文件

        return "\n\n".join(pages_content)
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("缺少 pypdf 依赖，请执行: pip install pypdf") from exc


def _line_contains_ad_link(line: str, feature_library: FeatureLibrary) -> bool:
    urls = URL_RE.findall(line)
    for url in urls:
        if any(domain in url.lower() for domain in feature_library.domains):
            return True
    link_match = MD_LINK_RE.search(line)
    if link_match:
        target = link_match.group(1).lower()
        return any(domain in target for domain in feature_library.domains)
    return False


def _is_ad_line(
    line: str,
    compiled_feature_library: CompiledFeatureLibrary,
) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if SEPARATOR_RE.match(stripped):
        return True
    if "<!-- ad -->" in stripped.lower() or "<!--ad-->" in stripped.lower():
        return True
    if any(pattern.search(stripped) for pattern in compiled_feature_library.line_patterns):
        return True
    if _line_contains_ad_link(stripped, compiled_feature_library.feature_library):
        return True
    # 关键词 + 诱导行动 或 短行判定
    has_keyword = any(k in stripped for k in compiled_feature_library.feature_library.keywords)
    has_cta = any(p.search(stripped) for p in compiled_feature_library.cta_patterns)
    return has_keyword and (has_cta or len(stripped) < 80)


def _fingerprint_block(block: str) -> str:
    normalized = re.sub(r"\s+", " ", block).strip().lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _remove_duplicate_blocks_with_fingerprint(markdown_text: str) -> str:
    blocks = markdown_text.split("\n\n")
    seen = set()
    result = []
    for block in blocks:
        normalized = re.sub(r"\s+", " ", block).strip()
        if len(normalized) < 20:
            result.append(block)
            continue
        fingerprint = _fingerprint_block(block)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(block)
    content = "\n\n".join(result)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return content + "\n" if content else ""


def clean_markdown_ads(
    markdown_text: str,
    feature_library: FeatureLibrary | None = None,
    compiled_feature_library: CompiledFeatureLibrary | None = None,
) -> str:
    if compiled_feature_library is None:
        feature_library = feature_library or FeatureLibrary.from_dict(DEFAULT_FEATURE_LIBRARY)
        compiled_feature_library = CompiledFeatureLibrary.from_feature_library(feature_library)

    content = markdown_text
    for pattern in compiled_feature_library.multiline_patterns:
        content = pattern.sub("", content)
    content = HTML_AD_BLOCK_RE.sub("", content)

    lines = content.splitlines()
    cleaned_lines: List[str] = []
    in_code_block = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            cleaned_lines.append(line)
            continue

        if in_code_block:
            cleaned_lines.append(line)
            continue

        if _is_ad_line(line, compiled_feature_library):
            continue
        cleaned_lines.append(line)

    content = "\n".join(cleaned_lines)
    content = _remove_duplicate_blocks_with_fingerprint(content)
    return re.sub(r"\n{3,}", "\n\n", content).strip() + "\n"


def _iter_convert_files(base_dir: Path, recursive: bool = True) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in base_dir.glob(pattern):
        if path.is_file() and path.suffix.lower() in {".html", ".htm", ".pdf"}:
            yield path


def _iter_markdown_files(base_dir: Path, recursive: bool = True) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in base_dir.glob(pattern):
        if path.is_file() and path.suffix.lower() in {".md", ".markdown"}:
            yield path

# --- 核心处理器 ---

def convert_directory(
    directory: Path,
    recursive: bool = True,
    output_dir: Path | None = None,
    backup: bool = False,
    dry_run: bool = False,
) -> ProcessStats:
    '''
    转换目录下，所有pdf、html、htm文件，并保存到输出目录下（如果输出目录为空，则保存到当前目录）
    directory: 输入目录
    recursive: 是否递归
    output_dir: 输出目录,如果为空，则保存到当前目录
    backup: 是否备份
    dry_run: 是否仅输出将发生的变更，不实际写入
    return: 统计信息
    '''

    stats = ProcessStats()
    # 统一管理输出目录下的 assets
    target_base = output_dir if output_dir else directory
    asset_mgr = AssetManager(target_base / "assets", dry_run)
    logger = logging.getLogger("knowledge_processor")
    # 如果有输出目录，确保它存在
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    for src_path in _iter_convert_files(directory, recursive=recursive):
        stats.scanned_files += 1
        suffix = src_path.suffix.lower()
        #TODO  target_md = src_path.with_suffix(".md")

        target_md = get_target_path(src_path, directory, output_dir)
        logger.info("开始转换文件: %s", src_path)

        raw_markdown:str = None
        try:
            if suffix in {".html", ".htm"}:
                html_content = src_path.read_text(encoding="utf-8", errors="ignore")
                image_rewriter = build_html_image_rewriter(
                    html_path=src_path, target_md=target_md, dry_run=dry_run, asset_mgr=asset_mgr, logger=logger
                )
                raw_markdown = convert_html_to_markdown(html_content, image_rewriter=image_rewriter)
            else:
                # pdf转换
                raw_markdown = convert_pdf_to_markdown(src_path, asset_mgr)
            stats.converted_files += 1
        except RuntimeError:
            stats.skipped_files += 1
            logger.error(f"转换失败（依赖问题）: {src_path}")
            continue
        except Exception as e:
            stats.skipped_files += 1
            logging.error(f"转换处理失败 {src_path}: {e}")
            continue

        if dry_run:
            current = target_md.read_text(encoding="utf-8") if target_md.exists() else ""
            if raw_markdown != current:
                stats.cleaned_files += 1
                _safe_print(f"[convert][dry-run] 将写入: {target_md}")
            continue

        if backup and target_md.exists():
            backup_path = target_md.with_suffix(target_md.suffix + ".bak")
            shutil.copy2(target_md, backup_path)

        current = target_md.read_text(encoding="utf-8") if target_md.exists() else ""
        if raw_markdown != current:
            target_md.write_text(raw_markdown, encoding="utf-8")
            stats.cleaned_files += 1
            logger.info("写入转换结果: %s", target_md)
            _safe_print(f"[convert] 已写入: {target_md}")
        else:
            _safe_print(f"[convert] 无变化: {target_md}")

    return stats

def migrate_markdown_assets(content: str, old_md_path: Path, new_md_path: Path):
    """在纯清洗模式下，寻找并迁移 MD 里的本地图片"""

    def replace_asset(match):
        alt_text = match.group(1)
        src = match.group(2)

        if _is_remote_or_data_url(src):
            return match.group(0)

        old_asset_path = (old_md_path.parent / src).resolve()
        if old_asset_path.exists() and old_asset_path.is_file():
            new_assets_dir = new_md_path.parent / f"{new_md_path.stem}_assets"
            new_assets_dir.mkdir(parents=True, exist_ok=True)

            new_asset_path = new_assets_dir / old_asset_path.name
            shutil.copy2(old_asset_path, new_asset_path)

            # 返回相对于新 MD 的新路径
            return f"![{alt_text}]({new_assets_dir.name}/{new_asset_path.name})"
        return match.group(0)

    # 匹配 ![alt](src)
    return re.sub(r"!\[(.*?)\]\((.*?)\)", replace_asset, content)

def clean_directory(
    directory: Path,
    recursive: bool = True,
    backup: bool = False,
    output_dir: Path | None = None,
    dry_run: bool = False,
    feature_library_file: Path | None = None,
) -> ProcessStats:
    '''
    清洗目录下，所有markdown文件，并保存到输出目录下（如果输出目录为空，则保存到当前目录）
    注意：如果是 pipeline，清洗的目录应该是 output_dir
    directory: 输入目录
    recursive: 是否递归
    output_dir: 输出目录,如果为空，则保存到当前目录
    backup: 是否备份,当清洗结果与原文件不同时，才备份
    dry_run: 是否仅输出将发生的变更，不实际写入
    feature_library_file: 广告特征库路径，支持 JSON/YAML
    return: 统计信息
    '''
    stats = ProcessStats()
    logger = logging.getLogger("knowledge_processor")
    # 如果有输出目录，确保它存在
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    feature_library = FeatureLibrary.from_file(feature_library_file)
    compiled_feature_library = CompiledFeatureLibrary.from_feature_library(feature_library)
    for md_path in _iter_markdown_files(directory, recursive=recursive):
        stats.scanned_files += 1
        try:
            original = md_path.read_text(encoding="utf-8")
        except Exception:
            stats.skipped_files += 1
            logger.exception("读取 Markdown 失败: %s", md_path)
            continue

        cleaned = clean_markdown_ads(original, compiled_feature_library=compiled_feature_library)
        if dry_run:
            if cleaned != original:
                stats.cleaned_files += 1
                _safe_print(f"[clean][dry-run] 将清理: {md_path}")
            continue

        if cleaned == original:
            _safe_print(f"[clean] 无变化: {md_path}")
            continue

        if cleaned != original and backup:
            backup_path = md_path.with_suffix(md_path.suffix + ".bak")
            shutil.copy2(md_path, backup_path)
        if output_dir:
            # TODO 净化后的输出目录
            target_path = md_path
            # 如果有输出目录，确保它存在
            target_path.mkdir(parents=True, exist_ok=True)
            md_path = target_path
        md_path.write_text(cleaned, encoding="utf-8")
        stats.cleaned_files += 1
        # 打印文件名和输出文件路径
        logger.info(f"[clean] 已清理: {md_path.name}, 写入净化结果: {md_path}")

    return stats


def process_directory(
    directory: Path,
    recursive: bool = True,
    backup: bool = False,
    dry_run: bool = False,
    feature_library_file: Path | None = None,
) -> ProcessStats:
    '''
    串联处理目录下，所有pdf、html、htm文件，并保存到输出目录下（如果输出目录为空，则保存到当前目录）
    directory: 输入目录
    recursive: 是否递归
    backup: 是否备份
    dry_run: 是否仅输出将发生的变更，不实际写入
    feature_library_file: 广告特征库路径，支持 JSON/YAML
    return: 统计信息
    '''
    convert_stats = convert_directory(
        directory=directory, recursive=recursive, backup=backup, dry_run=dry_run
    )
    clean_stats = clean_directory(
        directory=directory,
        recursive=recursive,
        backup=backup,
        dry_run=dry_run,
        feature_library_file=feature_library_file,
    )
    return ProcessStats(
        scanned_files=convert_stats.scanned_files + clean_stats.scanned_files,
        converted_files=convert_stats.converted_files,
        cleaned_files=convert_stats.cleaned_files + clean_stats.cleaned_files,
        skipped_files=convert_stats.skipped_files + clean_stats.skipped_files,
    )


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="笔记工具（格式转换/广告净化/串联处理）")
    parser.add_argument("directory", help="待处理目录")
    parser.add_argument(
        "--mode",
        choices=("convert", "clean", "pipeline"),
        default="pipeline",
        help="convert=仅格式转换, clean=仅广告净化, pipeline=先转再净化",
    )
    parser.add_argument("--workers", type=int, default=4, help="并行线程数")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="仅处理当前目录")
    parser.add_argument("--output", "-o", type=str, help="指定生成结果的输出目录，如果为空，则保存到当前目录")
    parser.add_argument("--backup", action="store_true", help="写入前备份已有文件")
    parser.add_argument("--dry-run", action="store_true", help="仅输出将发生的变更，不实际写入")
    parser.add_argument(
        "--feature-library",
        type=str,
        default=None,
        help="广告特征库路径，支持 JSON/YAML",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="logs/knowledge_processor.log",
        help="日志基础文件名（会自动追加时间戳）",
    )
    parser.set_defaults(recursive=True)
    return parser


def _build_timestamped_log_path(log_file: str) -> Path:
    base = Path(log_file)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if base.suffix:
        filename = f"{base.stem}_{timestamp}{base.suffix}"
    else:
        filename = f"{base.name}_{timestamp}.log"
    return base.with_name(filename)


def setup_logging(log_file: str) -> Path:
    log_path = _build_timestamped_log_path(log_file)
    if log_path.parent != Path("."):
        log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("knowledge_processor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return log_path


# --- Main & Execution ---

# --- CLI 入口 ---
def main() -> None:
    parser = build_cli_parser()
    args = parser.parse_args()

    directory = Path(args.directory)
    output_dir = Path(args.output_dir)
    if not directory.exists() or not directory.is_dir():
        raise SystemExit(f"目录不存在: {directory}")

    log_path = setup_logging(args.log_file)
    logger = logging.getLogger("knowledge_processor")
    logger.info("任务开始 mode=%s directory=%s dry_run=%s", args.mode, directory, args.dry_run)
    _safe_print(f"日志文件: {log_path}")

    feature_library_path = Path(args.feature_library) if args.feature_library else None
    stats = ProcessStats()
    if args.mode in {"convert", "pipeline"}:
        convert_stats = convert_directory(
            directory=directory,
            recursive=args.recursive,
            backup=args.backup,
            dry_run=args.dry_run,
            output_dir=output_dir
        )
        stats.merge(convert_stats)

    if args.mode in {"clean", "pipeline"}:
        # 注意：如果是 pipeline，清洗的目录应该是 output_dir
        clean_stats = clean_directory(
            directory=args.mode == "pipeline" and output_dir or directory,
            recursive=args.recursive,
            backup=args.backup,
            dry_run=args.dry_run,
            output_dir= output_dir,
            feature_library_file=feature_library_path,
        )
        stats.merge(clean_stats)
    # else:
    #     stats = process_directory(
    #         directory=directory,
    #         recursive=args.recursive,
    #         backup=args.backup,
    #         dry_run=args.dry_run,
    #         feature_library_file=feature_library_path,
    #     )

    _safe_print(
        f"扫描: {stats.scanned_files}, 转换: {stats.converted_files}, "
        f"净化写入: {stats.cleaned_files}, 跳过: {stats.skipped_files}"
    )
    logger.info(
        "任务结束 scanned=%s converted=%s cleaned=%s skipped=%s",
        stats.scanned_files,
        stats.converted_files,
        stats.cleaned_files,
        stats.skipped_files,
    )


if __name__ == "__main__":
    main()
