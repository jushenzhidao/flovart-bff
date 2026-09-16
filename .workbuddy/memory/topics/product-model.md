# product-model（细节卷 · 从 MEMORY.md 拆出 2026-09-16）

> MEMORY.md 只留铁律摘要，血案全过程/完整表格见本文件。

## 🔴🔴 产品模型解析口径铁律（已栽四次）
前端「产品模型」（用户能选的创作模型）有**两套解析函数**，用错必出 bug：

| 函数 | 语义 | 未登记模型 |
|---|---|---|
| `getProductModel(v)` | **只查登记目录** `PRODUCT_MODEL_CATALOG` | 返回 `undefined` |
| `resolveDisplayableProductModel(v)` | 登记走精确定义；未登记按能力**合成** `flovart:custom:<name>`；**幂等** | ✅ 返回合成定义 |

- ⛔ 凡「要拿到定义去干活」（执行/路由/参数/闸门/展示/选项）**一律用 `resolveDisplayableProductModel`**
- ⛔ `getProductModel` **只允许**出现在「确实需判定是否登记」的场景（如 `isRegisteredProductModel`）
- ⛔ **幂等铁律**：传入**已是** `flovart:custom:<name>` 的值时**必须原样解析回同一份定义，绝不能二次合成**（曾产出套娃 `flovart:custom:flovart:custom:X` → 所有「先解析拿 id 再查」的调用点全失配）。**已修**：首行 `if (value.startsWith('flovart:custom:')) return resolveProductDefinition(value);`
- ⛔ **自查口诀**：「能选中/能看到，但一用就报错或没反应」→ **第一件事比对两侧是不是同一个解析函数**

### ⭐ 准入原则（飞哥 2026-09-15 拍板，最高优先级，勿再加白名单）
> 「不应该限制产品目录啊，加入用户自己配置的模型，也不能用吗？**直连的不需要经过白名单的，只要配置的能通就可以使用**。」

- **产品目录只用于给「登记模型」提供精确渲染参数，绝不作为「能不能用」的准入闸门**
- 准入唯一依据 = **能推断创作能力（image/video）+ key 暴露该路由（能连通）**。三类来源一视同仁：① 用户 BYOK ② 管理员发布的平台服务 ③ 登记模型
- **已全量放开 12 处**（均 `getProductModel`→`resolveDisplayableProductModel`）：`workflowGeneration.ts`(提交闸门) / `generationCapabilities.ts`(参数面板) / `promptBarPolicy.ts`(模型标签) / `aiGateway.ts` / `PromptBar.tsx` / `SettingsPanel.tsx`(3 处) / `providerGenerationAdapter.ts`(原报错点) / `modelRefs.ts`(6 处) / `workflowPromptPolicy.ts` / `hooks/useApiKeys.ts` / `App.tsx` / `utils/platformModelGate.ts`(第二道拦截)
- **仍未放开（有意）**：非创作模型黑名单（`inferProductCapability` 返回 null 者，如 `text-moderation-latest`）
- 回归 `tests/unregisteredModelRouting.test.ts`（19 例）；**对照组已验**：回退任一处 → 对应用例精准失败
- ⚠️ `resolveRouteMapping` 读 key 上**已持久化**的 `routeMappings`；单造 key 测试须先过 `mergeSuggestedProductRouteMappings`

## 🔴 产品模型条目拆分（GPT Image 2.5 双模型）
- 飞哥会配 `GPT-Image-2.5-Flare`（速度优先）与 `-Sunburst`（精度优先）**两个独立模型**，下拉须**按模型名区分**
- ⛔ 曾把两者登记为**同一** `flovart:gpt-image-2.5` 的 `officialModelIds` → 下拉只有一条 → 无法区分。**必须拆两条**
- 拆分要点：`tools/flovart/product-models.js` 两条 + `services/productModelCatalog.ts` 的 `CAPABILITY_BY_ID` 两条（均 `GPT_IMAGE_2_5_QUALITIES`=`['low','medium','high','xhigh','max']`）；`normalizeLoose` **不剥 `-flare`/`-sunburst` 后缀**
- `promptBarPolicy.productFamily()` 按 `includes('gpt-image')` 归组 → 同 family，右侧 `product.name` 区分
- GPT Image 2.5 五档 quality（2 保三档 `['low','medium','high']`）；`PromptBar.tsx` 文案 low→低画质/medium→标准画质/xhigh→超高画质/max→极致画质/其余→高画质；`WorkflowNodePromptBar.tsx` 的 `normalizeQualityForModel()` 用 `getEffectiveProductModelCapabilities()` 夹取非法值回退 `'high'`

