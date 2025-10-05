# ui/log_view.py
import flet as ft

class LogView(ft.Container):
    def __init__(self):
        super().__init__(expand=True, content=ft.Text("", selectable=True))
    def append(self, line: str):
        self.content.value = (self.content.value + ("\n" if self.content.value else "") + line)
        self.update()
    def set(self, text: str):
        self.content.value = text or ""
        self.update()
