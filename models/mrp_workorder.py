"""MRP Production Flow — điều phối TỰ ĐỘNG chuỗi Work Order qua nhiều MO.

═══════════════════════════════════════════════════════════════════════════════
SƠ ĐỒ FLOW NGHIỆP VỤ  (auto-orchestration)
═══════════════════════════════════════════════════════════════════════════════
Mỗi WO có `flow_role`: manager (Quản lý) / assembly (Gia công) /
packing (Đóng gói) / none. MO tổng (master) giữ các WO master (manager,
assembly, packing) và sinh các MO con (child) qua route Manufacture.

  [User bấm Start WO "Quản lý" của MO tổng]
        │  button_start()                         ← override, hook flow vào
        ▼
  (1) _myco_start_first_child_workorders()        nếu role=manager + có MO con
        │   mỗi MO con: WO đầu (state 'ready') → _myco_auto_start()
        ▼
  [Mỗi WO Done (con hay master) → button_finish()/action_mark_as_done()
        → _myco_dispatch_after_done()]
        ▼
  (2) _myco_dispatch_after_done()
        ├─ WO con  → _myco_start_next_in_same_mo() (WO kế trong CÙNG MO con)
        │            + master._myco_advance_flow()
        └─ WO master → production._myco_advance_flow()
        ▼
  (3) _myco_advance_flow()  [mrp_production.py] — IDEMPOTENT, tự kiểm điều kiện:
        (a) _myco_start_assembly_when_children_done() → start "Gia công"
                khi MỌI WO của MO con đã Done.
        (b) _myco_start_packing_when_assembly_done()  → start "Đóng gói"
                khi "Gia công" đã Done.
        (c) _myco_finish_manager_when_done()          → finish "Quản lý"
                khi MỌI WO khác (MO + MO con) đã Done. ⇐ chính là điều kiện
                "Đóng gói xong & không còn WO nào chạy".
        ▼
  [Flow hoàn tất]   (gọi advance_flow nhiều lần / lệch thứ tự đều an toàn)

CẢNH BÁO MỀM (wizard myco.assembly.start.confirm — KHÔNG block, liệt kê WO chưa xong):
  • button_start() "Gia công" khi WO con chưa Done.
  • button_start() "Đóng gói" khi WO con / Gia công chưa Done.
  • button_finish() "Quản lý" khi còn WO khác chưa Done.
  • button_finish() "Đóng gói" khi còn WO flow khác chưa Done (TRỪ Quản lý — vì
        Đóng gói là bước CHỐT CUỐI, done xong sẽ TỰ done Quản lý).
  → _myco_open_flow_confirm(mode, pending); "Tiếp tục" = làm lại với myco_flow_confirmed.

GUARD CỨNG (còn lại):
  • button_mark_done() MO tổng → _myco_check_flow_done_before_close()
      [mrp_production.py]: chặn ĐÓNG MO khi còn WO chưa Done.

CỜ CONTEXT (chống đệ quy / bỏ qua cảnh báo):
  • myco_skip_flow=True      : _myco_auto_start() đặt khi gọi lại button_start,
                               để KHÔNG kích hoạt flow lần nữa (chống đệ quy).
  • myco_flow_confirmed=True : wizard đặt khi user bấm "Tiếp tục" → bỏ qua cảnh báo mềm.
  • myco_in_mark_as_done=True: action_mark_as_done() đặt, để button_finish()
                               bên trong KHÔNG dispatch sớm — dispatch 1 lần ngoài.

HÀM AN TOÀN:
  • _myco_auto_start(reason): MỌI auto-start đi qua đây; kiểm state rồi gọi
      button_start(myco_skip_flow=True).
  • _myco_get_next_startable_workorder_in_mo(): WO 'ready' kế tiếp trong MO
      (sorted theo (sequence, id)); gặp WO đang chạy/blocked thì dừng.

HẰNG SỐ (const.py):
  • WO_STARTABLE_STATES=('ready',)  • WO_RUNNING_STATE='progress'
  • WO_TERMINAL_STATES=('done','cancel')

HELPER trên mrp.production (mrp_production.py):
  • _myco_get_child_productions()  : MO con   (qua native _get_children()).
  • _myco_get_source_productions() : MO tổng  (qua native _get_sources()).
  • _myco_get_flow_workorders()    : toàn bộ WO của MO + MO con (trừ cancel).
  • _myco_get_workorder_by_role(r) : WO master theo role.
═══════════════════════════════════════════════════════════════════════════════
"""

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
        """STEP 1 — hook flow vào nút Start native.

        - CẢNH BÁO MỀM (không block): Start "Gia công" khi WO con chưa xong, hoặc
          Start "Đóng gói" khi WO con/Gia công chưa xong → mở wizard liệt kê WO
          chưa xong. "Tiếp tục" → start lại với cờ myco_flow_confirmed.
        - super() xử lý state. WO "Quản lý" + có MO con →
          _myco_start_first_child_workorders(); Start WO con khi Quản lý chưa chạy
          → _myco_start_manager_from_child(). Bỏ qua flow khi `myco_skip_flow`.
        """
        if not self.env.context.get("myco_skip_flow") and not self.env.context.get(
            "myco_flow_confirmed"
        ):
            for wo in self:
                pending = wo._myco_flow_pending_before_start()
                if pending:
                    return wo._myco_open_flow_confirm("start", pending)
        res = super().button_start(raise_on_invalid_state=raise_on_invalid_state)
        if self.env.context.get("myco_skip_flow"):
            return res
        for wo in self:
            if (
                wo.flow_role == "manager"
                and wo.production_id._myco_get_child_productions()
            ):
                wo._myco_start_first_child_workorders()
            else:
                # Khởi động flow TỪ MO con: nếu start 1 WO của MO con mà Quản lý
                # chưa chạy → auto-start Quản lý + WO đầu các MO con khác (song song).
                wo._myco_start_manager_from_child()
        return res

    def button_finish(self):
        """Hook nút Finish native + dispatch flow.

        - Trước super(): Finish WO "Quản lý" / "Đóng gói" khi còn WO chưa Done →
          mở wizard xác nhận MỀM (liệt kê WO chưa xong), KHÔNG block. "Tiếp tục"
          → finish lại với cờ myco_flow_confirmed.
        - Sau super(): mỗi WO → _myco_dispatch_after_done() — TRỪ khi đang trong
          action_mark_as_done (cờ `myco_in_mark_as_done`) để khỏi dispatch 2 lần.
        """
        if not self.env.context.get("myco_skip_flow") and not self.env.context.get(
            "myco_flow_confirmed"
        ):
            for wo in self:
                pending = wo._myco_finish_pending()
                if pending:
                    return wo._myco_open_flow_confirm("finish", pending)
        res = super().button_finish()
        if not self.env.context.get("myco_skip_flow") and not self.env.context.get(
            "myco_in_mark_as_done"
        ):
            for wo in self:
                wo._myco_dispatch_after_done()
        return res

    def action_mark_as_done(self):
        """Hook "Mark as Done" native (bên trong có gọi button_finish()).

        Đặt context `myco_in_mark_as_done` để button_finish() KHÔNG dispatch sớm,
        rồi tự gọi _myco_dispatch_after_done() đúng MỘT lần ở đây.
        """
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

    def _myco_start_manager_from_child(self):
        """Start một WO của MO con khi Quản lý CHƯA chạy → auto-start Quản lý
        rồi cascade WO đầu MỌI MO con. Giúp flow chạy song song dù khởi động từ
        MO con thay vì từ Quản lý (WO đang được user start sẽ tự bỏ qua vì đã chạy).
        """
        self.ensure_one()
        for master in self.production_id._myco_get_source_productions():
            manager = master._myco_get_workorder_by_role("manager")
            if manager and manager.state in WO_STARTABLE_STATES:
                if manager._myco_auto_start(reason="child_workorder_started"):
                    manager._myco_start_first_child_workorders()

    def _myco_flow_pending_before_start(self):
        """WO cần xong TRƯỚC khi Start WO này (cho cảnh báo MỀM). Empty = không cảnh báo.

          - Gia công (assembly): mọi WO con phải Done.
          - Đóng gói (packing) : mọi WO con + Gia công phải Done.
          - Vai trò khác        : không cảnh báo.
        """
        self.ensure_one()
        prod = self.production_id
        if self.flow_role == "assembly":
            pend = prod._myco_get_child_productions().mapped("workorder_ids")
        elif self.flow_role == "packing":
            pend = prod._myco_get_child_productions().mapped("workorder_ids")
            pend |= prod._myco_get_workorder_by_role("assembly")
        else:
            return self.env["mrp.workorder"]
        return pend.filtered(lambda wo: wo.state not in WO_TERMINAL_STATES)

    def _myco_finish_pending(self):
        """WO cần xong TRƯỚC khi Finish WO này (cảnh báo MỀM). Empty = không cảnh báo.

          - Quản lý (manager): MỌI WO flow khác (MO + MO con) chưa Done.
          - Đóng gói (packing): MỌI WO flow khác TRỪ "Quản lý" — vì Đóng gói là
              bước CHỐT CUỐI, done xong sẽ TỰ done Quản lý (xem
              _myco_finish_manager_when_done). Nên Quản lý không tính vào điều kiện.
          - Vai trò khác: không cảnh báo.
        """
        self.ensure_one()
        production = self.production_id
        if self.flow_role == "manager":
            if not production._myco_get_child_productions():
                return self.env["mrp.workorder"]
            candidates = production._myco_get_flow_workorders().filtered(
                lambda wo: wo.id != self.id
            )
        elif self.flow_role == "packing":
            manager = production._myco_get_workorder_by_role("manager")
            candidates = production._myco_get_flow_workorders().filtered(
                lambda wo: wo.id != self.id and wo not in manager
            )
        else:
            return self.env["mrp.workorder"]
        return candidates.filtered(lambda wo: wo.state not in WO_TERMINAL_STATES)

    def _myco_open_flow_confirm(self, mode, pending):
        """Mở wizard cảnh báo MỀM (mode='start'/'finish') liệt kê WO chưa xong.

        KHÔNG block: user bấm "Tiếp tục" → start/finish lại với cờ myco_flow_confirmed.
        """
        self.ensure_one()
        verb = "hoàn tất" if mode == "finish" else "chạy"
        wizard = self.env["myco.assembly.start.confirm"].create({
            "workorder_id": self.id,
            "mode": mode,
            "message": "Còn công đoạn chưa hoàn tất. Vẫn %s '%s'?" % (
                verb, self.display_name,
            ),
            "pending_names": "\n".join(
                "• " + name for name in pending.mapped("display_name")
            ),
        })
        return {
            "type": "ir.actions.act_window",
            "name": "Xác nhận công đoạn sản xuất",
            "res_model": "myco.assembly.start.confirm",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def _myco_dispatch_after_done(self):
        """Callback cross-MO sau khi MỘT WO Done.

        - WO thuộc MO con → start WO kế tiếp trong CÙNG MO con, rồi gọi
          _myco_advance_flow() trên (các) MO tổng.
        - WO master (Quản lý/Gia công/Đóng gói) → gọi _myco_advance_flow() trên
          chính MO tổng.

        _myco_advance_flow() tự kiểm điều kiện từng bước (xem hàm đó), idempotent,
        nên thứ tự thao tác KHÔNG quan trọng và gọi nhiều lần vẫn an toàn.
        """
        self.ensure_one()
        if self.state != "done":
            return False

        production = self.production_id
        sources = production._myco_get_source_productions()
        if sources:
            self._myco_start_next_in_same_mo()
            for master in sources:
                master._myco_advance_flow()
            return True
        production._myco_advance_flow()
        return True

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
        """WO 'ready' kế tiếp trong CÙNG MO sau WO này (sorted theo (sequence, id)).

        Trả empty recordset nếu: self không nằm trong list, gặp WO đang chạy/blocked
        trước đó, hoặc hết WO 'ready'. Dùng bởi _myco_start_next_in_same_mo().
        """
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
        """Auto-start AN TOÀN một WO. MỌI auto-start trong flow đều đi qua đây.

        Bỏ qua nếu state done/cancel/running/blocked, không 'ready', hoặc MO đã
        done/cancel. Gọi button_start(myco_skip_flow=True) để KHÔNG kích hoạt flow
        lần nữa (chống đệ quy). `reason` chỉ dùng cho log. Trả True nếu đã start.
        """
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
