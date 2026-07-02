"""
策略註冊表：backtest_engine.full_analysis() 依 strategy 參數分派到對應模組的
analyze()，回測與實盤（strategy_loop）共用同一份分派邏輯，不會各自長出兩套。

新增策略時：寫一個新模組，公開一個
    analyze(klines: List[Dict], filter_counter_trend: bool = False) -> Dict[str, Any]
函式，回傳的 dict 至少要有 "signals"（給 run_backtest 用，格式見 chanlun.py 的
generate_signals()：每個訊號至少要有 index/time/side/entry/sl/tp/reason）。
其餘欄位是這個策略自己決定要不要給前端顯示的結構資料（例如缠論的
fractals/bis/duans/zhongshu/conditions），格式不強制、換一個策略可以完全不一樣，
前端只有在該策略真的回傳這些欄位時才會畫對應的疊圖。

寫完後在下面的 STRATEGIES 註冊，main.py／backtest_engine.py 都不用改。
"""
from . import chanlun

STRATEGIES = {
    "chanlun": chanlun.analyze,
}
