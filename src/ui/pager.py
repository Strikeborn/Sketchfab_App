import flet as ft

def build_pager(total_rows: int, page: int, page_size: int, on_change_page, on_change_size):
    pages = max(1, (total_rows + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    return ft.Row(
        [
            ft.Text(f"{total_rows} rows  |  page {page+1}/{pages}"),
            ft.IconButton(icon=ft.Icons.SKIP_PREVIOUS, on_click=lambda e: on_change_page(0), disabled=page==0),
            ft.IconButton(icon=ft.Icons.NAVIGATE_BEFORE, on_click=lambda e: on_change_page(page-1), disabled=page==0),
            ft.IconButton(icon=ft.Icons.NAVIGATE_NEXT, on_click=lambda e: on_change_page(page+1), disabled=page>=pages-1),
            ft.IconButton(icon=ft.Icons.SKIP_NEXT, on_click=lambda e: on_change_page(pages-1), disabled=page>=pages-1),
            ft.Dropdown(
                value=str(page_size),
                options=[ft.dropdown.Option(str(s)) for s in (25, 50, 100, 200)],
                width=90,
                on_change=lambda e: on_change_size(int(e.control.value)),
                tooltip="Rows per page",
            ),
        ],
        spacing=8,
        alignment=ft.MainAxisAlignment.START,
    )
