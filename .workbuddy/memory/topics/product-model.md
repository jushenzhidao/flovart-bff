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

## 新增模型到底要不要更新镜像（2026-09-16 结论：**不用**）

飞哥问「配 gemini 图片模型要不要更新镜像」。逐层核完：**BFF 镜像不需要动，只有前端
`product-models.js` 才可能需要改（且那是重新构建前端，不是 BFF）**。

**BFF 侧为什么不用改（三条硬证据）**：
1. **没有模型白名单** —— 全仓 `ALLOWED_MODELS` / `MODEL_WHITELIST` / `allowed_models` 零命中，
   模型名从不参与 BFF 的分支判断（只在注释里出现）。
2. `app/image_model_modes.py:17` 的 `_IMAGE_KEYS` **已含 `"gemini"`** → `is_image_model('gemini-*')`
   恒为 True，按图片模型处理。
3. 9 个 `GATEWAY_SYNC_*_PATH` **全部** = `v1/images/generations`，能力靠**入参**
   （`image[]`/`mask`/`variant`）区分、不靠模型名（见 `topics/gateway.md`）。
4. 「平台共享服务」是**运行期数据**（`PUT /api/platform/services`，`routers/platform_services.py:154`），
   不编译进镜像；`image_model_modes.json` 同理（每次 `store.load_json` 读盘，改完即生效、不用重启）。

**前端侧的真实缺口** —— 这里有两层匹配，**别只看第一层**（2026-09-16 实测纠正）：

| 层级 | 入口 | 匹配方式 | 用在哪 |
|---|---|---|---|
| ① 查询/展示 | `getProductModel()`（`productModelCatalog.ts:151`） | 精确 → 再走 `normalizeLoose`（**会剥结尾 `-preview`**） | 「是否已登记产品模型」、展示名 |
| ② 路由绑定 | `suggestProductRouteMappings()`（`productModelCatalog.ts:503`） | **只认 `normalize()` 严格匹配** | 下拉能否选、参数面板、提交路由 |

⚠️ **「① 能解析」≠「② 能正确路由」。** `gemini-3-pro-image-preview` 正是这种：
① 因 `normalizeLoose` 剥 `-preview` 而解析到 `flovart:gemini-3-pro-image`，
② 严格匹配失败 → 落 `flovart:custom:` 兜底。**判断有没有缺口必须以 ② 为准。**

| chatfire 实测可用的模型名 | 路由绑定（②）结果 |
|---|---|
| `gemini-3.1-flash-image-preview` | ✅ `flovart:gemini-3.1-flash-image`（原有 aliases） |
| `gemini-3.1-flash-lite-image` | ✅ `flovart:gemini-3.1-flash-lite-image`（本来就登记了，无需补） |
| `gemini-3-pro-image-preview` | ⚠️ 曾走兜底 → 2026-09-16 补**独立条目** `flovart:gemini-3-pro-image-preview` |
| `gemini-3.1-flash-image-preview_2k` | ⚠️ 曾走兜底 → 2026-09-16 补**独立条目** `flovart:gemini-3.1-flash-image-2k`（`resolutions:['2K']` 收窄档位） |

**为什么 `-preview` 补独立条目而不是 alias**：`tests/productModelCatalog.test.ts:79` 有一条护栏断言
「已关停的 Google preview id 不得被自动映射到线上 GA 条目」。想让预览 id 显示成「Gemini Pro Image」
只需把它加进 GA 条目的 `aliases`（1 行），但那会推翻该护栏、需同步改测试 —— 待飞哥拍板。
（flash 的 `-preview` 早就是 GA 条目的 alias，两处口径本就不一致。）

匹配不上并非不可用：`resolveDisplayableProductModel()` → `inferProductCapability()`（`IMAGE_HINT`
正则含 `image`）→ `synthesizeProductModel()` 合成 `flovart:custom:<原名>`，能力参数走
`FALLBACK_IMAGE_CAPABILITY`（保守默认）。**能选中、能调用，只是参数面板不精确（分辨率/参考图上限偏保守）、
显示名是原始模型名。**

⚠️ 新增登记**必须两处同时改**，漏一处 `capabilities` 变 `undefined` → 参数面板消失：
`tools/flovart/product-models.js` 加条目 **+** `services/productModelCatalog.ts` 的 `CAPABILITY_BY_ID` 加同 id。

✅ 验证套路（不必开浏览器）：用 esbuild 把**真实 TS** 打进 node 跑
`./node_modules/.bin/esbuild entry.ts --bundle --platform=node --format=esm --outfile=out.mjs && node out.mjs`，
entry 里 import `getProductModel / resolveDisplayableProductModel / suggestProductRouteMappings /
getEffectiveProductModelCapabilities`，打印「严格命中 / 展示 id / 分辨率 / 路由 productModelId 是否 `flovart:custom:` 兜底」。
⚠️ 临时 entry 测完即删；`git stash push -- <两个文件>` 可对比「改动前」行为来确认失败是否本就存在。

