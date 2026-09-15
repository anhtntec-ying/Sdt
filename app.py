"""
App tra cứu địa chỉ + số điện thoại doanh nghiệp từ mã số thuế (MST)
Nguồn: VietQR (API, ổn định) và masothue.com (đọc trang web, có thể bị chặn)
Chạy thử trên máy:  streamlit run app.py
"""
import io
import re
import time

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# cloudscraper giúp vượt lớp chống bot của Cloudflare (masothue dùng lớp này)
try:
    import cloudscraper
    MST_SESSION = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
except Exception:
    MST_SESSION = requests.Session()
    MST_SESSION.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
MST_SESSION.headers["Accept-Language"] = "vi-VN,vi;q=0.9"

st.set_page_config(page_title="Tra cứu MST", page_icon="🔎", layout="wide")


# ----------------------------------------------------------------------
# 1. Chuẩn hóa MST (Excel hay làm mất số 0 đầu, thêm ".0" ở cuối)
# ----------------------------------------------------------------------
def chuan_hoa_mst(x):
    if pd.isna(x):
        return None
    s = str(x).strip().replace(" ", "")
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    s = re.sub(r"[^\d-]", "", s)
    if "-" in s:
        goc, _, duoi = s.partition("-")
    elif len(s) == 13:  # MST chi nhánh viết liền 13 số
        goc, duoi = s[:10], s[10:]
    else:
        goc, duoi = s, ""
    if len(goc) == 9:  # mất số 0 đầu
        goc = "0" + goc
    if len(goc) != 10:
        return None
    return f"{goc}-{duoi}" if duoi else goc


# ----------------------------------------------------------------------
# 2. Tra VietQR: trả tên + địa chỉ (KHÔNG có số điện thoại)
# ----------------------------------------------------------------------
def tra_vietqr(mst):
    out = {"Tên DN (VietQR)": "", "Địa chỉ (VietQR)": "", "Kết quả VietQR": ""}
    loi = ""
    for lan in range(3):
        try:
            r = requests.get(f"https://api.vietqr.io/v2/business/{mst}", timeout=15)
            if r.status_code == 429:  # gọi quá nhanh -> chờ rồi thử lại
                time.sleep(5 * (lan + 1))
                continue
            j = r.json()
            if j.get("code") == "00" and j.get("data"):
                d = j["data"]
                out["Tên DN (VietQR)"] = d.get("name") or ""
                out["Địa chỉ (VietQR)"] = d.get("address") or ""
                out["Kết quả VietQR"] = "OK"
            else:
                out["Kết quả VietQR"] = j.get("desc") or "Không tìm thấy"
            return out
        except Exception as e:
            loi = str(e)[:80]
            time.sleep(2)
    out["Kết quả VietQR"] = f"Lỗi: {loi or 'bị giới hạn tốc độ'}"
    return out


# ----------------------------------------------------------------------
# 3. Tra masothue.com: tên, địa chỉ, SĐT, tình trạng
# ----------------------------------------------------------------------
def _doc_trang(url, **kw):
    r = MST_SESSION.get(url, timeout=20, **kw)
    bi_chan = r.status_code in (403, 429, 503) or "Just a moment" in r.text[:3000]
    return r, bi_chan


def tra_masothue(mst):
    out = {
        "Tên DN (masothue)": "",
        "Địa chỉ (masothue)": "",
        "SĐT (masothue)": "",
        "Tình trạng (masothue)": "",
        "Kết quả masothue": "",
    }
    try:
        r, bi_chan = _doc_trang(
            "https://masothue.com/Search/", params={"q": mst, "type": "auto"}
        )
        if bi_chan:
            out["Kết quả masothue"] = f"Bị chặn ({r.status_code})"
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        bang = soup.select_one("table.table-taxinfo")

        # Nếu ra trang danh sách kết quả -> mở link đầu tiên khớp MST
        if bang is None:
            link = soup.select_one(f'a[href^="/{mst}-"]')
            if link is None:
                out["Kết quả masothue"] = "Không tìm thấy"
                return out
            r, bi_chan = _doc_trang("https://masothue.com" + link["href"])
            if bi_chan:
                out["Kết quả masothue"] = f"Bị chặn ({r.status_code})"
                return out
            soup = BeautifulSoup(r.text, "html.parser")
            bang = soup.select_one("table.table-taxinfo")
            if bang is None:
                out["Kết quả masothue"] = "Không đọc được trang"
                return out

        ten = bang.select_one("thead th") or soup.select_one("h1")
        out["Tên DN (masothue)"] = ten.get_text(" ", strip=True) if ten else ""

        for tr in bang.select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            nhan = tds[0].get_text(" ", strip=True).lower()
            gia_tri = tds[1].get_text(" ", strip=True)
            if "điện thoại" in nhan and not out["SĐT (masothue)"]:
                out["SĐT (masothue)"] = gia_tri
            elif "địa chỉ" in nhan and not out["Địa chỉ (masothue)"]:
                out["Địa chỉ (masothue)"] = gia_tri
            elif "tình trạng" in nhan and not out["Tình trạng (masothue)"]:
                out["Tình trạng (masothue)"] = gia_tri
        out["Kết quả masothue"] = "OK"
    except Exception as e:
        out["Kết quả masothue"] = f"Lỗi: {str(e)[:80]}"
    return out


# ----------------------------------------------------------------------
# 4. Giao diện
# ----------------------------------------------------------------------
st.title("Tra cứu địa chỉ và số điện thoại theo MST")

with st.sidebar:
    st.header("Cài đặt")
    nguon = st.multiselect(
        "Nguồn tra cứu", ["VietQR", "masothue"], default=["VietQR", "masothue"],
        help="VietQR chỉ có tên + địa chỉ. Số điện thoại chỉ lấy được từ masothue.",
    )
    nghi = st.slider(
        "Nghỉ giữa mỗi lần tra (giây)", 0.5, 5.0, 1.5, 0.5,
        help="Nghỉ lâu hơn thì ít bị chặn hơn nhưng chạy chậm hơn.",
    )

tab_file, tab_dan = st.tabs(["Tải file lên", "Dán danh sách"])
df_goc, cot_mst = None, None

with tab_file:
    f = st.file_uploader("File Excel hoặc CSV có cột MST", type=["xlsx", "xls", "csv"])
    if f is not None:
        if f.name.lower().endswith(".csv"):
            df_goc = pd.read_csv(f, dtype=str, encoding="utf-8-sig")
        else:
            df_goc = pd.read_excel(f, dtype=str)
        goi_y = next(
            (c for c in df_goc.columns
             if any(k in str(c).lower() for k in ["mst", "thuế", "thue", "tax"])),
            df_goc.columns[0],
        )
        cot_mst = st.selectbox(
            "Cột chứa MST", df_goc.columns, index=list(df_goc.columns).index(goi_y)
        )
        st.dataframe(df_goc.head(), use_container_width=True)

with tab_dan:
    van_ban = st.text_area("Mỗi dòng một MST", height=150)
    if van_ban.strip() and df_goc is None:
        dong = [x.strip() for x in van_ban.splitlines() if x.strip()]
        df_goc, cot_mst = pd.DataFrame({"MST": dong}), "MST"

if df_goc is not None:
    df_goc = df_goc.copy()
    df_goc["MST chuẩn hóa"] = df_goc[cot_mst].apply(chuan_hoa_mst)
    ds_mst = df_goc["MST chuẩn hóa"].dropna().unique().tolist()
    so_loi = df_goc["MST chuẩn hóa"].isna().sum()

    uoc_tinh = len(ds_mst) * (nghi + 1) * max(len(nguon), 1) / 60
    st.info(
        f"{len(ds_mst):,} MST hợp lệ (đã bỏ trùng), {so_loi:,} dòng MST không hợp lệ. "
        f"Thời gian ước tính khoảng {uoc_tinh:,.0f} phút — giữ tab mở trong lúc chạy."
    )

    if st.button("Bắt đầu tra cứu", type="primary", disabled=not nguon):
        st.session_state["ket_qua"] = {}
        thanh = st.progress(0.0)
        dong_trang_thai = st.empty()
        for i, mst in enumerate(ds_mst, 1):
            dong = {"MST chuẩn hóa": mst}
            if "VietQR" in nguon:
                dong.update(tra_vietqr(mst))
                time.sleep(nghi)
            if "masothue" in nguon:
                dong.update(tra_masothue(mst))
                time.sleep(nghi)
            # lưu dần từng dòng: bấm Stop giữa chừng vẫn giữ được phần đã tra
            st.session_state["ket_qua"][mst] = dong
            thanh.progress(i / len(ds_mst))
            dong_trang_thai.write(f"Đã tra {i:,}/{len(ds_mst):,} — MST vừa tra: {mst}")
        dong_trang_thai.success("Tra cứu xong.")

    if st.session_state.get("ket_qua"):
        df_kq = pd.DataFrame(st.session_state["ket_qua"].values())
        df_xuat = df_goc.merge(df_kq, on="MST chuẩn hóa", how="left")

        st.subheader(f"Kết quả ({len(df_kq):,} MST đã tra)")
        for cot in ["Kết quả VietQR", "Kết quả masothue"]:
            if cot in df_kq:
                st.caption(f"{cot}: " + ", ".join(
                    f"{k}: {v:,}" for k, v in df_kq[cot].value_counts().items()
                ))
        st.dataframe(df_xuat, use_container_width=True)

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df_xuat.to_excel(w, index=False, sheet_name="Ket_qua")
        st.download_button(
            "Tải file Excel kết quả", buf.getvalue(),
            file_name="ket_qua_tra_cuu_mst.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
else:
    st.write("Tải file lên hoặc dán danh sách MST để bắt đầu.")
