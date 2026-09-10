# flovart-bff 长期记忆

## 定位
Flovart「在线创作站」FastAPI BFF：登录/持久化/new-api 代理。前端独立仓库 D:\code\flovart-web（同机并存，直接改）。

## 架构铁律
- 登录=BFF 独立注册；注册=管理员影子建 new-api 号+赠送+自动登录（口令只换 PAT，不存密码）
- 会话=AES-256-GCM 加密 Cookie（服务端零存储）
- 云端：PostgreSQL(asyncpg)+OSS/COS/S3；本机兜底 USE_PG=False→LocalMeta(SQLite)+LocalBlob(本地文件)。cloudstore 改动须同时实现 Pg/Local 两套
- new-api 双头 PAT+New-Api-User；登录即 DELETE sessions/{sid} 归还（50 上限）
- 多业务隔离：每业务独立 new-api 管理员账号(uid)，严禁共用（PAT 互踢 401 雪崩）
- 出口 quota→points；单 worker

## 聊天免 Key
- /api/chat/completions 走 new-api /v1（只认 sk-）；每用户 mint+持久化归属配额 sk-(user_keys.py)
- 模型须配 BFF_CHAT_DEFAULT_MODEL/BFF_CHAT_VISION_MODEL

## 图片/视频任务（tasks.py 同步/异步双模式）
- async(image-gen/video-gen)透传网关；sync(upscale/remove-bg/outpaint/mask/annotate/relight/edit)阻塞直出落盘
- submit(uid,kind,params)：带 _gateway→BFF 直连用户指定外部网关(用户 BYOK)；否则 na.request_as_user 走 new-api 平台(管理员配的服务，计费落用户配额)
- 前端轮询键用返回 id(=request_id)
- 图片归一化 _normalize_sync_result：OpenAI {data}→{images}；前端 extractImageOutputs 扫 image/images/layers/data
- 网关端点契约见 docs/gateway-endpoint-contracts.md；方案 A(model-plaza 映射驱动)见 docs/gateway-mapping-design.md
- 关键坑：image 字段两种编码——images/generations 图生图用 image:string[]；其余编辑类用 image:{data,mimeType}

## 素材共享（A 点对点）
- admin_resolve_uid_by_username；cloud_media_shares(owner+target+media_key+perm)；字节不复制，/api/shared/media/{key} 代理校验
- 共享本地素材前先 ensureCloudKey 上传 BFF 拿 cloudMediaKey

## 代码地图
后端 app/routers/{auth,keys,usage,console,billing,promo,chat,shares}.py；user_keys.py；tasks.py+thirdparty/wavespeed.py；app/{db,oss,security,store,config,newapi_client,cloudstore}.py
前端：FlovartAgentPanel.tsx / services/{browserAgentKernel,imageTask,aiGateway,hostedClient,historyCloudSync}.ts / stores/useHostedStore.ts / components/ConfigManager/ConfigSelector.tsx / components/workflow/*

## 坑/待办
- M1：console 列表契约待真实 new-api 实测
- 前端多用户隔离已闭环(13 localforage 按 uid 隔离)
- 弱 SECRET_KEY dev-only-secret-change-me 由 /readyz 拦截勿删
- BFF venv：C:\Users\81068\.workbuddy\binaries\python\envs\flovart-bff
- 用户 PAT 失效自愈：/api/user/self 与 /api/me/ensure-key 降级管理员代用户通道
- ✅ 普通用户平台能力（2026-09-10 已修 + 本轮补模型可见性 + 守卫 bug 真正修正 + 开放用户自配）：①aiGateway.ts 平台判断 isHostedPlatform(key)=hosted 登录且 extraConfig.flovart_platform==='1'（带 key+baseUrl 的平台注入 Key 不再被误判 BYOK）；generateImageWithProvider/editImageWithProvider/runImageAgentWithProvider 三处统一 submitImageTask('image-gen') 复用管理员 gpt-image-2，model 取 platformImageModel()（优先 key.imageGenModel，否则 /models 第一个图片模型）；不带 _gateway，BFF 代发、计费落用户配额。②「管理员配的模型普通用户看不到」根因=三处连锁：App.tsx 注入平台 Key 未带 imageGenModel/videoGenModel/capabilities（已从 /api/models 启用模型识别图文模型补全）；useApiKeys.dynamicModelOptions 与 PromptBar 模型选择器原只认 key.imageGenModel，已加 customModels 回退（getProductModel(m)?.capability==='image'|'video'）让平台 Key 的模型进选择器。③平台 Key 注入守卫 bug（真根因，2026-09-10 末轮真正修正）：App.tsx 平台注入 effect 顶部 `if (platformInjectRef.current) return` 一次性守卫始终没被去掉，首跑时 hostedModels 异步未回（空）注入了空 customModels 平台 Key 并置 ref，之后模型目录填充、effect 重跑被守卫挡掉 → 平台 Key 永远「映射 0」。修正：彻底移除顶部 return 守卫及 platformInjectRef；已有 Key 时每次 hostedModels 变化走 needsUpdate 比较并 handleUpdateApiKey 刷新。④开放用户自配 + 隐藏网关原始模型 chips：`SettingsPanel.tsx` 把 hosted 普通用户 Tab 从「平台模型+安全」扩为「平台模型+AI 服务+模型映射+安全」；`PlatformModelPanel.tsx` 移除网关模型 chips 与数量，改为「由平台统一配置」。⑤AI 服务 tab 展示平台服务卡片（2026-09-10 追加）：普通用户打开「AI 服务」不再只看到空状态，而是在列表顶部看到只读的「平台模型」服务卡片（名称/已就绪/能力/默认生图视频模型/模型数量），明确告知「由管理员统一配置，所有账号可直接使用」；下方仍为用户自管 BYOK。实现：`SettingsPanel.tsx` 新增 `platformEntry = userApiKeys.find(k => k.extraConfig?.flovart_platform === '1')`，在 managedApiKeys 列表前渲染平台服务卡片，空状态文案根据 platformEntry 切换。⑤WaveSpeed 分层(split-layers)/多角度(multi-angle) 走 BFF thirdparty 服务端直连（WAVESPEED_API_KEY 在服务端 env），对所有登录用户本就可用，无需前端改动。用户自配 BYOK 仍直连并存。
- ✅ 前端多账号本地隔离（2026-09-10 **真正修正**）：workflow 存储 (`components/workflow/storage.ts`)、Agent 会话 (`services/browserAgentKernel.ts`) 原用**模块顶层 const 实例**按加载时 uid 固定 name，不刷新切号就串库（飞哥实测普通账号看到管理员工作流+Agent 历史）。真正修复：①两处 localforage 实例改为 `getXxxInstance()` 动态函数——每次读写按 `currentHostedUid()` 取/建实例并缓存，uid 变了下次读写自动落新账号库。②workflow 双层隔离：`components/workflow/storage.ts` 给每个 key 也追加 `__u${uid}:` 前缀，即使 zustand persist 的 name 固定，物理 key 也按 uid 隔离。③`App.tsx` 监听 `hostedUser?.id`，uid 切换时 `flushWorkflowPersistence`→`persist.clearStorage()`→清空内存→`rehydrate`，确保不刷新页面切账号时内存里不保留上一个账号的工作流。④`components/workflow/store.ts` 的 legacy migration 禁止在 hosted 模式下迁移旧未隔离数据，避免把旧共享工作流迁到普通账号。⑤Agent 会话 (`browserAgentKernel.ts`) 同样改为动态实例。素材/生成历史/keyVault 同机制已隔离。飞哥验证：同一浏览器切号后**硬刷新一次**即彻底隔离。
