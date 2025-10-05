import flet as ft
import math

PLACEHOLDER_COL = ft.DataColumn(ft.Text("No data yet"))

class LikedTab(ft.Column):
    def __init__(self):
        super().__init__(expand=True)
        self.pager = ft.Row()
        self.table = ft.DataTable(columns=[PLACEHOLDER_COL], rows=[], expand=True)
        self.controls = [self.pager, self.table]

    def set_df(self, df, page_idx=0, page_size=50):
        if df is None or df.empty:
            self.pager.controls = [ft.Text("0 rows")]
            self.table.columns, self.table.rows = [PLACEHOLDER_COL], []
            self.update()
            return

        # slice the current page
        total = len(df)
        pages = max(1, math.ceil(total / page_size))
        page_idx = max(0, min(page_idx, pages-1))
        start, end = page_idx * page_size, min((page_idx+1)*page_size, total)
        dfp = df.iloc[start:end]

        self.table.columns = [ft.DataColumn(ft.Text(c)) for c in df.columns]
        self.table.rows = [
            ft.DataRow(cells=[ft.DataCell(ft.Text("" if v is None else str(v))) for v in row])
            for row in dfp.itertuples(index=False, name=None)
        ]
        self.update()
