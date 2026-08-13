"""案件管理系统 —— 应用包。

- deps：依赖注入（get_db / get_current_user）
- utils：共享工具（案号校验/生成）
- audit：审计日志写入
- files_config：文件类型配置与上传校验规则
- ocr_tasks：内存 OCR 任务表与后台识别
- routers：按业务域拆分路由
"""
