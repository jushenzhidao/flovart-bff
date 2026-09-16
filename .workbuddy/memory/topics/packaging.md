# packaging（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## 📦 打包交付口径（2026-09-15 锁定）
**本项目打包 = zip 压缩包，不是 Docker 镜像。** 部署形态：源码 + venv + uvicorn。
- **BFF 纯运行包**：`scripts/pack_runtime.py` → `dist/flovart-bff-<ver>-<date>.zip`
  含 `app/` + `docs/` + 10 个顶层文件（requirements/pyproject/.env.example/README/4 份设计文档）
  **不含** `.env`（管理员账密+SECRET_KEY）、`data/`、`tests/`、`scripts/`、`__pycache__`
- **前端 dist 包**：`scripts/pack_dist.py`（flovart-web 侧）→ `dist-pkg/flovart-web-dist-<ver>-<date>.zip`
  先 `vite build` 再打包；产物含 index.html + assets/，Nginx 直接指向即可，**无需 npm install**
- ⚠️ ARCHITECTURE.md 里 M3「Docker 多阶段构建」是**待办但非当前诉求**，勿自作主张上 Docker
- 本机无 `zip`/`7z` CLI → 统一用 Python `zipfile`（`ZIP_DEFLATED, compresslevel=9`）

### ⛔ 两个「平台」标记极易混（2026-09-15 展示瘦身）
| 标记 | 含义 | 注入点 | 展示点 |
|---|---|---|---|
| `platformSource='1'` | 平台共享服务（管理员发布**模型清单**） | `App.tsx` `sharedEntries` | ~~`SettingsPanel` 「平台服务」区块~~ **已删（勿恢复）** |
| `flovart_platform='1'` | 平台 Key 池（按用户签发凭据，**普通用户唯一可用入口**） | `App.tsx` L236-252 payload | ~~`PromptBar` 的 `label:'平台模型'`~~ **已过滤（勿恢复）** |

- **两条都已从 UI 移除（飞哥 2026-09-15 拍板）**：用户不需要看到「管理员配了什么」，
  也不需要看到硬编码的「平台模型」假条目。**但注入链路必须保留**——删展示 ≠ 删能力。
- ⚠️ 「用户能选到管理员发布的模型」**不依赖**被删的展示区块，靠 `platformFilteredModelOptions` + 产品模型目录那条独立链路。

