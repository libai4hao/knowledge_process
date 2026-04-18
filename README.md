# knowledge_process

批量笔记处理工具，支持：

- 格式统一：将本地 `html/htm/pdf` 转换为本地 `md`
- 内容净化：基于特征库清理广告内容并做指纹去重
- 流水线：先转换再净化

## 环境要求

- Python 3.9+
- Windows / Linux / macOS

可选依赖：

- `pypdf`：用于 PDF 转 Markdown
- `pyyaml`：用于完整 YAML 特征库解析（未安装时会使用内置简化 YAML 解析器）

安装示例：

```bash
pip install pypdf pyyaml
```

## 快速开始

在项目根目录运行：

```bash
python knowledge_processor.py <目标目录> --mode pipeline
```

常见模式：

- `--mode convert`：仅做格式统一
- `--mode clean`：仅做广告净化
- `--mode outopt`：输出目录，未配置则在原文件目录生成
- `--mode pipeline`：先转换后净化（默认）

其他常用参数：

- `--feature-library <path>`：指定广告特征库（JSON/YAML）
- `--log-file <path>`：日志基础文件名，实际会自动追加时间戳
- `--dry-run`：预览变更，不落盘
- `--backup`：写入前备份（`.bak`）
- `--no-recursive`：仅处理当前目录

示例：

```bash
python knowledge_processor.py tests/test_data --mode clean --feature-library ad_feature_library.yaml --log-file logs/process.log
```

## 日志说明

运行后会生成带时间戳的日志文件，例如：

- `logs/process_20260416_043033.log`

控制台会输出：

- 日志文件路径
- 每个文件的处理结果（已写入/已清理/无变化/干跑将处理）
- 汇总统计（扫描、转换、净化写入、跳过）

## 特征库格式

### 1) 推荐格式（`patterns`）

```yaml
patterns:
  keywords:
    - "长按下方二维码"
    - "关注公众号并回复"
  link_domains:
    - "mp.weixin.qq.com/s"
    - "doc.iocoder.cn"
  regex_blocks:
    - "最近有一些小伙伴.*?欢迎下载！"
```

映射规则：

- `patterns.keywords` -> 关键词/行匹配
- `patterns.link_domains` -> 链接域名黑名单
- `patterns.regex_blocks` -> 多行块匹配（`re.S`）

### 2) 直接字段格式

也支持：

- `keywords`
- `domains`
- `cta_patterns`
- `line_patterns`
- `multiline_patterns`

## 样式与附件处理

HTML 转 Markdown 时支持：

- 标题、段落、列表、引用
- 粗体/斜体/删除线
- 代码块与行内代码
- 链接和图片

图片附件策略：

- 本地图片会复制到 `<目标md文件名>_assets/`
- Markdown 中图片链接会重写为相对路径
- 远程链接（`http/https`）和 `data:` 图片不改写

## 测试

运行全部测试：

```bash
python -m unittest discover -s tests -p "test_*.py"
```

当前测试覆盖：

- 广告净化与去重
- HTML 转 Markdown 结构与样式
- 图片附件拷贝与路径重写
- JSON/YAML 特征库
- CLI 日志输出
- `tests/test_data` 全量 Markdown 幂等性
