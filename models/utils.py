import unicodedata


def normalize_vn(text):
    """Chuẩn hóa chuỗi tiếng Việt về ASCII không dấu, chữ thường.

    Dùng để so khớp tên WO / workcenter / operation khi auto-detect
    flow_role (ví dụ: "Quản lý" -> "quan ly", "Đóng gói" -> "dong goi").
    """
    text = (text or "").lower().replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))
