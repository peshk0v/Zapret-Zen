from __future__ import annotations

from typing import Any

from ui.ctk_theme import CtkTheme
from ui.ctk_tooltip import attach_tooltip_to_widgets


def create_entry(ctk, parent, theme, *, var=None, width=0, height=36, radius=10, **kw):
    opts = {
        "font": (theme.ui_font_family, 13), "corner_radius": radius,
        "fg_color": theme.bg, "border_color": theme.field_border,
        "border_width": 1, "text_color": theme.text_primary,
    }
    if var is not None:
        opts["textvariable"] = var
    if width:
        opts["width"] = width
    opts["height"] = height
    opts.update(kw)
    return ctk.CTkEntry(parent, **opts)


def create_checkbox(ctk, parent, theme, text, variable):
    return ctk.CTkCheckBox(
        parent, text=text, variable=variable,
        font=(theme.ui_font_family, 13), text_color=theme.text_primary,
        fg_color=theme.tg_blue, hover_color=theme.tg_blue_hover,
        corner_radius=6, border_width=2, border_color=theme.field_border,
    )


def create_label(ctk, parent, theme, text, *, size=12, bold=False, secondary=True, **kw):
    weight = "bold" if bold else "normal"
    return ctk.CTkLabel(
        parent, text=text,
        font=(theme.ui_font_family, size, weight),
        text_color=theme.text_secondary if secondary else theme.text_primary,
        anchor="w", **kw,
    )


def create_labeled_entry(ctk, parent, theme, label_text, value, *, tip="", width=0, pack_fill=False):
    col = ctk.CTkFrame(parent, fg_color="transparent")
    lbl = create_label(ctk, col, theme, label_text)
    lbl.pack(anchor="w", pady=(0, 2))
    var = ctk.StringVar(master=parent, value=str(value))
    ent = create_entry(ctk, col, theme, var=var, width=width)
    if pack_fill:
        ent.pack(fill="x")
    else:
        ent.pack(anchor="w")
    if tip:
        attach_tooltip_to_widgets([lbl, ent, col], tip)
    return col, var


def create_config_section(
    ctk: Any,
    parent: Any,
    theme: CtkTheme,
    title: str,
    *,
    bottom_spacer: int = 6,
) -> Any:
    wrap = ctk.CTkFrame(parent, fg_color="transparent")
    wrap.pack(fill="x", pady=(0, bottom_spacer))
    create_label(ctk, wrap, theme, title, secondary=False, bold=True).pack(anchor="w", pady=(0, 2))
    card = ctk.CTkFrame(
        wrap, fg_color=theme.field_bg, corner_radius=10,
        border_width=1, border_color=theme.field_border,
    )
    card.pack(fill="x")
    inner = ctk.CTkFrame(card, fg_color="transparent")
    inner.pack(fill="x", padx=10, pady=8)
    return inner


def create_combobox(ctk, parent, theme, *, variable, values, command=None):
    return ctk.CTkComboBox(
        parent, values=values, variable=variable, command=command,
        height=32, font=(theme.ui_font_family, 12),
        text_color=theme.text_primary, fg_color=theme.bg,
        border_color=theme.field_border, button_color=theme.field_border,
        button_hover_color=theme.text_secondary,
        dropdown_fg_color=theme.field_bg,
        dropdown_text_color=theme.text_primary,
        dropdown_hover_color=theme.field_border,
        corner_radius=8, state="readonly",
    )
