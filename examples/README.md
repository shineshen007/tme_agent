# 合成示例

`synthetic_expression.csv` 为 4 个虚构基因 × 4 个虚构样本，`synthetic_samples.csv` 为 4 行样本信息。数值仅用于检查输入和输出，不来自真实样本，不用于生物学推断。

这是固定示例生成器在 seed=2024 下生成的首组整数（脚本见 `scripts/01_generate_synthetic_example.py`）。安装器首次运行会把它们复制到本地 `data/raw/examples/` 和 `data/metadata/examples/`，已有文件保持原样。可以输入：“检查这两个示例的样本 ID 一致性，输出维度和缺失值表，不做组间推断”。自然语言任务需要自行配置模型；自动验收使用确定性模拟模型，不调用真实 API。
