"""
file_provider - 源文件获取抽象（§4）

抽象 FileProvider 接口，下游（路由/索引链路）只依赖接口，不关心底层存储实现。
当前实现：MinIOFileProvider（默认）/ MockFileProvider（兜底）。
新增对象存储（如 OSS）时，实现 FileProvider 抽象并在 build_file_provider 注册即可，
接口与业务逻辑零改动。对象路径与租户前缀规则集中在 provider 模块统一维护。
"""
