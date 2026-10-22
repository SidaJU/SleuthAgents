# ue-perf-sleuth

UE 项目多 Agent 分析工具。**当前阶段 (A)：Planner + CodeSage + AssetSage**。

## 架构

```
User Input (自然语言 / 文件名 / 模块名 / 符号名 / 资产路径 / 混合)
        │
        ▼
   ┌─────────────┐
   │   Planner   │  真 Agent：规则强制路由 + LLM 决策 + LLM 汇总
   └──────┬──────┘
          │ handoff (Command 语义)
    ┌─────┴─────┐
    ▼           ▼
┌─────────┐ ┌──────────┐
│ CodeSage│ │AssetSage │
│ 真 Agent│ │ 真 Agent │
└─────────┘ └──────────┘
 SQLite索引    UnrealMCP TCP
 ripgrep       (127.0.0.1:55557)
 read_file     UE 编辑器活数据
```

**"真 Agent" 定义**：每个 Agent 都有独立 LLM + 独立工具集 + 独立 ReAct 循环 + 独立 finalize 判定。

## Agent 能力

| Agent | 工具 | 数据源 | 用途 |
|-------|------|--------|------|
| Planner | classify / handoff / finalize | 无 | 意图识别、路由、汇总 |
| CodeSage | lookup_symbol / find_callers / read_file / class_hierarchy | SQLite 索引 + ripgrep | 源码问题（函数/类/调用/继承） |
| AssetSage | list_assets / get_asset_info / get_static_mesh_collision_info / get_available_materials / get_visible_static_mesh_actors / scan_actors_for_component | UnrealMCP TCP → UE 编辑器 | 资产运行时属性（Nanite/LOD/材质引用/关卡 Actor） |

## 强制路由关键词

- **CodeSage**：`用代码专家 / 查代码 / 看源码 / grep / ripgrep / 查符号 ...`
- **AssetSage**：`用资产专家 / 查资产 / 查编辑器 / 查蓝图 / 查 nanite / 查 lod / unrealmcp ...`
- **优先级**：资产关键词优先于代码关键词（因为更具体）

## 用法

```bash
# 前置：ollama serve（qwen2.5:7b）
# 可选：UE 编辑器开着 + UnrealMCP 插件运行（否则 AssetSage 会优雅降级）

cd G:\tools\ue-perf-sleuth

# 自动规划
python -m perf_sleuth.cli "为什么 BMP3 履带碎片一直发 occlusion query"

# 强制 CodeSage
python -m perf_sleuth.cli "用代码专家搜 SetHiddenInGame 在哪调用"

# 强制 AssetSage
python -m perf_sleuth.cli "查资产 SM_BMP3_Track 的 Nanite 设置"

# 通用输入
python -m perf_sleuth.cli "RDestruction 模块干什么的"
python -m perf_sleuth.cli "UPrimitiveComponent"
python -m perf_sleuth.cli "/Game/Meshes/SM_Foo"

# 想看内部工具调用轨迹
python -m perf_sleuth.cli --dump-trace out.json "..."
```

## 目录

```
ue-perf-sleuth/
├── perf_sleuth/
│   ├── cli.py                  # 命令行入口
│   ├── state.py                # 共享 state
│   ├── graph.py                # Planner ↔ CodeSage ↔ AssetSage 编排循环
│   ├── shared/
│   │   ├── llm.py              # LLM (Ollama chat_json，带 JSON 修复重试)
│   │   ├── explorer_bridge.py  # SQLite + ripgrep（Schema 自适应）
│   │   └── mcp_client.py       # UnrealMCP TCP 客户端
│   └── agents/
│       ├── planner.py          # Planner Agent（3 向路由）
│       ├── codesage.py         # CodeSage Agent（4 工具）
│       └── assetsage.py        # AssetSage Agent（6 工具）
├── tests/test_smoke.py         # 13/13 通过
├── config.toml
├── run.bat
└── README.md
```

## 环境依赖

- Python 3.11+（用了 stdlib 的 `tomllib`）
- `pip install requests`
- ripgrep（bridge 层会自动从 `G:\tools\ripgrep\...` 注入 PATH）
- Ollama + qwen2.5:7b
- UnrealMCP 插件在 UE 编辑器里运行（仅 AssetSage 需要）

## 复用清单

| 来源 | 用途 |
|------|------|
| `g:/tools/SourceCode_db/symbols_20260702_1913_e-Source_g-Source.db` | 197 万符号索引 |
| `g:/tools/ripgrep/.../rg.exe` | 代码搜索 |
| `ue-callchain-explorer/ue_explorer/index/callers.py` | ripgrep 封装 |
| `RaidGame/Plugins/UnrealMCP` | UE 编辑器 TCP 服务（31 个 Command Handler） |
