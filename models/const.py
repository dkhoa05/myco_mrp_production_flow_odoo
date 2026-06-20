# Trạng thái workorder dùng chung cho toàn module.
# Tách riêng để tránh duplicate hằng số giữa các file model.

# WO đã kết thúc — không thể start lại, bỏ qua khi xét tiến độ flow.
WO_TERMINAL_STATES = ("done", "cancel")

# WO đang chạy.
WO_RUNNING_STATE = "progress"

# WO sẵn sàng để start (Odoo tự set 'ready' khi blocked_by_workorder_ids done).
WO_STARTABLE_STATES = ("ready",)
