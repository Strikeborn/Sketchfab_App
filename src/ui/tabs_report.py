# ui/tabs_report.py
import flet as ft

class ReportTab(ft.Column):
    def __init__(self, on_find_similar):
        super().__init__(expand=True)
        self.log = ft.Text("", selectable=True)
        self.find_btn = ft.ElevatedButton("Find Similar Collections", icon=ft.Icons.TRAVEL_EXPLORE,
                                          on_click=lambda e: on_find_similar())
        self.controls = [self.find_btn, ft.Divider(), self.log]

    def set_text(self, s: str):
        self.log.value = s or ""
        self.update()
