# ui/toolbar.py
import flet as ft


def build_toolbar(
    on_collect, on_match, on_auto_assign, on_apply_manual,
    on_save, on_push, on_merge, on_scan_downloads,
    overwrite_ref, dry_run_ref,
    hide_nsfw_ref, hide_female_ref, hide_male_ref,
    on_content_filter_change,
    status_trailing: list[ft.Control] | None = None,
    on_quick_assign=None,  # kept for call-site compat; Sel→ moved onto Liked cards
    on_scan_originals=None,
    auto_push_ref=None,
    on_stop=None,
    on_quick_collect=None,
):
    overwrite_switch = ft.Switch(
        label="Overwrite", value=overwrite_ref.value,
        label_position=ft.LabelPosition.RIGHT,
        on_change=lambda e: overwrite_ref.set(e.control.value),
        tooltip="When ON, Auto-Assign overwrites existing Assigned values. When OFF, only fills empty Assigned.",
    )
    dry_run_switch = ft.Switch(
        label="Dry-run", value=dry_run_ref.value,
        label_position=ft.LabelPosition.RIGHT,
        on_change=lambda e: dry_run_ref.set(e.control.value),
        tooltip="Simulate Push — log actions without writing to Sketchfab",
    )
    auto_push_switch = None
    if auto_push_ref is not None:
        auto_push_switch = ft.Switch(
            label="Auto-push",
            value=bool(auto_push_ref.value),
            label_position=ft.LabelPosition.RIGHT,
            on_change=lambda e: auto_push_ref.set(e.control.value),
            tooltip="After local assign, automatically Push to Sketchfab (respects Dry-run)",
        )

    def _toggle_nsfw(e):
        nsfw_btn.selected = not nsfw_btn.selected
        hide_nsfw_ref.set(nsfw_btn.selected)
        nsfw_btn.update()
        on_content_filter_change()

    def _toggle_female(e):
        female_btn.selected = not female_btn.selected
        hide_female_ref.set(female_btn.selected)
        female_btn.update()
        on_content_filter_change()

    def _toggle_male(e):
        male_btn.selected = not male_btn.selected
        hide_male_ref.set(male_btn.selected)
        male_btn.update()
        on_content_filter_change()

    nsfw_btn = ft.IconButton(
        icon=ft.Icons.VISIBILITY_OFF,
        selected=hide_nsfw_ref.value,
        selected_icon=ft.Icons.BLOCK,
        tooltip="N — hide NSFW text + models in N Collection / NSFW (Assigned or Already In)",
        style=ft.ButtonStyle(
            bgcolor={ft.ControlState.SELECTED: "#7f1d1d"},
            color={ft.ControlState.SELECTED: ft.Colors.RED_200},
        ),
        on_click=_toggle_nsfw,
    )
    female_btn = ft.IconButton(
        icon=ft.Icons.WOMAN,
        selected=hide_female_ref.value,
        selected_icon=ft.Icons.WOMAN_2,
        tooltip="W — hide female content + Female* collections (Assigned or Already In; mannequins kept unless assigned)",
        style=ft.ButtonStyle(
            bgcolor={ft.ControlState.SELECTED: "#14532d"},
            color={ft.ControlState.SELECTED: ft.Colors.GREEN_200},
        ),
        on_click=_toggle_female,
    )
    male_btn = ft.IconButton(
        icon=ft.Icons.MAN,
        selected=hide_male_ref.value,
        selected_icon=ft.Icons.MAN_2,
        tooltip="M — hide male content + Male* collections (Assigned or Already In; mannequins kept unless assigned)",
        style=ft.ButtonStyle(
            bgcolor={ft.ControlState.SELECTED: "#1e3a5f"},
            color={ft.ControlState.SELECTED: ft.Colors.CYAN_200},
        ),
        on_click=_toggle_male,
    )

    stop_btn = ft.IconButton(
        icon=ft.Icons.STOP_CIRCLE,
        icon_color=ft.Colors.RED_300,
        tooltip="Stop Push / Collect — already-posted assignments are saved; remaining stay Pending",
        on_click=lambda e: on_stop() if on_stop else None,
        disabled=on_stop is None,
    )

    controls: list[ft.Control] = [
        ft.ElevatedButton(
            "Collect",
            icon=ft.Icons.CLOUD_DOWNLOAD,
            height=36,
            on_click=lambda e: on_collect(),
            tooltip="Full Collect — all likes + list every collection's models",
        ),
        ft.ElevatedButton(
            "Quick",
            icon=ft.Icons.UPDATE,
            height=36,
            on_click=lambda e: on_quick_collect() if on_quick_collect else on_collect(),
            tooltip="Quick Collect — recent likes/unlikes + re-check drifted collections only",
            disabled=on_quick_collect is None,
        ),
        ft.ElevatedButton("Match", icon=ft.Icons.PSYCHOLOGY, height=36, on_click=lambda e: on_match(), tooltip="Fill Suggested/Fuzzy columns (local workbook only)"),
        ft.ElevatedButton("Auto-Assign", icon=ft.Icons.SMART_BUTTON, height=36, on_click=lambda e: on_auto_assign(), tooltip="Apply collections_terms.yaml rules to Assigned column"),
        overwrite_switch,
        ft.ElevatedButton("Manual→Assigned", icon=ft.Icons.INPUT, height=36, on_click=lambda e: on_apply_manual(), tooltip="Copy Manual column into Assigned (per-row overrides)"),
        ft.ElevatedButton("Save", icon=ft.Icons.SAVE, height=36, on_click=lambda e: on_save()),
        ft.ElevatedButton("Scan DL", icon=ft.Icons.FOLDER_OPEN, height=36, on_click=lambda e: on_scan_downloads()),
        ft.ElevatedButton(
            "Scan Originals",
            icon=ft.Icons.INVENTORY_2,
            height=36,
            on_click=lambda e: on_scan_originals() if on_scan_originals else None,
            tooltip="Check Download API for author original archives (FBX/OBJ/DAE/…) — downloadable models only",
            disabled=on_scan_originals is None,
        ),
        stop_btn,
        ft.Container(
            padding=ft.padding.symmetric(horizontal=4),
            border=ft.border.all(1, "#334155"),
            border_radius=6,
            content=ft.Row(
                [
                    ft.Text("N", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.RED_400),
                    nsfw_btn,
                    ft.Text("W", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_400),
                    female_btn,
                    ft.Text("M", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.CYAN_400),
                    male_btn,
                ],
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        ),
        dry_run_switch,
        *( [auto_push_switch] if auto_push_switch is not None else [] ),
        ft.ElevatedButton("Push", icon=ft.Icons.ROCKET_LAUNCH, height=36, on_click=lambda e: on_push(), tooltip="Add models to their Assigned collection(s) on Sketchfab"),
        ft.IconButton(icon=ft.Icons.MERGE_TYPE, tooltip="Find similar collections", on_click=lambda e: on_merge()),
    ]
    if status_trailing:
        controls.append(ft.VerticalDivider(width=1))
        controls.extend(status_trailing)
    return ft.Row(
        controls,
        alignment=ft.MainAxisAlignment.START,
        wrap=False,
        scroll=ft.ScrollMode.ALWAYS,
        spacing=6,
        run_spacing=4,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
