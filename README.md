# LogiMate · 供应链多 Agent 运营系统独立复现

基于明确标注的模拟数据，实现订单、物流、仓储、售后四域只读查询、异常诊断、证据追踪和持久审核。未取得原始基础工程，不冒称原项目历史贡献或生产部署。

实现 FastAPI、DeepSeek 单Agent、LangGraph 协调＋四专家、真实MCP stdio、CPU混合检索/Milvus适配、取消/降级/熔断、SQLite工单和原生interrupt/resume。

## 无需密钥的演示

Python 3.12，Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --require-hashes -r requirements-checkpoint.lock
.\.venv\Scripts\python.exe tools\demo_logimate.py
```

Linux/macOS解释器改为`.venv/bin/python`。演示真实HTTP、LangGraph和SQLite，依次创建/关闭三个API实例；审核员由脚本代演，不调用模型/MCP/嵌入或生产系统，不执行退款/改库存。结果写入Git忽略的`docs/evidence/`。

这是公开分发版的安装指令，历史验收基于已配置环境；全新机器从零安装尚未验证。

## 启动API

```powershell
.\.venv\Scripts\python.exe -m uvicorn logimate.api:app --host 127.0.0.1 --port 8000
```

默认演示令牌为`local-demo-token`，通过Bearer传递；非生产认证。可选真实模型只读取环境变量`DEEPSEEK_API_KEY`，缺密钥时关闭。不要提交密钥。

```powershell
docker compose up --build -d
docker compose logs api
docker compose down
```

Compose映射本机回环地址，读者/审核令牌分开，持久SQLite使用独立卷。公开发行Dockerfile/Compose经过整理，尚未单独完成镜像重建验收，不挪用历史镜像成绩；默认不启用检索/模型分析。

## 结构和测试

- `logimate/`：应用、图、协议、检索、可靠性、审核及评测。
- `data/`、`prompts/`、`eval/`：模拟数据、提示词和评测/发布契约。
- `tests/`：规则、权限、协议、持久化和评测测试。
- `tools/`：适配本仓库目录的无密钥演示与独立审计。
- `scripts/`：原实验工具，部分历史验收依赖未公开的本机回执/镜像。

完整测试还需要检索依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-retrieval.lock
.\.venv\Scripts\python.exe -m pytest -q
```

依赖私有历史回执的测试明确跳过，不伪造记录。因此不直接沿用原工程测试总数。公开版验证结果见[验收摘要](docs/VALIDATION.md)。显式`--live`脚本可能产生费用，日常演示不需要执行。

## 边界

[架构说明](docs/ARCHITECTURE.md)。三方案在已使用的16题/8家族上各16/16，不是外部盲测或生产95%准确率；未见复杂方案正确率收益。CPU C8突发72次仅12次完整检索，不能宣称高并发目标达标。

未完成：并发工作池/队列、独立Milvus/付费整图负载、生产认证/真实系统集成、Redis/MySQL、MinIO/S3和大规模HNSW。没有真实运营工时对照，不宣称材料业务收益已复现。

原PDF、个人信息、开发复盘、排障日志、详细历史回执、模型缓存和本机配置均不公开。历史manifest保留来源hash，不保证私有历史资料可下载；题集已使用并公开，不得称后续执行为新盲测。
