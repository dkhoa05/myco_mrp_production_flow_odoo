from odoo import fields, models


class MycoAssemblyStartConfirm(models.TransientModel):
    """Wizard cảnh báo MỀM cho công đoạn sản xuất khi còn WO chưa xong.

    Dùng chung cho 3 tình huống (KHÔNG block):
      - Start "Gia công" khi WO con chưa Done.
      - Start "Đóng gói" khi WO con / Gia công chưa Done.
      - Finish "Quản lý" khi còn WO chưa Done.
    Hiện hộp thoại liệt kê WO chưa xong; "Tiếp tục" → start/finish với cờ
    myco_flow_confirmed (bỏ qua cảnh báo lần này); "Hủy" → không làm gì.
    """

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
