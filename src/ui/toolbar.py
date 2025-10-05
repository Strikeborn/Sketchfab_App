# ui/toolbar.py
import flet as ft

def build_toolbar(on_collect, on_match, on_auto_assign, on_apply_manual,
                  on_save, on_push, on_merge, overwrite_ref, dry_run_ref):
    # Switches bound to state via refs (so main can read their value)
    overwrite_switch = ft.Switch(label="Overwrite Assigned", value=overwrite_ref.value,
                                 on_change=lambda e: overwrite_ref.set(e.control.value), tooltip="If on, Auto-Assign may replace existing Assigned.")
    dry_run_switch = ft.Switch(label="Dry-run Push", value=dry_run_ref.value,
                               on_change=lambda e: dry_run_ref.set(e.control.value), tooltip="If on, simulate push without API writes.")
    return ft.Row([
        ft.ElevatedButton("Collect", icon=ft.Icons.CLOUD_DOWNLOAD, on_click=lambda e: on_collect()),
        ft.ElevatedButton("Match", icon=ft.Icons.PSYCHOLOGY, on_click=lambda e: on_match()),
        ft.ElevatedButton("Auto-Assign", icon=ft.Icons.SMART_BUTTON, on_click=lambda e: on_auto_assign()),
        overwrite_switch,
        ft.ElevatedButton("Apply Manual → Assigned", icon=ft.Icons.INPUT, on_click=lambda e: on_apply_manual()),
        ft.ElevatedButton("Save Workbook", icon=ft.Icons.SAVE, on_click=lambda e: on_save()),
        dry_run_switch,
        ft.ElevatedButton("Push Assigned", icon=ft.Icons.ROCKET_LAUNCH, on_click=lambda e: on_push()),
        ft.IconButton(icon=ft.Icons.MERGE_TYPE, tooltip="Find/Merge similar collections", on_click=lambda e: on_merge()),
    ], alignment=ft.MainAxisAlignment.START,
    wrap=False,                          # keep on one line
    scroll=ft.ScrollMode.ALWAYS,         # enable horizontal scroll if needed
    spacing=8,
)
