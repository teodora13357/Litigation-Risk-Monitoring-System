"""案件管理系统 —— 应用包。

所有业务代码集中在 app/ 包内，根目录仅保留 main.py 入口：

- database：PostgreSQL 连接与会话管理
- models：SQLAlchemy ORM 模型（LawsuitCase + 字典/文件/任务/审计/时间线表）
- schemas：Pydantic 请求/响应模型
- crud：数据访问层（增查删改、筛选、排序）
- auth：认证工具（密码哈希 / JWT / 角色权限）
- logging：统一结构化日志
- constants：业务枚举常量
- error_codes：业务错误码与统一响应
- excel_export：Excel 导出
- ocr_service：OCR 识别服务（本地 MinerU）
- redis_client：Redis 客户端（预留缓存模块）
- deps：依赖注入（get_db / get_current_user）
- utils：共享工具（案号校验/生成）
- audit：审计日志写入
- files_config：文件类型配置与上传校验规则
- ocr_tasks：内存 OCR 任务表与后台识别
- routers：按业务域拆分路由
"""
