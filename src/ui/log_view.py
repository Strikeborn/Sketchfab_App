# ui/log_view.py
import flet as ft

class LogView(ft.Container):
    def __init__(self):
        super().__init__(expand=True, content=ft.Text("", selectable=True))

    def append(self, line: str):
        t = self.content
        t.value = (t.value + ("\n" if t.value else "") + line)
        if self.page:           # only update when mounted
            self.update()

    def set(self, text: str):
        self.content.value = text or ""
        if self.page:
            self.update()
