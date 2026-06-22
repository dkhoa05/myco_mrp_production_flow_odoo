import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from .const import WO_STARTABLE_STATES, WO_RUNNING_STATE, WO_TERMINAL_STATES

_logger = logging.getLogger(__name__)


class MrpWorkorder(models.Model):
    _inherit = "mrp.workorder"

    flow_role = fields.Selection(
        [
            ("none", "Không"),
            ("manager", "Quản lý"),
            ("assembly", "Gia công"),
            ("packing", "Đóng gói"),
        ],
        string="Vai trò Flow",
        default="none",
        copy=False,
    )

    # ------------------------------------------------------------------
    # Overrides — hook cross-MO logic vào native button_*
    # ------------------------------------------------------------------
    def button_start(self, raise_on_invalid_state=False):
        res = super().button_start(raise_on_invalid_state=raise_on_invalid_state)
        if self.env.context.get("myco_skip_flow"):
            return res
        for wo in self:
            if (
                wo.flow_role == "manager"
                and wo.production_id._myco_get_child_productions()
            ):
                wo._myco_start_first_child_workorders()
        return res

    def button_finish(self):
        if not self.env.context.get("myco_skip_flow"):
            for wo in self.filtered(lambda item: item.flow_role == "manager"):
                wo._myco_check_manager_can_finish()
        res = super().button_finish()
        if not self.env.context.get("myco_skip_flow") and not self.env.context.get(
            "myco_in_mark_as_done"
        ):
            for wo in self:
                wo._myco_dispatch_after_done()
        return res

    def action_mark_as_done(self):
        # Native action_mark_as_done gọi button_finish() bên trong, nên dùng
        # context myco_in_mark_as_done để button_finish KHÔNG dispatch sớm,
        # rồi tự dispatch một lần ở đây.
        if self.env.context.get("myco_in_mark_as_done"):
            return super().action_mark_as_done()
        res = super(
            MrpWorkorder, self.with_context(myco_in_mark_as_done=True)
        ).action_mark_as_done()
        if not self.env.context.get("myco_skip_flow"):
            for wo in self:
                wo._myco_dispatch_after_done()
        return res

    # ------------------------------------------------------------------
    # Cross-MO triggers
    # ------------------------------------------------------------------
    def _myco_start_first_child_workorders(self):
        """Khi WO Quản lý MO tổng start → start WO đầu (ready) của mỗi MO con.

        WO đầu mỗi MO con luôn ở 'ready' (không có blocked_by). Các WO sau
        được Odoo + module Quality tự đẩy tuần tự nên không cần xử lý ở đây.
        """
        self.ensure_one()
        started = self.env["mrp.workorder"]
        for child in self.production_id._myco_get_child_productions():
            first_wo = child.workorder_ids.filtered(
                lambda wo: wo.state in WO_STARTABLE_STATES
            ).sorted(lambda wo: (wo.sequence, wo.id))[:1]
            if first_wo and first_wo._myco_auto_start(reason="manager_start"):
                started |= first_wo
        _logger.info(
            "Manager %s auto-started: %s",
            self.display_name,
            started.mapped("display_name") or "none",
        )
        return bool(started)

    def _myco_check_manager_can_finish(self):
        self.ensure_one()
        production = self.production_id
        if not production._myco_get_child_productions():
            return
        pending = production._myco_get_flow_workorders().filtered(
            lambda wo: wo.id != self.id and wo.state not in WO_TERMINAL_STATES
        )
        if pending:
            raise UserError(
                _(
                    "Chưa thể Finish Quản lý. Công đoạn chưa Done: %(names)s",
                    names=", ".join(pending.mapped("display_name")),
                )
            )

    def _myco_dispatch_after_done(self):
        """Callback cross-MO sau khi WO done.

        - WO thuộc MO con → tự start WO kế tiếp trong CÙNG MO con, rồi báo
          MO tổng kiểm tra start Gia công.
        - WO Gia công (MO tổng) done → start Đóng gói.
        - WO Đóng gói (MO tổng) done → finish Quản lý.
        """
        self.ensure_one()
        if self.state != "done":
            return False

        production = self.production_id
        sources = production._myco_get_source_productions()
        if sources:
            self._myco_start_next_in_same_mo()
            for master in sources:
                master._myco_start_assembly_when_children_done()
            return True
        if self.flow_role == "assembly":
            return production._myco_start_packing_when_assembly_done()
        if self.flow_role == "packing":
            return production._myco_finish_manager_when_packing_done()
        return False

    def _myco_start_next_in_same_mo(self):
        """Tự start WO kế tiếp (ready) trong CÙNG một MO sau khi WO này done.

        Native Odoo chỉ đẩy WO sau sang 'ready' chứ không tự start, nên cần
        helper này để chuỗi WO trong một MO chạy nối tiếp liên tục.
        """
        self.ensure_one()
        next_wo = self._myco_get_next_startable_workorder_in_mo()
        return bool(next_wo) and next_wo._myco_auto_start(
            reason="previous_workorder_done"
        )

    def _myco_get_next_startable_workorder_in_mo(self):
        self.ensure_one()
        workorders = self.production_id.workorder_ids.sorted(
            lambda wo: (wo.sequence, wo.id)
        )
        ids = workorders.ids
        if self.id not in ids:
            return self.env["mrp.workorder"]
        for wo in workorders[ids.index(self.id) + 1:]:
            if wo.state in WO_TERMINAL_STATES:
                continue
            if wo.state == WO_RUNNING_STATE:
                return self.env["mrp.workorder"]
            if wo.state in WO_STARTABLE_STATES:
                return wo
            if wo.state == "blocked":
                _logger.info("Next WO %s blocked; skip.", wo.display_name)
                return self.env["mrp.workorder"]
        return self.env["mrp.workorder"]

    def _myco_auto_start(self, reason=""):
        self.ensure_one()
        if self.state in WO_TERMINAL_STATES or self.state == WO_RUNNING_STATE:
            return False
        if self.state == "blocked":
            _logger.info("WO %s blocked; skip auto-start.", self.display_name)
            return False
        if self.state not in WO_STARTABLE_STATES:
            _logger.info(
                "WO %s state=%s not startable.", self.display_name, self.state
            )
            return False
        if self.production_id.state in ("done", "cancel"):
            return False
        try:
            self.with_context(myco_skip_flow=True).button_start(
                raise_on_invalid_state=True
            )
        except UserError:
            raise
        except Exception as err:  # noqa: BLE001
            _logger.exception("Auto-start failed WO %s: %s", self.display_name, err)
            raise UserError(
                _(
                    "Không thể auto-start %(wo)s: %(err)s",
                    wo=self.display_name,
                    err=str(err),
                )
            )
        _logger.info("Auto-started %s reason=%s", self.display_name, reason or "-")
        return True
