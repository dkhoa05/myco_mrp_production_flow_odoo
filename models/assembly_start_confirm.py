"""Wizard cảnh báo MỀM — hộp thoại "Tiếp tục/Hủy" khi thao tác sớm trong flow.

VAI TRÒ FILE: TransientModel hiện popup xác nhận (KHÔNG block cứng). Thuộc mục
"CẢNH BÁO MỀM" của SƠ ĐỒ FLOW TỔNG ở models/mrp_workorder.py.

LUỒNG (step → hàm → biến):
  1. mrp_workorder._myco_open_flow_confirm(mode, pending) TẠO record wizard này khi:
       • Start "Gia công" lúc WO con chưa Done.
       • Start "Đóng gói" lúc WO con / Gia công chưa Done.
       • Finish "Quản lý" / "Đóng gói" lúc còn WO chưa Done.
     → set biến: workorder_id (WO đích), mode ('start'/'finish'), message,
       pending_names (danh sách WO chưa xong).
  2. Form (views/assembly_start_confirm_views.xml) hiện `message` + `pending_names`.
  3. "Tiếp tục" → action_confirm() chạy lại button_start/button_finish của WO với
     context `myco_flow_confirmed=True` (bỏ qua cảnh báo lần này). "Hủy" = không làm gì.
"""

from odoo import fields, models


class MycoAssemblyStartConfirm(models.TransientModel):
    """Wizard xác nhận MỀM dùng chung cho start Gia công/Đóng gói và finish
    Quản lý/Đóng gói. Xem flow chi tiết ở docstring đầu file."""

    _name = "myco.assembly.start.confirm"
    _description = "Xác nhận công đoạn sản xuất khi còn WO chưa xong"

    workorder_id = fields.Many2one(
        "mrp.workorder", string="Công đoạn", required=True, readonly=True
    )
    mode = fields.Selection(
        [("start", "Bắt đầu"), ("finish", "Hoàn tất")],
        string="Thao tác", default="start", required=True, readonly=True,
    )
    message = fields.Char(string="Cảnh báo", readonly=True)
    pending_names = fields.Text(string="Công đoạn chưa xong", readonly=True)

    def action_confirm(self):
        """Đồng ý → start/finish công đoạn, bỏ qua cảnh báo (myco_flow_confirmed)."""
        self.ensure_one()
        wo = self.workorder_id.with_context(myco_flow_confirmed=True)
        if self.mode == "finish":
            return wo.button_finish()
        return wo.button_start()
