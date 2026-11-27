"""源文件获取抽象：FileProvider 接口供下游依赖，当前实现 MinIO 与本地目录；新增存储实现接口并在 build_file_provider 注册即可，路径/租户前缀规则集中在 provider 模块。"""
