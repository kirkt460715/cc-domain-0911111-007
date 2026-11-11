# Corpus Gate 语料治理

处理授权来源的合成文本，提供规范化、去重簇、审查、注释、抽样和发布。重复簇表示相似性连通分量，不等于语义等价；隐私检测只是复核信号。

## 分层抽样调查

`survey.*` 操作把抽样设计一路带到估计报告，避免用抽中样本的不合格比例冒充整批质量。

- `survey.design` 在创建瞬间冻结总体：记录语料版本、每个层的总体量 `N_h`、入样概率 `n_h/N_h` 与设计权重 `N_h/n_h`。支持按来源（`source`）、风险（`risk`，由冻结文档的 findings 严重度推导）或来源加风险（`source_risk`）分层，分配方式支持按比例（最大余数法）、等额和显式分配。`unit_type=cluster` 时把去重簇作为整簇抽样单位，并快照簇成员的文档版本。
- 复核只接受样本冻结的文档版本（`survey.respond`）；重复提交相同结论是幂等的，结论冲突会被拒绝。撤回或换版的样本通过 `survey.noninterview` 记为缺访（`withdrawn`、`replaced`、`unreachable`），`survey.status` 也会自动识别已发生的撤回或换版，缺访和未完成复核都不会混入合格数。
- `survey.estimate` 用设计权重计算不合格率、不合格总量及相符标准误；整簇设计按簇间方差估计，并逐层给出有限总体修正因子 `1 - n_h/N_h`。无观测的层、只有一个抽样单位的层、未入样的层会在 `not_estimable` 中明确标注，不参与总体估计。
- `survey.compare` / `decompose_reports` 用 Kitagawa 分解把总体结构变化（各层占比变化）与层内质量变化（层内不合格率变化）分开；所有数字只来自冻结总体框和已存复核，因此随时可按原冻结总体复算。


## 运行环境

使用 Python 3.11，核心功能依赖标准库和 SQLite。演示和测试只使用本地合成数据，不需要账户、设备或外部运行服务。请从项目根目录执行命令。

## 检查与运行

构建检查验证语法，测试使用临时数据目录，不会连接远端服务。

```sh
python3 -m compileall -q corpus_gate
python3 -m unittest discover -s tests -v
python3 -m corpus_gate --db :memory: demo --fixture fixtures/demo.json
```

演示会展示产品的实际数据处理和输出，运行结束自行退出。查看现有接口：

```sh
python3 -m corpus_gate --db :memory: operations
```

## 目录与数据

corpus_gate/ 保存领域逻辑和请求入口，tests/ 包含正常行为、边界与恢复测试。例子中的标识和记录为合成资料，不包含真实人员或生产测量。业务目录和本地数据库由调用者指定，应与源码分开保存。

## 干净环境复现

容器定义固定运行时版本。构建时准备环境，运行时无需网络；每次启动会复制只读源码到临时工作目录，测试产生的文件不会写回仓库。

```sh
docker build -t local-product-check .
docker run --rm --network none local-product-check
```
