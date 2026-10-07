import flet as ft


def build_pager(
    total_rows: int,
    page: int,
    page_size: int,
    on_change_page,
    on_change_size,
    show_slider: bool = False,
    compact: bool = False,
):
    pages = max(1, (total_rows + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start_row = page * page_size + 1 if total_rows else 0
    end_row = min((page + 1) * page_size, total_rows)

    page_field = ft.TextField(
        value=str(page + 1),
        width=44 if compact else 52,
        text_size=12,
        dense=True,
        text_align=ft.TextAlign.CENTER,
        tooltip="Jump to page (Enter)",
        content_padding=ft.padding.symmetric(horizontal=4, vertical=2 if compact else 4),
    )

    def _jump(e=None):
        try:
            target = int((page_field.value or "1").strip()) - 1
            on_change_page(max(0, min(target, pages - 1)))
        except ValueError:
            page_field.value = str(page + 1)
            page_field.update()

    page_field.on_submit = _jump

    icon_sz = 16 if compact else 18
    row_controls: list[ft.Control] = [
        ft.Text(
            f"{start_row:,}-{end_row:,} of {total_rows:,}",
            size=11 if compact else 12,
            color=ft.Colors.GREY_400,
        ),
        ft.IconButton(
            icon=ft.Icons.FIRST_PAGE,
            icon_size=icon_sz,
            tooltip="First",
            on_click=lambda e: on_change_page(0),
            disabled=page == 0,
        ),
        ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT,
            icon_size=icon_sz,
            tooltip="Prev",
            on_click=lambda e: on_change_page(page - 1),
            disabled=page == 0,
        ),
        page_field,
        ft.Text(f"/ {pages}", size=11 if compact else 12),
        ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT,
            icon_size=icon_sz,
            tooltip="Next",
            on_click=lambda e: on_change_page(page + 1),
            disabled=page >= pages - 1,
        ),
        ft.IconButton(
            icon=ft.Icons.LAST_PAGE,
            icon_size=icon_sz,
            tooltip="Last",
            on_click=lambda e: on_change_page(pages - 1),
            disabled=page >= pages - 1,
        ),
    ]

    if show_slider and pages > 1:
        slider = ft.Slider(
            min=1,
            max=float(pages),
            value=float(page + 1),
            divisions=max(0, min(pages - 1, 200)),
            label="{value}",
            width=140 if compact else 200,
            height=28,
        )
        slider.on_change_end = lambda e: on_change_page(int(slider.value) - 1)
        row_controls.append(slider)

    row_controls.append(
        ft.Dropdown(
            label="Pg size",
            value=str(page_size),
            width=88 if compact else 96,
            text_size=12,
            dense=True,
            options=[ft.dropdown.Option(str(s)) for s in (25, 50, 100, 200, 500)],
            on_change=lambda e: on_change_size(int(e.control.value)),
            content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
        )
    )

    return ft.Container(
        content=ft.Row(
            row_controls,
            spacing=2 if compact else 4,
            wrap=False,
            scroll=ft.ScrollMode.AUTO,
            alignment=ft.MainAxisAlignment.START,
            tight=True,
        ),
        padding=ft.padding.symmetric(vertical=0 if compact else 4),
    )
