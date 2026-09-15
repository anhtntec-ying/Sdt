"""
App tra cứu địa chỉ + số điện thoại doanh nghiệp từ mã số thuế (MST)
Nguồn: VietQR (API, ổn định) và masothue.com (đọc trang web, có thể bị chặn)
Bản 3: tra nhiều MST cùng lúc (đa luồng), tự giảm tốc khi bị giới hạn
"""
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

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

# Session có "hồ" kết nối đủ lớn cho nhiều luồng
VQR_SESSION = requests.Session()
VQR_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20))

TIMEOUT = 10
TAT_SAU_N_LAN_CHAN = 5
SO_DONG_HIEN_THI = 200
MASOTHUE_TOI_DA_CUNG_LUC = 2  # masothue dễ chặn -> tối đa 2 yêu cầu cùng lúc

st.set_page_config(page_title="Tra cứu MST", page_icon="🔎", layout="wide")


class TrangThaiChung:
    """Thông tin dùng chung giữa các luồng trong 1 lần chạy."""
    def __init__(self):
        self.lock = threading.Lock()
        self.vqr_nghi_den = 0.0          # VietQR báo quá tải -> mọi luồng cùng chờ tới mốc này
        self.masothue_bat = True
        self.masothue_chan_lien_tiep = 0
        self.masothue_slot = threading.Semaphore(MASOTHUE_TOI_DA_CUNG_LUC)


# ---------------- Chuẩn hóa MST ----------------
def chuan_hoa_mst(x):
    if pd.isna(x):
        return None
    s = str(x).strip().replace(" ", "")
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    s = re.sub(r"[^\d-]", "", s)
    if "-" in s:
        goc, _, duoi = s.partition("-")
    elif len(s) == 13:
        goc, duoi = s[:10], s[10:]
    else:
        goc, duoi = s, ""
    if len(goc) == 9:
        goc = "0" + goc
    if len(goc) != 10:
        return None
    return f"{goc}-{duoi}" if duoi else goc


@st.cache_data(show_spinner=False)
def doc_file(noi_dung: bytes, ten_file: str):
    bio = io.BytesIO(noi_dung)
    if ten_file.lower().endswith(".csv"):
        return pd.read_csv(bio, dtype=str, encoding="utf-8-sig")
    return pd.read_excel(bio, dtype=str)


# ---------------- VietQR ----------------
def tra_vietqr(mst, tt):
    out = {"Tên DN (VietQR)": "", "Địa chỉ (VietQR)": "", "Kết quả VietQR": ""}
    loi = ""
    for lan in range(5):
        cho = tt.vqr_nghi_den - time.time()
        if cho > 0:
            time.sleep(cho)
        try:
            r = VQR_SESSION.get(f"https://api.vietqr.io/v2/business/{mst}", timeout=TIMEOUT)
            if r.status_code == 429:  # quá giới hạn -> cả nhóm cùng nghỉ
                with tt.lock:
                    tt.vqr_nghi_den = max(tt.vqr_nghi_den, time.time() + 2 * (lan + 1))
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
            time.sleep(1)
    out["Kết quả VietQR"] = f"Lỗi: {loi or 'bị giới hạn tốc độ'}"
    return out


# ---------------- masothue ----------------
def _doc_trang(url, **kw):
    r = MST_SESSION.get(url, timeout=TIMEOUT, **kw)
    bi_chan = r.status_code in (403, 429, 503) or "Just a moment" in r.text[:3000]
    return r, bi_chan


def _tra_masothue(mst):
    out = {
        "Tên DN (masothue)": "", "Địa chỉ (masothue)": "", "SĐT (masothue)": "",
        "Tình trạng (masothue)": "", "Kết quả masothue": "",
    }
    try:
        r, bi_chan = _doc_trang("https://masothue.com/Search/", params={"q": mst, "type": "auto"})
        if bi_chan:
            out["Kết quả masothue"] = f"Bị chặn ({r.status_code})"
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        bang = soup.select_one("table.table-taxinfo")
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


def tra_masothue(mst, tt, nghi):
    if not tt.masothue_bat:
        return {"Kết quả masothue": "Bỏ qua (đã tắt do bị chặn)"}
    with tt.masothue_slot:
        out = _tra_masothue(mst)
        if nghi:
            time.sleep(nghi)
    with tt.lock:
        if out["Kết quả masothue"].startswith(("Bị chặn", "Lỗi")):
            tt.masothue_chan_lien_tiep += 1
            if tt.masothue_chan_lien_tiep >= TAT_SAU_N_LAN_CHAN:
                tt.masothue_bat = False
        else:
            tt.masothue_chan_lien_tiep = 0
    return out


def tra_1_mst(mst, dung_vqr, dung_mst, tt, nghi):
    dong = {"MST chuẩn hóa": mst}
    if dung_vqr:
        dong.update(tra_vietqr(mst, tt))
    if dung_mst:
        dong.update(tra_masothue(mst, tt, nghi))
    return dong


# ---------------- Giao diện ----------------
st.title("Tra cứu địa chỉ và số điện thoại theo MST")

with st.sidebar:
    st.header("Cài đặt")
    nguon = st.multiselect(
        "Nguồn tra cứu", ["VietQR", "masothue"], default=["VietQR", "masothue"],
        help="VietQR chỉ có tên + địa chỉ. Số điện thoại chỉ lấy được từ masothue.",
    )
    so_luong = st.slider(
        "Số MST tra cùng lúc", 1, 10, 5,
        help="Càng cao càng nhanh. Nếu thấy nhiều dòng báo lỗi/giới hạn thì giảm xuống.",
    )
    nghi = st.slider("Nghỉ sau mỗi lần tra masothue (giây)", 0.0, 3.0, 0.5, 0.5,
                     help="Chỉ áp dụng cho masothue để đỡ bị chặn.")

tab_file, tab_dan = st.tabs(["Tải file lên", "Dán danh sách"])
df_goc, cot_mst = None, None

with tab_file:
    f = st.file_uploader("File Excel hoặc CSV có cột MST", type=["xlsx", "xls", "csv"])
    if f is not None:
        df_goc = doc_file(f.getvalue(), f.name)
        goi_y = next(
            (c for c in df_goc.columns
             if any(k in str(c).lower() for k in ["mst", "thuế", "thue", "tax"])),
            df_goc.columns[0],
        )
        cot_mst = st.selectbox("Cột chứa MST", df_goc.columns,
                               index=list(df_goc.columns).index(goi_y))
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
    so_loi = int(df_goc["MST chuẩn hóa"].isna().sum())
    st.info(f"{len(ds_mst):,} MST hợp lệ (đã bỏ trùng), {so_loi:,} dòng MST không hợp lệ. "
            "Giữ tab mở và không bấm gì khác trong lúc chạy.")

    if st.button("Bắt đầu tra cứu", type="primary", disabled=not nguon):
        st.session_state["ket_qua"] = {}
        tt = TrangThaiChung()
        thanh = st.progress(0.0)
        dong_trang_thai = st.empty()
        canh_bao = st.empty()
        da_bao = False
        bat_dau = time.time()

        pool = ThreadPoolExecutor(max_workers=so_luong)
        try:
            viec = [
                pool.submit(tra_1_mst, m, "VietQR" in nguon, "masothue" in nguon, tt, nghi)
                for m in ds_mst
            ]
            for i, v in enumerate(as_completed(viec), 1):
                dong = v.result()
                st.session_state["ket_qua"][dong["MST chuẩn hóa"]] = dong

                if not tt.masothue_bat and not da_bao and "masothue" in nguon:
                    canh_bao.warning(
                        "masothue chặn liên tục nên app đã tự bỏ qua nguồn này cho các MST còn lại. "
                        "Lần sau nên bỏ chọn masothue để chạy nhanh hơn, hoặc chạy app trên laptop để lấy SĐT."
                    )
                    da_bao = True

                if i % 10 == 0 or i == len(ds_mst):
                    da_chay = time.time() - bat_dau
                    toc_do = i / da_chay if da_chay else 0
                    con_lai = (len(ds_mst) - i) / toc_do / 60 if toc_do else 0
                    thanh.progress(i / len(ds_mst))
                    dong_trang_thai.write(
                        f"Đã tra {i:,}/{len(ds_mst):,} — tốc độ {toc_do:,.1f} MST/giây — "
                        f"còn khoảng {con_lai:,.0f} phút"
                    )
        finally:
            # bấm Stop giữa chừng thì hủy các việc chưa chạy
            pool.shutdown(wait=False, cancel_futures=True)
        dong_trang_thai.success(f"Tra cứu xong trong {(time.time() - bat_dau) / 60:,.1f} phút.")

    if st.session_state.get("ket_qua"):
        df_kq = pd.DataFrame(st.session_state["ket_qua"].values())
        df_xuat = df_goc.merge(df_kq, on="MST chuẩn hóa", how="left")

        st.subheader(f"Kết quả ({len(df_kq):,} MST đã tra)")
        for cot in ["Kết quả VietQR", "Kết quả masothue"]:
            if cot in df_kq:
                st.caption(f"{cot}: " + ", ".join(
                    f"{k}: {v:,}" for k, v in df_kq[cot].value_counts().items()))

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df_xuat.to_excel(w, index=False, sheet_name="Ket_qua")
        st.download_button(
            "Tải file Excel kết quả", buf.getvalue(),
            file_name="ket_qua_tra_cuu_mst.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        if len(df_xuat) > SO_DONG_HIEN_THI:
            st.caption(f"Chỉ hiện {SO_DONG_HIEN_THI} dòng đầu, file Excel có đủ {len(df_xuat):,} dòng.")
        st.dataframe(df_xuat.head(SO_DONG_HIEN_THI), use_container_width=True)
else:
    st.write("Tải file lên hoặc dán danh sách MST để bắt đầu.")
