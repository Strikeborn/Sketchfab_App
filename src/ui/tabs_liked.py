import flet as ft

PLACEHOLDER_COL = ft.DataColumn(ft.Text("No data yet"))

class LikedTab(ft.Column):
    def __init__(self):
        super().__init__(expand=True)
        self.table = ft.DataTable(columns=[PLACEHOLDER_COL], rows=[], expand=True)
        self.controls = [self.table]

    def set_df(self, df):
        if df is None or df.empty:
            self.table.columns = [PLACEHOLDER_COL]
            self.table.rows = []
        else:
            self.table.columns = [ft.DataColumn(ft.Text(c)) for c in df.columns]
            self.table.rows = [
                ft.DataRow(
                    cells=[ft.DataCell(ft.Text("" if v is None else str(v))) for v in row]
                )
                for row in df.itertuples(index=False, name=None)
            ]
        self.update()
