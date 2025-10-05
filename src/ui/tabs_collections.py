# ui/tabs_collections.py
import flet as ft
import math
import pandas as pd

PLACEHOLDER_COL = ft.DataColumn(ft.Text("No data yet"))

def _fmt(v): return "None" if pd.isna(v) else str(v)

class CollectionsTab(ft.Column):
	def __init__(self):
		super().__init__(expand=True)                  # the whole tab fills
		self.pager = ft.Row()  # main.py will inject the real pager controls here
		self.table = ft.DataTable(columns=[PLACEHOLDER_COL], rows=[], expand=True)
		self.scroller = ft.ListView(expand=True, controls=[self.table])  # << scroll here
		self.controls = [self.pager, self.scroller]    # pager on top, table scrolls

	def set_df(self, df, page_idx: int = 0, page_size: int = 50):
		# empty / not loaded yet
		if df is None or getattr(df, "empty", True):
			self.pager.controls = [ft.Text("0 rows")]
			self.table.columns, self.table.rows = [PLACEHOLDER_COL], []
			self.update(); return
        
		# slice current page
		total = len(df)
		pages = max(1, math.ceil(total / page_size))
		page_idx = max(0, min(page_idx, pages - 1))
		start, end = page_idx * page_size, min((page_idx + 1) * page_size, total)
		dfp = df.iloc[start:end]
        
		# render
		self.table.columns = [ft.DataColumn(ft.Text(c)) for c in df.columns]
		self.table.rows = [
            ft.DataRow(
                cells=[ft.DataCell(ft.Text(_fmt(v))) for v in row]
            )
            for row in dfp.itertuples(index=False, name=None)
        ]
		self.update()
