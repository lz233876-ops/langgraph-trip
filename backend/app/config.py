"""配置管理模块"""

from typing import List
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# 加载当前目录的 .env
load_dotenv()

# LLM配置字段在环境变量留空时回退的默认值
_LLM_FIELD_DEFAULTS = {
    "llm_model": "gpt-4o",
    "llm_temperature": 0.7,
    "llm_timeout": 60,
}


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,  # 控制是否环境变量匹配字段时是否区分大小写。
        extra="ignore",  # 忽略未声明的环境变量
    )

    # 应用基本配置
    app_name: str = "LangChain智能旅行助手"
    app_version: str = "1.0.0"
    debug: bool = False

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # CORS配置 - 使用字符串,在代码中分割
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # 高德地图API配置
    amap_api_key: str = ""

    # LLM配置 (LangChain ChatOpenAI, 兼容任意OpenAI格式端点)
    # 优先读取 LLM_* 命名, 同时兼容 OPENAI_* 旧命名
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY"),  # 多别名备选，按顺序寻找环境变量，优先去找环境变量 LLM_API_KEY如果找不到 LLM_API_KEY，自动退而求其次读取 OPENAI_API_KEY
    )
    llm_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL"),
    )
    llm_model: str = Field(
        default="gpt-4o",
        validation_alias=AliasChoices(
            "LLM_MODEL_ID", "LLM_MODEL", "OPENAI_MODEL", "OPENAI_MODEL_NAME"
        ),
    )
    llm_temperature: float = 0.7
    llm_timeout: int = 60

    # 日志配置
    log_level: str = "INFO"

    
    # @field_validator：Pydantic 字段校验钩子
    # 在环境变量赋值给类字段之前 / 之后，拦截值，自定义处理逻辑。
    @field_validator("llm_model", "llm_temperature", "llm_timeout", mode="before")
    @classmethod
    def _empty_env_to_default(cls, v, info):
        """环境变量为空字符串时回退到默认值,避免覆盖默认配置"""
        if v == "" or v is None:
            return _LLM_FIELD_DEFAULTS.get(info.field_name)
        return v

    def get_cors_origins_list(self) -> List[str]:
        """获取CORS origins列表"""
        return [origin.strip() for origin in self.cors_origins.split(",")]


# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


# 验证必要的配置
def validate_config():
    """验证配置是否完整"""
    errors = []
    warnings = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY未配置")

    if not settings.llm_api_key:
        warnings.append("LLM_API_KEY或OPENAI_API_KEY未配置,LLM功能可能无法使用")

    if errors:
        error_msg = "配置错误:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    if warnings:
        print("\n⚠️  配置警告:")
        for w in warnings:
            print(f"  - {w}")

    return True


# 打印配置信息(用于调试)
def print_config():
    """打印当前配置(隐藏敏感信息)"""
    print(f"应用名称: {settings.app_name}")
    print(f"版本: {settings.app_version}")
    print(f"服务器: {settings.host}:{settings.port}")
    print(f"高德地图API Key: {'已配置' if settings.amap_api_key else '未配置'}")

    print(f"LLM API Key: {'已配置' if settings.llm_api_key else '未配置'}")
    print(f"LLM Base URL: {settings.llm_base_url or 'https://api.openai.com/v1 (官方默认)'}")
    print(f"LLM Model: {settings.llm_model}")
    print(f"LLM Temperature: {settings.llm_temperature}")
    print(f"LLM Timeout: {settings.llm_timeout}s")
    print(f"日志级别: {settings.log_level}")


if __name__ == "__main__":
    print("当前配置:")
    settings = Settings()
    print_config()
