# 固定分词构建输入

此目录只为构建项目分词资源提供不可变输入，不安装或替换运行依赖。
源码构建及 sdist 均携带这些文件，因此声明的 setuptools 构建环境无需预装 jieba。
Lambda 构建仍显式使用锁定目标 wheel 中的同一原始词典。

`dict.txt` 和 `PKG-INFO` 原样取自本工作树已有、已锁定的 jieba 0.42.1 源分发物；
文件未改写、未重新编码：

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| 上游 `jieba-0.42.1.tar.gz` | 见既有目标构建锁 | `055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2` |
| `dict.txt` | 5071852 | `7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8` |
| `PKG-INFO` | 2551 | `c76b730d7e1b86c474d48d7333bec1a11c6ddd42bec57dbe0b7c0acda2053cd9` |
| `LICENSE` | 1075 | `18ba0984839f85853b29fadaf992f7dba8fd0ca0fbeae34de2b8735222dc7a37` |

上游许可为 MIT，作者及许可声明保留在未修改的 `PKG-INFO`。
`LICENSE` 原样取自上游 [v0.42.1 标签的许可证](https://github.com/fxsjy/jieba/blob/v0.42.1/LICENSE)，
通过同标签的[官方原始文件](https://raw.githubusercontent.com/fxsjy/jieba/v0.42.1/LICENSE)
下载并核对上述摘要，保留 `Copyright (c) 2013 Sun Junyi` 及完整 MIT 条款。
该许可证随 sdist 收录，并由固定 setuptools 的 `license-files` 元数据复制到
wheel 的 `.dist-info/licenses/build_inputs/jieba-0.42.1/LICENSE`。

构建脚本在解析前核对原始词典 SHA-256，并在写入前核对最终 JSON 的固定摘要。
修改此输入必须同时评估分词语义、资源摘要和许可证信息，不能仅替换文件。
