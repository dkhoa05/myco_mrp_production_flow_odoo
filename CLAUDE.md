# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this module is

`myco_mrp_production_flow` (Odoo 19, depends only on `mrp`) is a **thin orchestration layer** on top of Odoo's native Source/Child MO flow. It does not reimplement manufacturing logic — it hooks the native `mrp.workorder` buttons (`button_start`, `button_finish`, `action_mark_as_done`) and the native `mrp.production` close path to **auto-advance a chain of Work Orders across multiple MOs**, plus an OWL widget that visualizes the flow on the Master MO form.

The code, docstrings, UI strings, and commit messages are in **Vietnamese**. Match that when editing.

## Core concept: flow_role + cross-MO auto-orchestration

Every `mrp.workorder` gets a `flow_role`: `manager` (Quản lý) / `assembly` (Gia công) / `packing` (Đóng gói) / `none`. A **Master MO** holds the manager/assembly/packing WOs and spawns **Child MOs** via the native Manufacture route. Roles are auto-assigned on `action_confirm` by `_myco_auto_detect_flow_roles`, which matches WO/workcenter/operation names normalized via `normalize_vn` (`utils.py`, strips Vietnamese diacritics: "Quản lý"→"quan ly").

The automatic sequence (full ASCII diagram lives at the top of `models/mrp_workorder.py` — read it before touching flow logic):

1. User starts the manager WO → `_myco_start_first_child_workorders` auto-starts the first `ready` WO of every Child MO (parallel).
2. Each WO finishing fires `_myco_dispatch_after_done`: a Child WO starts the next WO in the **same** MO (`_myco_start_next_in_same_mo`) then advances the master; a master WO just advances the master.
3. `_myco_advance_flow` (on `mrp.production`) is **idempotent** and self-guarding — each step re-checks its own preconditions: (a) start assembly when all child WOs done, (b) start packing when assembly done, (c) finish manager when every other WO is done/cancel. Calling it repeatedly or out of order is safe; rely on that rather than tracking order.

Starting from a Child MO also works: `_myco_start_manager_from_child` back-fills the manager start and cascades the other children.

### Hard guard vs. soft confirm
- **Hard guard**: `button_mark_done` on the Master MO calls `_myco_check_flow_done_before_close`, which `raise UserError` if any flow WO is unfinished. This is the only hard block.
- **Soft confirm**: starting assembly/packing early, or finishing manager/packing early, opens the `myco.assembly.start.confirm` transient wizard listing unfinished WOs. It does **not** block — "Tiếp tục" re-runs the action with the confirm flag set. Logic in `_myco_flow_pending_before_start` / `_myco_finish_pending`. The packing-finish check **excludes the manager** from its pending list because finishing packing (the closing step) auto-finishes the manager via step (c); the manager-finish check excludes only itself.

### Context flags (recursion control — critical, don't break)
- `myco_skip_flow=True` — every auto-start routes through `_myco_auto_start`, which sets this so the re-entered `button_start` does **not** re-trigger the flow. Prevents infinite recursion.
- `myco_flow_confirmed=True` — set by the wizard's "Tiếp tục" to skip the soft-confirm check once.
- `myco_in_mark_as_done=True` — set by `action_mark_as_done` so the inner `button_finish` does not dispatch; dispatch happens exactly once at the outer level.

`_myco_auto_start` is the single safe entry point for all auto-starts: it bails on terminal/running/blocked/non-ready states and on done/cancel MOs, then calls `button_start(myco_skip_flow=True)`.

## File map
- `models/mrp_workorder.py` — WO button overrides, cross-MO triggers, `_myco_auto_start`. **Has the authoritative flow diagram.**
- `models/mrp_production.py` — Master MO helpers (wrap native `_get_children()`/`_get_sources()`), `_myco_advance_flow`, close guard, and `action_get_production_flow_data` (single-payload RPC for the widget, with explicit prefetch to avoid N+1).
- `models/const.py` — shared WO state constants (`WO_TERMINAL_STATES`, `WO_RUNNING_STATE`, `WO_STARTABLE_STATES`). Reuse these; do not inline state literals.
- `models/utils.py` — `normalize_vn`.
- `models/assembly_start_confirm.py` — soft-confirm wizard.
- `static/src/js/production_flow_tab.js` + `.xml` template (`myco_mrp.ProductionFlowTab`) + `.css` — OWL field widget `production_flow_tab` rendering the Master MO dashboard with a live client-side timer. Server `duration` comes from `get_duration()`; the client adds elapsed time only when `is_running !== false` (note the `=== false` checks for backward-compat with servers that don't return `is_running`).
- `views/` — all views **inherit** native mrp views (never copy actions/views): adds the `flow_role` column to the WO list, and a "Production Flow" notebook page on the MO form gated by core field `mrp_production_child_count == 0`.

## Conventions (follow these — they are load-bearing here)
- **Native-first**: inherit and extend Odoo core; reuse existing core fields/methods (e.g. `mrp_production_child_count`, `_get_children`) instead of recomputing. Read the relevant core source before adding anything — the Odoo source is available at `D:\odoo\reference\odoo` (and `D:\odoo\reference\mrp.zip`).
- All custom methods/fields are prefixed `_myco_` / `myco_` to stay clear of core.
- Keep changes idempotent and guarded by `flow_role`; never assume operation order.
- No automated tests exist in this module; verify changes by installing/upgrading the module against a running Odoo 19 instance and exercising the flow manually.

## Relevant skills
When working in this repo, the `odoo-native-first` and `odoo-development-skill` skills apply (Odoo custom-module standards: inherit core, read core source first, don't reinvent existing functionality).
