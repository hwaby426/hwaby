from typing import List, Dict, Type, Any
from signals.base import BaseStrategy, SignalRecord
from signals.ma_strategy import MACrossStrategy
from signals.macd_strategy import MACDCrossStrategy, MACDPredictiveCrossStrategy
from signals.kdj_strategy import KDJStrategy
from signals.boll_strategy import BOLLStrategy
from signals.resonance_strategy import MACDKDJResonanceStrategy
from signals.multi_factor_strategy import MultiFactorScoreStrategy

STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    '均线金叉': MACrossStrategy,
    'MACD金叉': MACDCrossStrategy,
    'MACD预测金叉': MACDPredictiveCrossStrategy,
    'KDJ超买超卖': KDJStrategy,
    'BOLL突破': BOLLStrategy,
    'MACD+KDJ共振': MACDKDJResonanceStrategy,
    '多因子打分': MultiFactorScoreStrategy,
}


def get_strategy(name: str, **kwargs: Any) -> BaseStrategy:
    """获取策略实例，支持向策略构造函数传参（如 check_volume=False）。

    注：只有 MACDCrossStrategy 会读取这些 kwargs，
    其他策略的默认构造函数忽略未知参数（它们没有 __init__ 参数）。
    为了安全，这里只把 kwargs 透传给 MACDCrossStrategy。
    """
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知策略: {name}")
    if name == 'MACD金叉':
        return cls(**kwargs)
    return cls()


def get_all_strategies(**kwargs: Any) -> List[BaseStrategy]:
    """获取全部策略实例，kwargs 仅对 MACDCrossStrategy 生效。"""
    return [get_strategy(name, **kwargs) for name in STRATEGY_REGISTRY.keys()]


def get_strategy_names() -> List[str]:
    return list(STRATEGY_REGISTRY.keys())
