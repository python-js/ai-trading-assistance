"""价格显示格式化（仅展示层；存储与计算保持原始精度）。

- 虚拟币：最少 4 位小数，按价格量级自适应至多 8 位：d = clamp(3 - floor(log10(x)), 4, 8)。
  例：83558.9→83558.9000；13.376→13.3760；0.5109→0.5109；0.0778→0.07780。
- A 股（exchange='akshare'）：固定 2 位（与行情报价习惯一致）。
"""
import math

_MIN_DEC = 4
_MAX_DEC = 8


def price_decimals(x: float, exchange: str = "") -> int:
    if exchange == "akshare":
        return 2
    if not math.isfinite(x) or x <= 0:
        return _MIN_DEC
    return min(_MAX_DEC, max(_MIN_DEC, 3 - math.floor(math.log10(x))))


def fmt_price(x: float, exchange: str = "") -> str:
    return f"{x:.{price_decimals(x, exchange)}f}"
