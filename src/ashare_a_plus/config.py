from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StrategyConfig:
    rs_window: int = 120
    rs_top_quantile: float = 0.80
    high_low_window: int = 250
    pivot_window: int = 50
    min_listing_days: int = 250
    min_amount: float = 20_000_000.0
    max_distance_to_high: float = 0.15
    min_distance_from_low: float = 0.30
    max_distance_to_pivot: float = 0.05
    breakout_volume_multiple: float = 1.30


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    position_fraction: float = 0.10
    max_positions: int = 10
    stop_loss: float = 0.07
    first_target: float = 0.15
    second_target: float = 0.30
    sell_fee: float = 0.0003
    buy_fee: float = 0.0003
    stamp_tax: float = 0.0005
    slippage: float = 0.0005
    max_holding_days: int = 60


@dataclass(frozen=True)
class Paths:
    root: Path = Path(".")
    raw_dir: Path = Path("data/raw")
    processed_dir: Path = Path("data/processed")
    report_dir: Path = Path("reports")

    def ensure(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
