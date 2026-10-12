"""全局配置：从 .env 读取（P0-2）。"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _csv(key: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(key, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    pg_dsn: str
    okx_proxy: str
    symbols: tuple[str, ...]
    stock_symbols: tuple[str, ...]
    contract_types: tuple[str, ...]
    timeframes: tuple[str, ...]
    backfill_days: int
    pool_top_n: int
    wecom_webhook: str
    feishu_webhook: str
    feishu_secret: str
    llm_api_key: str
    llm_base_url: str
    llm_model_extract: tuple[str, ...]
    llm_model_explain: tuple[str, ...]
    llm_model_vision: tuple[str, ...]


def load() -> Settings:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "trading")
    user = os.getenv("POSTGRES_USER", "trading")
    pw = os.getenv("POSTGRES_PASSWORD", "change_me")
    return Settings(
        pg_dsn=f"postgresql://{user}:{pw}@{host}:{port}/{db}",
        okx_proxy=os.getenv("OKX_PROXY", ""),
        symbols=_csv("SYMBOLS", "BTC/USDT"),
        stock_symbols=_csv("STOCK_SYMBOLS", "000157.SZ,002284.SZ,600199.SH"),
        contract_types=_csv("CONTRACT_TYPES", "spot"),
        timeframes=_csv("TIMEFRAMES", "1h"),
        backfill_days=int(os.getenv("BACKFILL_DAYS", "30")),
        pool_top_n=int(os.getenv("POOL_TOP_N", "5")),
        wecom_webhook=os.getenv("WECOM_WEBHOOK", ""),
        feishu_webhook=os.getenv("FEISHU_WEBHOOK", ""),
        feishu_secret=os.getenv("FEISHU_SECRET", ""),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        llm_model_extract=_csv("LLM_MODEL_EXTRACT", "qwen-plus"),
        llm_model_explain=_csv("LLM_MODEL_EXPLAIN", "qwen-plus"),
        llm_model_vision=_csv("LLM_MODEL_VISION", "qwen-vl-max"),
    )


settings = load()
