"""mrp.production — nửa MASTER MO của Production Flow (điều phối phía MO).

VAI TRÒ FILE: gán vai trò WO khi confirm, cung cấp helper Source/Child MO, và
"đẩy" flow tiến lên mỗi khi một WO Done. SƠ ĐỒ FLOW TỔNG (đầy đủ) nằm ở
models/mrp_workorder.py — đọc file đó trước; file này chỉ là các bước phía MO.

HÀM CHÍNH theo bước flow (step → hàm → biến):
  • action_confirm() → _myco_auto_detect_flow_roles()
        Gán field `flow_role` theo TÊN WO (chuẩn hoá qua utils.normalize_vn):
        'quan ly'→manager, 'gia cong'/'nhung'/'lanh'→assembly, 'dong goi'→packing.
  • _myco_advance_flow()  [IDEMPOTENT — gọi sau mỗi WO Done từ
        mrp_workorder._myco_dispatch_after_done()]. Tự kiểm điều kiện, chạy 3 bước:
        (a) _myco_start_assembly_when_children_done() — MỌI WO con Done → start Gia công.
        (b) _myco_start_packing_when_assembly_done()  — Gia công Done → start Đóng gói.
        (c) _myco_finish_manager_when_done()          — MỌI WO khác Done → finish Quản lý
            (cũng là lúc Đóng gói Done kéo theo Quản lý Done).
  • button_mark_done() → _myco_check_flow_done_before_close()
        GUARD CỨNG: chặn đóng MO tổng khi còn WO chưa Done.
  • action_get_production_flow_data() — RPC single-payload cho OWL widget (prefetch
        chống N+1 rồi build dict master/children/master_workorders).

HELPER Source/Child (bọc quan hệ native, đều cần ensure_one):
  • _myco_get_child_productions()  : MO con  (native _get_children()).
  • _myco_get_source_productions() : MO tổng (native _get_sources()).
  • _myco_get_flow_workorders()    : toàn bộ WO của MO + MO con (trừ cancel).
  • _myco_get_workorder_by_role(r) : WO master đầu tiên theo role (sorted sequence,id).

BIẾN/QUAN HỆ chính: production_group_id (gốc quan hệ Source/Child native),
flow_role (mrp.workorder), hằng trạng thái ở const.py.
"""

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
        """Tính myco_flow_state + myco_flow_progress (hiển thị trên widget/UI).

        `children`=MO con, `workorders`=WO của MO + MO con (trừ cancel).
        progress = trung bình progress các WO; state suy từ trạng thái WO:
        empty (không có) / ready / running / blocked / done.
        """
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
        """Override: sau khi confirm MO → _myco_auto_detect_flow_roles() gán role."""
        res = super().action_confirm()
        self._myco_auto_detect_flow_roles()
        return res

    # ------------------------------------------------------------------
    # Helpers — wrap native Source/Child MO logic
    # ------------------------------------------------------------------
    def _myco_get_child_productions(self):
        """MO con (qua native _get_children()), bỏ chính nó và MO cancel."""
        self.ensure_one()
        return self._get_children().filtered(
            lambda mo: mo.id != self.id and mo.state != "cancel"
        )

    def _myco_get_source_productions(self):
        """MO tổng/nguồn (qua native _get_sources()), bỏ chính nó và MO cancel."""
        self.ensure_one()
        return self._get_sources().filtered(
            lambda mo: mo.id != self.id and mo.state != "cancel"
        )

    def _myco_get_flow_workorders(self, children=False):
        """Toàn bộ WO của MO này + MO con (trừ cancel).

        `children` truyền sẵn để khỏi tính lại (tối ưu khi caller đã có).
        """
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
        """WO master ĐẦU TIÊN theo `role` (manager/assembly/packing),
        sorted (sequence, id), bỏ cancel."""
        self.ensure_one()
        return self.workorder_ids.filtered(
            lambda wo: wo.flow_role == role and wo.state != "cancel"
        ).sorted(lambda wo: (wo.sequence, wo.id))[:1]

    # ------------------------------------------------------------------
    # Auto-detect flow_role on confirm
    # ------------------------------------------------------------------
    def _myco_auto_detect_flow_roles(self):
        """Gán flow_role cho từng WO theo TÊN (chuẩn hoá không dấu qua normalize_vn).

        'quan ly'→manager; 'gia cong'/'nhung'/'lanh'→assembly; 'dong goi'→packing.
        WO đã có role (!= 'none') thì bỏ qua. Gọi từ action_confirm().
        """
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
        """STEP 2→3: khi TẤT CẢ WO của MO con đã Done → auto-start WO "Gia công".

        `child_wos` = WO mọi MO con (trừ cancel); còn WO != done → return False.
        Gọi từ _myco_dispatch_after_done() của một WO con vừa Done.
        """
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
        """STEP 3→4: WO "Gia công" Done → auto-start WO "Đóng gói".

        Gọi từ _myco_dispatch_after_done() của WO assembly (MO tổng) vừa Done.
        """
        self.ensure_one()
        assembly = self._myco_get_workorder_by_role("assembly")
        if assembly and assembly.state != "done":
            return False
        packing = self._myco_get_workorder_by_role("packing")
        if not packing:
            _logger.info("MO %s: no packing WO.", self.display_name)
            return False
        return packing._myco_auto_start(reason="assembly_done")

    def _myco_finish_manager_when_done(self):
        """Finish WO "Quản lý" khi MỌI WO khác (MO tổng + MO con) đã Done/Cancel.

        Điều kiện "mọi WO done" đã BAO HÀM "Đóng gói xong" (Đóng gói là một trong
        các WO đó) — nên không cần kiểm packing riêng. Còn BẤT KỲ WO chưa xong (vd
        Sơ chế của MO con đang chạy) → KHÔNG finish. button_finish dùng
        myco_skip_flow để khỏi kích hoạt flow lần nữa.
        """
        self.ensure_one()
        manager = self._myco_get_workorder_by_role("manager")
        if not manager or manager.state in WO_TERMINAL_STATES:
            return False
        pending = self._myco_get_flow_workorders().filtered(
            lambda wo: wo.id != manager.id and wo.state not in WO_TERMINAL_STATES
        )
        if pending:
            _logger.info(
                "MO %s: chưa finish Quản lý, còn WO chưa xong: %s",
                self.display_name, pending.mapped("display_name"),
            )
            return False
        manager.with_context(myco_skip_flow=True).button_finish()
        _logger.info("Auto-finished manager %s.", manager.display_name)
        return True

    def _myco_advance_flow(self):
        """Đẩy flow tự động sau khi MỘT WO bất kỳ Done — IDEMPOTENT, không phụ thuộc
        thứ tự thao tác. Mỗi bước tự kiểm điều kiện, chỉ chạy khi đủ:

          (a) start Gia công  — khi MỌI WO của MO con đã Done.
          (b) start Đóng gói  — khi Gia công đã Done.
          (c) finish Quản lý  — khi MỌI WO khác (MO + MO con) đã Done.

        Gọi nhiều lần vẫn an toàn. Gọi từ _myco_dispatch_after_done() trên MO tổng.
        """
        self.ensure_one()
        self._myco_start_assembly_when_children_done()   # (a)
        self._myco_start_packing_when_assembly_done()    # (b)
        self._myco_finish_manager_when_done()            # (c)

    # ------------------------------------------------------------------
    # Validation — chặn đóng MO tổng khi còn WO chưa done
    # ------------------------------------------------------------------
    def _myco_check_flow_done_before_close(self):
        """GUARD: chặn hoàn tất MO tổng khi còn WO (MO + MO con) chưa Done.

        Không có MO con → bỏ qua. `pending` != done/cancel → raise UserError.
        Gọi từ button_mark_done().
        """
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
        """Override: trước khi đóng MO → _myco_check_flow_done_before_close()
        (trừ khi context myco_skip_flow)."""
        if not self.env.context.get("myco_skip_flow"):
            for production in self:
                production._myco_check_flow_done_before_close()
        return super().button_mark_done()

    # ------------------------------------------------------------------
    # RPC cho OWL widget — single payload
    # ------------------------------------------------------------------
    def action_get_production_flow_data(self):
        """RPC single-payload cho OWL widget Production Flow.

        Trả dict: master {flow_state, flow_progress, child_count}, children[]
        (mỗi MO con + workorders[]), master_workorders[]. Prefetch field/quan hệ
        trước để tránh N+1. wo_payload() chuẩn hoá 1 WO thành dict cho JS.
        """
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
