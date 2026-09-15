"""
App tra cứu địa chỉ + số điện thoại doanh nghiệp từ mã số thuế (MST)
Nguồn: VietQR (API) và masothue.com (đọc trang web, có thể bị chặn)
Bản 4: tự điều chỉnh tốc độ gọi VietQR + nút tra lại các MST bị lỗi
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

VQR_SESSION = requests.Session()
VQR_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20))

TIMEOUT = 10
TAT_SAU_N_LAN_CHAN = 5
SO_DONG_HIEN_THI = 200
MASOTHUE_TOI_DA_CUNG_LUC = 2
VQR_SO_LAN_THU = 8

st.set_page_config(page_title="Tra cứu MST", page_icon="🔎", layout="wide")


# ---------------- Bộ điều tốc: tự chậm lại khi bị giới hạn, tự nhanh dần khi ổn ----------------
class BoDieuToc:
    def __init__(self, khoang_cach=1.0, nhanh_nhat=0.3, cham_nhat=30.0):
        self.lock = threading.Lock()
        self.khoang_cach = khoang_cach   # số giây giữa 2 lần gọi
        self.nhanh_nhat = nhanh_nhat
        self.cham_nhat = cham_nhat
        self.luot_ke_tiep = 0.0
        self.ok_lien_tiep = 0
        self.so_lan_bi_gioi_han = 0

    def cho_luot(self):
        with self.lock:
            bay_gio = time.time()
            luot = max(bay_gio, self.luot_ke_tiep)
            self.luot_ke_tiep = luot + self.khoang_cach
        if luot > bay_gio:
            time.sleep(luot - bay_gio)

    def bao_bi_gioi_han(self, retry_after=None):
        with self.lock:
            self.so_lan_bi_gioi_han += 1
            self.ok_lien_tiep = 0
            self.khoang_cach = min(self.khoang_cach * 2, self.cham_nhat)
            nghi = retry_after if retry_after else self.khoang_cach * 3
            self.luot_ke_tiep = max(self.luot_ke_tiep, time.time() + nghi)

    def bao_thanh_cong(self):
        with self.lock:
            self.ok_lien_tiep += 1
            if self.ok_lien_tiep >= 20:  # 20 lần ổn liên tiếp -> nhanh lên 20%
                self.khoang_cach = max(self.khoang_cach * 0.8, self.nhanh_nhat)
                self.ok_lien_tiep = 0


class TrangThaiChung:
    def __init__(self, khoang_cach_vqr):
        self.lock = threading.Lock()
        self.vqr = BoDieuToc(khoang_cach_vqr)
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
def _doc_retry_after(r):
    try:
        return float(r.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


def tra_vietqr(mst, tt):
    out = {"Tên DN (VietQR)": "", "Địa chỉ (VietQR)": "", "Kết quả VietQR": ""}
    loi = ""
    for _ in range(VQR_SO_LAN_THU):
        tt.vqr.cho_luot()
        try:
            r = VQR_SESSION.get(f"https://api.vietqr.io/v2/business/{mst}", timeout=TIMEOUT)
            if r.status_code in (429, 403, 503):
                tt.vqr.bao_bi_gioi_han(_doc_retry_after(r))
                loi = f"bị giới hạn tốc độ ({r.status_code})"
                continue
            try:
                j = r.json()
            except ValueError:  # trả về trang HTML thay vì dữ liệu -> coi như bị giới hạn
                tt.vqr.bao_bi_gioi_han()
                loi = f"phản hồi lạ ({r.status_code})"
                continue
            desc = str(j.get("desc") or "")
            if j.get("code") == "00" and j.get("data"):
                d = j["data"]
                out["Tên DN (VietQR)"] = d.get("name") or ""
                out["Địa chỉ (VietQR)"] = d.get("address") or ""
                out["Kết quả VietQR"] = "OK"
                tt.vqr.bao_thanh_cong()
                return out
            if any(k in desc.lower() for k in ["limit", "too many", "quá nhiều", "giới hạn"]):
                tt.vqr.bao_bi_gioi_han()
                loi = f"bị giới hạn tốc độ ({desc[:40]})"
                continue
            out["Kết quả VietQR"] = desc or "Không tìm thấy"
            tt.vqr.bao_thanh_cong()
            return out
        except Exception as e:
            loi = str(e)[:80]
            time.sleep(2)
    out["Kết quả VietQR"] = f"Lỗi: {loi}"
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


def tra_1_mst(mst, dung_vqr, dung_mst, tt, nghi, ket_qua_cu):
    dong = dict(ket_qua_cu or {"MST chuẩn hóa": mst})
    if dung_vqr:
        dong.update(tra_vietqr(mst, tt))
    if dung_mst:
        dong.update(tra_masothue(mst, tt, nghi))
    return dong


def chay_tra_cuu(ds_mst, dung_vqr, dung_mst, so_luong, nghi):
    """Tra danh sách MST, ghi dần kết quả vào session_state."""
    kq = st.session_state.setdefault("ket_qua", {})
    tt = TrangThaiChung(st.session_state.get("khoang_cach_vqr", 1.0))
    thanh = st.progress(0.0)
    dong_trang_thai = st.empty()
    canh_bao = st.empty()
    da_bao = False
    bat_dau = time.time()
    tong = len(ds_mst)

    pool = ThreadPoolExecutor(max_workers=so_luong)
    try:
        viec = [
            pool.submit(tra_1_mst, m, dung_vqr, dung_mst, tt, nghi, kq.get(m))
            for m in ds_mst
        ]
        for i, v in enumerate(as_completed(viec), 1):
            dong = v.result()
            kq[dong["MST chuẩn hóa"]] = dong

            if dung_mst and not tt.masothue_bat and not da_bao:
                canh_bao.warning(
                    "masothue chặn liên tục nên app đã bỏ qua nguồn này cho các MST còn lại."
                )
                da_bao = True

            if i % 5 == 0 or i == tong:
                da_chay = time.time() - bat_dau
                toc_do = i / da_chay if da_chay else 0
                con_lai = (tong - i) / toc_do / 60 if toc_do else 0
                thanh.progress(i / tong)
                nhip = (f" — nhịp VietQR: 1 lần/{tt.vqr.khoang_cach:.1f} giây, "
                        f"đã bị giới hạn {tt.vqr.so_lan_bi_gioi_han} lần") if dung_vqr else ""
                dong_trang_thai.write(
                    f"Đã tra {i:,}/{tong:,} — còn khoảng {con_lai:,.0f} phút{nhip}"
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        # nhớ nhịp đang chạy ổn để lần sau (hoặc lúc tra lại) bắt đầu từ đó
        st.session_state["khoang_cach_vqr"] = tt.vqr.khoang_cach
    dong_trang_thai.success(f"Xong {tong:,} MST trong {(time.time() - bat_dau) / 60:,.1f} phút.")


# ---------------- Giao diện ----------------
st.title("Tra cứu địa chỉ và số điện thoại theo MST")

with st.sidebar:
    st.header("Cài đặt")
    nguon = st.multiselect(
        "Nguồn tra cứu", ["VietQR", "masothue"], default=["VietQR"],
        help="VietQR chỉ có tên + địa chỉ. Số điện thoại chỉ lấy được từ masothue.",
    )
    so_luong = st.slider("Số MST tra cùng lúc", 1, 5, 1,
                         help="VietQR đã có bộ tự điều tốc, để 1 là ổn nhất.")
    nghi = st.slider("Nghỉ sau mỗi lần tra masothue (giây)", 0.0, 3.0, 0.5, 0.5)

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

    dung_vqr, dung_mst = "VietQR" in nguon, "masothue" in nguon

    if st.button("Bắt đầu tra cứu", type="primary", disabled=not nguon):
        st.session_state["ket_qua"] = {}
        chay_tra_cuu(ds_mst, dung_vqr, dung_mst, so_luong, nghi)

    kq = st.session_state.get("ket_qua", {})
    if kq:
        # Nút tra lại: chỉ chạy các MST bị lỗi / chưa tra xong
        loi_vqr = [m for m in ds_mst if dung_vqr and
                   str(kq.get(m, {}).get("Kết quả VietQR", "Lỗi")).startswith("Lỗi")]
        loi_mst = [m for m in ds_mst if dung_mst and
                   not str(kq.get(m, {}).get("Kết quả masothue", "")).startswith(("OK", "Không tìm thấy"))]
        if loi_vqr or loi_mst:
            if st.button(f"Tra lại {len(set(loi_vqr) | set(loi_mst)):,} MST bị lỗi hoặc chưa tra"):
                if loi_vqr:
                    chay_tra_cuu(loi_vqr, True, False, so_luong, nghi)
                if loi_mst:
                    chay_tra_cuu(loi_mst, False, True, so_luong, nghi)
                kq = st.session_state["ket_qua"]

        df_kq = pd.DataFrame(kq.values())
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
