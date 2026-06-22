import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .const import WO_RUNNING_STATE, WO_TERMINAL_STATES
from .utils import normalize_vn

_logger = logging.getLogger(__name__)


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    myco_flow_state = fields.Selection(
        [
            ("empty", "No Child MO"),
            ("ready", "Ready"),
            ("running", "Running"),
            ("blocked", "Blocked"),
            ("done", "Done"),
        ],
        compute="_compute_myco_flow_metrics",
    )
    myco_flow_progress = fields.Float(compute="_compute_myco_flow_metrics")

    @api.depends(
        "production_group_id.child_ids.production_ids.state",
        "production_group_id.child_ids.production_ids.workorder_ids.state",
        "production_group_id.child_ids.production_ids.workorder_ids.progress",
        "workorder_ids.state",
        "workorder_ids.progress",
        "workorder_ids.flow_role",
    )
    def _compute_myco_flow_metrics(self):
        for production in self:
            children = production._myco_get_child_productions()
            workorders = production._myco_get_flow_workorders(children=children)

            if not children and not workorders:
                production.myco_flow_state = "empty"
                production.myco_flow_progress = 0.0
                continue
            if not workorders:
                production.myco_flow_state = "ready"
                production.myco_flow_progress = 0.0
                continue

            production.myco_flow_progress = (
                sum(workorders.mapped("progress")) / len(workorders)
            )
            if all(wo.state in WO_TERMINAL_STATES for wo in workorders):
                production.myco_flow_state = "done"
            elif any(wo.state == WO_RUNNING_STATE for wo in workorders):
                production.myco_flow_state = "running"
            elif any(wo.state == "blocked" for wo in workorders):
                production.myco_flow_state = "blocked"
            else:
                production.myco_flow_state = "ready"

    def action_confirm(self):
        res = super().action_confirm()
        self._myco_auto_detect_flow_roles()
        return res

    # ------------------------------------------------------------------
    # Helpers — wrap native Source/Child MO logic
    # ------------------------------------------------------------------
    def _myco_get_child_productions(self):
        self.ensure_one()
        return self._get_children().filtered(
            lambda mo: mo.id != self.id and mo.state != "cancel"
        )

    def _myco_get_source_productions(self):
        self.ensure_one()
        return self._get_sources().filtered(
            lambda mo: mo.id != self.id and mo.state != "cancel"
        )

    def _myco_get_flow_workorders(self, children=False):
        self.ensure_one()
        children = (
            children
            if children is not False
            else self._myco_get_child_productions()
        )
        return (self | children).mapped("workorder_ids").filtered(
            lambda wo: wo.state != "cancel"
        )

    def _myco_get_workorder_by_role(self, role):
        self.ensure_one()
        return self.workorder_ids.filtered(
            lambda wo: wo.flow_role == role and wo.state != "cancel"
        ).sorted(lambda wo: (wo.sequence, wo.id))[:1]

    # ------------------------------------------------------------------
    # Auto-detect flow_role on confirm
    # ------------------------------------------------------------------
    def _myco_auto_detect_flow_roles(self):
        for production in self:
            for wo in production.workorder_ids:
                if wo.flow_role and wo.flow_role != "none":
                    continue
                text = normalize_vn(
                    " ".join(
                        filter(
                            None,
                            (
                                wo.name,
                                wo.workcenter_id.display_name,
                                wo.operation_id.display_name,
                            ),
                        )
                    )
                )
                if "quan ly" in text:
                    wo.flow_role = "manager"
                elif any(k in text for k in ("gia cong", "nhung", "lanh")):
                    wo.flow_role = "assembly"
                elif "dong goi" in text:
                    wo.flow_role = "packing"

    # ------------------------------------------------------------------
    # Cross-MO trigger chain
    # ------------------------------------------------------------------
    def _myco_start_assembly_when_children_done(self):
        self.ensure_one()
        child_wos = self._myco_get_child_productions().mapped(
            "workorder_ids"
        ).filtered(lambda wo: wo.state != "cancel")
        if not child_wos or any(wo.state != "done" for wo in child_wos):
            return False
        assembly = self._myco_get_workorder_by_role("assembly")
        if not assembly:
            _logger.info("MO %s: no assembly WO.", self.display_name)
            return False
        return assembly._myco_auto_start(reason="all_child_workorders_done")

    def _myco_start_packing_when_assembly_done(self):
        self.ensure_one()
        assembly = self._myco_get_workorder_by_role("assembly")
        if assembly and assembly.state != "done":
            return False
        packing = self._myco_get_workorder_by_role("packing")
        if not packing:
            _logger.info("MO %s: no packing WO.", self.display_name)
            return False
        return packing._myco_auto_start(reason="assembly_done")

    def _myco_finish_manager_when_packing_done(self):
        self.ensure_one()
        packing = self._myco_get_workorder_by_role("packing")
        if packing and packing.state != "done":
            return False
        manager = self._myco_get_workorder_by_role("manager")
        if not manager or manager.state in WO_TERMINAL_STATES:
            return False
        manager.with_context(myco_skip_flow=True).button_finish()
        _logger.info("Auto-finished manager %s.", manager.display_name)
        return True

    # ------------------------------------------------------------------
    # Validation — chặn đóng MO tổng khi còn WO chưa done
    # ------------------------------------------------------------------
    def _myco_check_flow_done_before_close(self):
        self.ensure_one()
        if not self._myco_get_child_productions():
            return
        pending = self._myco_get_flow_workorders().filtered(
            lambda wo: wo.state not in WO_TERMINAL_STATES
        )
        if pending:
            raise UserError(
                _(
                    "Không thể hoàn tất MO tổng. Công đoạn chưa Done: %(names)s",
                    names=", ".join(pending.mapped("display_name")),
                )
            )

    def button_mark_done(self):
        if not self.env.context.get("myco_skip_flow"):
            for production in self:
                production._myco_check_flow_done_before_close()
        return super().button_mark_done()

    # ------------------------------------------------------------------
    # RPC cho OWL widget — single payload
    # ------------------------------------------------------------------
    def action_get_production_flow_data(self):
        self.ensure_one()
        children = self._myco_get_child_productions().sorted(lambda mo: mo.id)
        master_wos = self.workorder_ids.filtered(
            lambda wo: wo.state != "cancel"
        ).sorted(lambda wo: (wo.sequence, wo.id))

        # Prefetch toàn bộ field/quan hệ dùng trong payload để tránh N+1
        # query khi loop qua children bên dưới.
        flow_wos = (self | children).mapped("workorder_ids").filtered(
            lambda wo: wo.state != "cancel"
        )
        flow_wos.mapped("display_name")
        flow_wos.mapped("workcenter_id.display_name")
        flow_wos.mapped("progress")
        flow_wos.mapped("is_user_working")
        flow_wos.mapped("time_ids.duration")  # get_duration() đọc time_ids

        def wo_payload(wo):
            return {
                "id": wo.id,
                "name": wo.display_name,
                "workcenter": wo.workcenter_id.display_name or "",
                "state": wo.state,
                "is_running": wo.is_user_working,
                "flow_role": wo.flow_role or "none",
                "duration_expected": wo.duration_expected or 0.0,
                "duration": wo.get_duration(),
                "progress": wo.progress or 0.0,
            }

        return {
            "master": {
                "id": self.id,
                "name": self.display_name,
                "state": self.state,
                "flow_state": self.myco_flow_state,
                "flow_progress": self.myco_flow_progress,
                "child_count": len(children),
            },
            "children": [
                {
                    "id": child.id,
                    "name": child.display_name,
                    "product": child.product_id.display_name or "",
                    "qty": child.product_qty,
                    "uom": child.product_uom_id.display_name or "",
                    "state": child.state,
                    "workorders": [
                        wo_payload(wo)
                        for wo in child.workorder_ids.filtered(
                            lambda wo: wo.state != "cancel"
                        ).sorted(lambda wo: (wo.sequence, wo.id))
                    ],
                }
                for child in children
            ],
            "master_workorders": [wo_payload(wo) for wo in master_wos],
        }
