"""第三方直连 Provider 适配层（绕过 new-api 网关）。

每个第三方服务一个模块，统一暴露：
- submit_*(uid, ...) -> str           返回第三方任务/prediction id
- get_status(task_id) -> (status, urls)   status∈pending/processing/completed/failed；urls 为图片 URL 列表
- cancel(task_id) -> None            尽力取消（忽略失败）
- close() -> None                     归还 httpx 连接池

BFF tasks.py 按 TASK_TYPES[kind]["tp"] 分发到对应模块，产物统一落 BFF cloud_media。
"""
