# state.py
from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd

@dataclass
class AppState:
    liked_df: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    colls_df: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    overwrite: bool = False
    dry_run: bool = True
    busy: bool = False
    username: str = ""
    # paging
    liked_page: int = 0
    colls_page: int = 0
    page_size: int = 50