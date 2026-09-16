"""
App tra cứu địa chỉ + số điện thoại doanh nghiệp từ mã số thuế (MST)
Nguồn: VietQR (API) và masothue.com (đọc trang web, có thể bị chặn)
Bản 8: + nguồn thongtindoanhnghiep.co (TTDN) và thongtincongty.vn (TTCT), cột tổng hợp
"""
import io
import re
import threading
import zipfile
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
        self.ttdn = BoDieuToc(1.5)
        self.ttdn_bat = True
        self.ttdn_chan_lien_tiep = 0
        self.ttct = BoDieuToc(1.5)
        self.ttct_bat = True
        self.ttct_chan_lien_tiep = 0


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


# ---------------- thongtindoanhnghiep.co (TTDN) - nguồn dự phòng ----------------
# Tên các trường dữ liệu của nguồn này không được công bố rõ, nên đọc "mềm":
# tìm khóa có chứa chữ khóa phù hợp, bỏ qua các khóa của cơ quan thuế quản lý.
def _lay_truong(d, uu_tien, chua, loai_tru=()):
    phang = {
        re.sub(r"[^a-z0-9]", "", str(k).lower()): v
        for k, v in d.items()
        if isinstance(v, (str, int, float)) and str(v).strip() not in ("", "None", "null")
    }
    for k in uu_tien:
        if k in phang:
            return str(phang[k]).strip()
    for k, v in phang.items():
        if any(c in k for c in chua) and not any(x in k for x in loai_tru):
            return str(v).strip()
    return ""


def tra_ttdn(mst, tt):
    out = {"Tên DN (TTDN)": "", "Địa chỉ (TTDN)": "", "SĐT (TTDN)": "", "Kết quả TTDN": ""}
    if not tt.ttdn_bat:
        out["Kết quả TTDN"] = "Bỏ qua (đã tắt do bị chặn)"
        return out
    loi = ""
    for _ in range(3):
        tt.ttdn.cho_luot()
        try:
            r = MST_SESSION.get(f"https://thongtindoanhnghiep.co/api/company/{mst}", timeout=TIMEOUT)
            if r.status_code in (403, 429, 503) or "Just a moment" in r.text[:3000]:
                tt.ttdn.bao_bi_gioi_han(_doc_retry_after(r))
                loi = f"Bị chặn ({r.status_code})"
                continue
            if r.status_code == 404:
                out["Kết quả TTDN"] = "Không tìm thấy"
                break
            try:
                j = r.json()
            except ValueError:
                loi = f"Bị chặn (phản hồi không phải dữ liệu, {r.status_code})"
                tt.ttdn.bao_bi_gioi_han()
                continue
            if isinstance(j, dict) and isinstance(j.get("data"), dict):
                j = j["data"]
            if not isinstance(j, dict) or not j:
                out["Kết quả TTDN"] = "Không tìm thấy"
                break
            loai_tru_cqt = ("noidangky", "noinopthue", "coquanthue", "nhanthongbao")
            out["Tên DN (TTDN)"] = _lay_truong(
                j, ["title", "ten", "tencongty", "tendoanhnghiep", "name"],
                ["tencongty", "tendoanhnghiep"], loai_tru_cqt + ("en", "viettat"))
            out["Địa chỉ (TTDN)"] = _lay_truong(
                j, ["diachicongty", "diachi", "address"], ["diachi", "address"], loai_tru_cqt)
            out["SĐT (TTDN)"] = _lay_truong(
                j, ["dienthoai", "sodienthoai", "phone"], ["dienthoai", "phone"],
                loai_tru_cqt + ("fax",))
            if out["Tên DN (TTDN)"] or out["Địa chỉ (TTDN)"]:
                out["Kết quả TTDN"] = "OK"
            else:
                out["Kết quả TTDN"] = "Không đọc được (khóa: " + ", ".join(list(j)[:8]) + ")"
            tt.ttdn.bao_thanh_cong()
            break
        except Exception as e:
            loi = f"Lỗi: {str(e)[:80]}"
            time.sleep(2)
    else:
        out["Kết quả TTDN"] = loi or "Lỗi"

    with tt.lock:
        if out["Kết quả TTDN"].startswith(("Bị chặn", "Lỗi")):
            tt.ttdn_chan_lien_tiep += 1
            if tt.ttdn_chan_lien_tiep >= TAT_SAU_N_LAN_CHAN:
                tt.ttdn_bat = False
        else:
            tt.ttdn_chan_lien_tiep = 0
    return out


# ---------------- thongtincongty.vn (TTCT) ----------------
# Trang chi tiết có dạng /ma-so-thue/<MST>-<tên-viết-liền>, dữ liệu nằm trong 1 bảng 2 cột:
# Mã số thuế | Tên đơn vị | Địa chỉ theo CQT | Địa chỉ sau sáp nhập | Trạng thái | Cơ quan thuế quản lý
# Trang này KHÔNG có số điện thoại.
TTCT_GOC = "https://thongtincongty.vn"


def _ttct_doc_bang(soup, mst):
    """Đọc bảng thông tin; trả về dict hoặc None nếu trang không phải trang của MST này."""
    du_lieu = {}
    for tr in soup.find_all("tr"):
        o = tr.find_all(["th", "td"])
        if len(o) >= 2:
            du_lieu[o[0].get_text(" ", strip=True).lower()] = o[1]
    o_mst = du_lieu.get("mã số thuế")
    if o_mst is None or mst not in o_mst.get_text(" ", strip=True):
        return None
    return du_lieu


def _ttct_link(soup, mst):
    for a in soup.find_all("a", href=True):
        href = a["href"]
        duong_dan = href.replace(TTCT_GOC, "")
        if duong_dan.startswith(f"/ma-so-thue/{mst}-") or duong_dan.rstrip("/") == f"/ma-so-thue/{mst}":
            return TTCT_GOC + duong_dan
    return None


def tra_ttct(mst, tt):
    out = {
        "Tên DN (TTCT)": "", "Địa chỉ (TTCT)": "", "Địa chỉ sau sáp nhập (TTCT)": "",
        "Trạng thái (TTCT)": "", "Kết quả TTCT": "",
    }
    if not tt.ttct_bat:
        out["Kết quả TTCT"] = "Bỏ qua (đã tắt do bị chặn)"
        return out

    def lay(url, **kw):
        tt.ttct.cho_luot()
        r = MST_SESSION.get(url, timeout=TIMEOUT, **kw)
        if r.status_code in (403, 429, 503) or "Just a moment" in r.text[:3000]:
            tt.ttct.bao_bi_gioi_han(_doc_retry_after(r))
            raise PermissionError(f"Bị chặn ({r.status_code})")
        tt.ttct.bao_thanh_cong()
        return r

    ket_qua = ""
    for _ in range(2):
        try:
            bang = None
            # thử lần lượt: link trực tiếp theo MST, rồi các kiểu trang tìm kiếm
            ung_vien = [
                (f"{TTCT_GOC}/ma-so-thue/{mst}", {}),
                (f"{TTCT_GOC}/tim-kiem", {"params": {"q": mst}}),
                (f"{TTCT_GOC}/", {"params": {"q": mst}}),
            ]
            for url, kw in ung_vien:
                r = lay(url, **kw)
                if r.status_code >= 400:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                bang = _ttct_doc_bang(soup, mst)
                if bang is None:
                    link = _ttct_link(soup, mst)
                    if link:
                        soup = BeautifulSoup(lay(link).text, "html.parser")
                        bang = _ttct_doc_bang(soup, mst)
                if bang is not None:
                    break
            if bang is None:
                ket_qua = "Không tìm thấy"
                break

            def gt(nhan):
                o = bang.get(nhan)
                return o.get_text(" ", strip=True) if o is not None else ""

            out["Tên DN (TTCT)"] = gt("tên đơn vị") or gt("tên công ty") or gt("tên doanh nghiệp")
            out["Địa chỉ (TTCT)"] = gt("địa chỉ theo cqt") or gt("địa chỉ")
            out["Trạng thái (TTCT)"] = gt("trạng thái")
            sap_nhap = gt("địa chỉ sau sáp nhập")
            m = re.search(r"Địa chỉ 1\s*:?\s*(.+?)(?:\s*-\s*Căn cứ|\s*---|$)", sap_nhap)
            out["Địa chỉ sau sáp nhập (TTCT)"] = m.group(1).strip(" -") if m else ""
            ket_qua = "OK" if (out["Tên DN (TTCT)"] or out["Địa chỉ (TTCT)"]) else "Không đọc được trang"
            break
        except PermissionError as e:
            ket_qua = str(e)
        except Exception as e:
            ket_qua = f"Lỗi: {str(e)[:80]}"
            time.sleep(2)
    out["Kết quả TTCT"] = ket_qua or "Lỗi"

    with tt.lock:
        if out["Kết quả TTCT"].startswith(("Bị chặn", "Lỗi")):
            tt.ttct_chan_lien_tiep += 1
            if tt.ttct_chan_lien_tiep >= TAT_SAU_N_LAN_CHAN:
                tt.ttct_bat = False
        else:
            tt.ttct_chan_lien_tiep = 0
    return out


def _dau_tien(dong, cac_cot):
    for c in cac_cot:
        v = dong.get(c)
        if v is not None and str(v).strip() not in ("", "nan"):
            return str(v).strip()
    return ""


def them_cot_tong_hop(dong):
    # lấy giá trị đầu tiên có dữ liệu theo thứ tự ưu tiên nguồn
    dong["Tên DN (tổng hợp)"] = _dau_tien(
        dong, ["Tên DN (VietQR)", "Tên DN (TTDN)", "Tên DN (TTCT)", "Tên DN (masothue)"])
    dong["Địa chỉ (tổng hợp)"] = _dau_tien(
        dong, ["Địa chỉ (VietQR)", "Địa chỉ (TTDN)", "Địa chỉ (TTCT)", "Địa chỉ (masothue)"])
    dong["SĐT (tổng hợp)"] = _dau_tien(dong, ["SĐT (masothue)", "SĐT (TTDN)"])
    return dong


def vqr_loi(dong):
    return str(dong.get("Kết quả VietQR", "Lỗi")).startswith("Lỗi")


def ttdn_ok(dong):
    return str(dong.get("Kết quả TTDN", "")).startswith(("OK", "Không tìm thấy"))


def ttct_ok(dong):
    return str(dong.get("Kết quả TTCT", "")).startswith(("OK", "Không tìm thấy"))


def du_phong_da_co(dong):
    """Có ít nhất 1 nguồn dự phòng tra ra dữ liệu."""
    return any(str(dong.get(c, "")).startswith("OK") for c in ["Kết quả TTDN", "Kết quả TTCT"])


def tra_1_mst(mst, dung_vqr, dung_mst, tt, nghi, ket_qua_cu,
              dung_ttdn=False, du_phong=False, dung_ttct=False):
    dong = dict(ket_qua_cu or {"MST chuẩn hóa": mst})
    if dung_vqr:
        dong.update(tra_vietqr(mst, tt))
    can_du_phong = du_phong and dung_vqr and vqr_loi(dong)
    # Dự phòng theo thứ tự: VietQR lỗi -> TTDN -> (TTDN chưa ra) -> TTCT
    if dung_ttdn or can_du_phong:
        dong.update(tra_ttdn(mst, tt))
    if dung_ttct or (can_du_phong and not str(dong.get("Kết quả TTDN", "")).startswith("OK")):
        dong.update(tra_ttct(mst, tt))
    if dung_mst:
        dong.update(tra_masothue(mst, tt, nghi))
    return them_cot_tong_hop(dong)


def chay_tra_cuu(ds_mst, dung_vqr, dung_mst, so_luong, nghi, khung,
                 dung_ttdn=False, du_phong=False, dung_ttct=False):
    """Tra 1 lô MST, ghi dần kết quả vào session_state. khung = các ô hiển thị dùng chung."""
    kq = st.session_state.setdefault("ket_qua", {})
    tt = TrangThaiChung(st.session_state.get("khoang_cach_vqr", 1.0))
    bat_dau = time.time()
    tong = len(ds_mst)
    khung["lo_bar"].progress(0.0)

    pool = ThreadPoolExecutor(max_workers=so_luong)
    try:
        viec = [
            pool.submit(tra_1_mst, m, dung_vqr, dung_mst, tt, nghi, kq.get(m),
                        dung_ttdn, du_phong, dung_ttct)
            for m in ds_mst
        ]
        for i, v in enumerate(as_completed(viec), 1):
            dong = v.result()
            kq[dong["MST chuẩn hóa"]] = dong
            khung["da_xong"] += 1

            if dung_mst and not tt.masothue_bat:
                khung["canh_bao"].warning(
                    "masothue chặn liên tục nên app đã bỏ qua nguồn này cho các MST còn lại của lô."
                )

            if (dung_ttdn or du_phong) and not tt.ttdn_bat:
                khung["canh_bao_ttdn"].warning(
                    "thongtindoanhnghiep.co chặn liên tục nên app đã bỏ qua nguồn này cho phần còn lại của lô."
                )

            if (dung_ttct or du_phong) and not tt.ttct_bat:
                khung["canh_bao_ttct"].warning(
                    "thongtincongty.vn chặn liên tục nên app đã bỏ qua nguồn này cho phần còn lại của lô."
                )

            if i % 5 == 0 or i == tong:
                da_chay = time.time() - bat_dau
                toc_do = i / da_chay if da_chay else 0
                khung["lo_bar"].progress(i / tong)
                khung["tong_bar"].progress(khung["da_xong"] / khung["tong"])
                nhip = (f" — nhịp VietQR: 1 lần/{tt.vqr.khoang_cach:.1f} giây, "
                        f"bị giới hạn {tt.vqr.so_lan_bi_gioi_han} lần") if dung_vqr else ""
                khung["trang_thai"].write(
                    f"Lô này: {i:,}/{tong:,} MST — tốc độ {toc_do:,.1f} MST/giây{nhip}"
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        st.session_state["khoang_cach_vqr"] = tt.vqr.khoang_cach


def chay_theo_lo(ds_mst, dung_vqr, dung_mst, so_luong, nghi, co_lo, nghi_giua_lo, ten_viec,
                 dung_ttdn=False, du_phong=False, dung_ttct=False):
    """Tự chia danh sách thành các lô nhỏ, tra lần lượt, nghỉ giữa các lô."""
    cac_lo = [ds_mst[i:i + co_lo] for i in range(0, len(ds_mst), co_lo)]
    st.markdown(f"**{ten_viec}: {len(ds_mst):,} MST, chia thành {len(cac_lo)} lô**")
    khung = {
        "tieu_de_lo": st.empty(),
        "lo_bar": st.progress(0.0),
        "trang_thai": st.empty(),
        "tong_tieu_de": st.empty(),
        "tong_bar": st.progress(0.0),
        "dem_nguoc": st.empty(),
        "canh_bao": st.empty(),
        "canh_bao_ttdn": st.empty(),
        "canh_bao_ttct": st.empty(),
        "da_xong": 0,
        "tong": len(ds_mst),
    }
    bat_dau = time.time()
    for k, lo in enumerate(cac_lo, 1):
        khung["tieu_de_lo"].info(f"Đang tra lô {k}/{len(cac_lo)} ({len(lo)} MST)")
        khung["tong_tieu_de"].caption(
            f"Tổng tiến độ: {khung['da_xong']:,}/{khung['tong']:,} MST — đã chạy "
            f"{(time.time() - bat_dau) / 60:,.1f} phút"
        )
        chay_tra_cuu(lo, dung_vqr, dung_mst, so_luong, nghi, khung, dung_ttdn, du_phong, dung_ttct)
        st.session_state["lo_da_xong"] = k

        if k < len(cac_lo) and nghi_giua_lo > 0:
            for con in range(int(nghi_giua_lo), 0, -1):
                khung["dem_nguoc"].write(
                    f"Xong lô {k}. Nghỉ {con} giây rồi chạy lô {k + 1}..."
                )
                time.sleep(1)
            khung["dem_nguoc"].empty()

    khung["tieu_de_lo"].success(
        f"Xong {len(cac_lo)} lô, {len(ds_mst):,} MST trong {(time.time() - bat_dau) / 60:,.1f} phút."
    )
    khung["tong_bar"].progress(1.0)


@st.cache_data(show_spinner=False)
def dong_goi_zip(df: pd.DataFrame, tien_to: str):
    """Tách bảng theo cột 'Lô' thành nhiều file Excel, nén vào 1 file zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for lo, phan in df.dropna(subset=["Lô"]).groupby("Lô"):
            b = io.BytesIO()
            phan.to_excel(b, index=False, engine="openpyxl")
            z.writestr(f"{tien_to}_lo_{int(lo):03d}.xlsx", b.getvalue())
    return buf.getvalue()


@st.cache_data(show_spinner=False)
def ra_excel(df: pd.DataFrame):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Ket_qua")
    return buf.getvalue()


def tong_hop_loi(df_xuat, dung_vqr, dung_mst, dung_ttdn=False, dung_ttct=False):
    """Gom MST có vấn đề thành 3 nhóm, mỗi nhóm 1 sheet, kèm cột 'Lý do'."""
    def ly_do(dong):
        cac_ly_do = []
        kq_vqr = str(dong.get("Kết quả VietQR") or "")
        kq_mst = str(dong.get("Kết quả masothue") or "")
        kq_ttdn = str(dong.get("Kết quả TTDN") or "")
        kq_ttct = str(dong.get("Kết quả TTCT") or "")
        if dung_vqr and (kq_vqr in ("", "nan") or kq_vqr.startswith("Lỗi")) and not du_phong_da_co(dong):
            ly = "VietQR: " + (kq_vqr if kq_vqr not in ("", "nan") else "chưa tra")
            for ten, kq_dp in [("TTDN", kq_ttdn), ("TTCT", kq_ttct)]:
                if kq_dp not in ("", "nan"):
                    ly += f" | {ten}: {kq_dp}"
            cac_ly_do.append(ly)
        if not dung_vqr:
            if dung_ttdn and not ttdn_ok(dong):
                cac_ly_do.append("TTDN: " + (kq_ttdn if kq_ttdn not in ("", "nan") else "chưa tra"))
            if dung_ttct and not ttct_ok(dong):
                cac_ly_do.append("TTCT: " + (kq_ttct if kq_ttct not in ("", "nan") else "chưa tra"))
        if dung_mst and not kq_mst.startswith(("OK", "Không tìm thấy")):
            cac_ly_do.append("masothue: " + (kq_mst if kq_mst not in ("", "nan") else "chưa tra"))
        return "; ".join(cac_ly_do)

    def khong_thay(dong):
        kq_vqr = str(dong.get("Kết quả VietQR") or "")
        kq_mst = str(dong.get("Kết quả masothue") or "")
        ly = []
        kq_ttdn = str(dong.get("Kết quả TTDN") or "")
        if dung_vqr and kq_vqr not in ("", "nan", "OK") and not kq_vqr.startswith("Lỗi"):
            ly.append("VietQR: " + kq_vqr)
        kq_ttct = str(dong.get("Kết quả TTCT") or "")
        if kq_vqr != "OK" and not du_phong_da_co(dong):
            if kq_ttdn.startswith("Không tìm thấy"):
                ly.append("TTDN: không tìm thấy")
            if kq_ttct.startswith("Không tìm thấy"):
                ly.append("TTCT: không tìm thấy")
        if dung_mst and kq_mst.startswith("Không tìm thấy"):
            ly.append("masothue: không tìm thấy")
        return "; ".join(ly)

    hop_le = df_xuat[df_xuat["MST chuẩn hóa"].notna()].copy()

    sai_dinh_dang = df_xuat[df_xuat["MST chuẩn hóa"].isna()].copy()
    sai_dinh_dang.insert(0, "Lý do", "MST sai định dạng (không đủ 10 hoặc 13 số)")

    loi = hop_le.copy()
    loi.insert(0, "Lý do", loi.apply(ly_do, axis=1))
    loi = loi[loi["Lý do"] != ""]

    kt = hop_le.copy()
    kt.insert(0, "Lý do", kt.apply(khong_thay, axis=1))
    kt = kt[(kt["Lý do"] != "") & (~kt["MST chuẩn hóa"].isin(loi["MST chuẩn hóa"]))]

    return {
        "Loi_tra_cuu": loi,           # lỗi kết nối, bị chặn/giới hạn, chưa tra -> nên tra lại
        "Khong_tim_thay": kt,         # nguồn trả lời là không có dữ liệu -> kiểm tra lại MST
        "MST_sai_dinh_dang": sai_dinh_dang,
    }


@st.cache_data(show_spinner=False)
def ra_excel_nhieu_sheet(cac_sheet: dict):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for ten, df in cac_sheet.items():
            df.to_excel(w, index=False, sheet_name=ten)
    return buf.getvalue()


# ---------------- Giao diện ----------------
st.title("Tra cứu địa chỉ và số điện thoại theo MST")

with st.sidebar:
    st.header("Cài đặt")
    nguon = st.multiselect(
        "Nguồn tra cứu", ["VietQR", "thongtindoanhnghiep", "thongtincongty", "masothue"],
        default=["VietQR"],
        help="VietQR: tên + địa chỉ. thongtindoanhnghiep: tên + địa chỉ (+ SĐT nếu có). "
             "thongtincongty: tên + địa chỉ + địa chỉ sau sáp nhập + trạng thái (không có SĐT). "
             "masothue: tên + địa chỉ + SĐT nhưng hay bị chặn.",
    )
    du_phong = st.checkbox(
        "Khi VietQR lỗi, tự tra thay bằng thongtindoanhnghiep → thongtincongty", value=True,
        help="Chỉ gọi nguồn dự phòng cho những MST mà VietQR báo lỗi/giới hạn. "
             "thongtincongty chỉ được gọi nếu thongtindoanhnghiep cũng không ra.",
    )
    so_luong = st.slider("Số MST tra cùng lúc", 1, 5, 1,
                         help="VietQR đã có bộ tự điều tốc, để 1 là ổn nhất.")
    nghi = st.slider("Nghỉ sau mỗi lần tra masothue (giây)", 0.0, 3.0, 0.5, 0.5)
    st.divider()
    co_lo = st.slider("Số MST mỗi lô", 10, 100, 100, 10,
                      help="File gốc được tự chia thành các lô nhỏ, tối đa 100 MST/lô.")
    nghi_giua_lo = st.slider("Nghỉ giữa các lô (giây)", 0, 600, 60, 10,
                             help="Cho VietQR/masothue \"nguội\" lại trước khi chạy lô tiếp theo.")

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
    so_lo = -(-len(ds_mst) // co_lo)  # làm tròn lên

    # đánh số lô cho từng MST
    lo_cua_mst = {m: i // co_lo + 1 for i, m in enumerate(ds_mst)}
    df_goc["Lô"] = df_goc["MST chuẩn hóa"].map(lo_cua_mst)

    st.info(
        f"{len(ds_mst):,} MST hợp lệ (đã bỏ trùng), {so_loi:,} dòng MST không hợp lệ. "
        f"Sẽ chia thành {so_lo} lô, mỗi lô tối đa {co_lo} MST, nghỉ {nghi_giua_lo} giây giữa các lô. "
        "Giữ tab mở và không bấm gì khác trong lúc chạy."
    )
    st.download_button(
        f"Tải {so_lo} file nhỏ đã tách (zip)",
        dong_goi_zip(df_goc, "file_goc"),
        file_name="file_goc_da_tach.zip", mime="application/zip",
    )

    dung_vqr, dung_mst = "VietQR" in nguon, "masothue" in nguon
    dung_ttdn = "thongtindoanhnghiep" in nguon
    dung_ttct = "thongtincongty" in nguon

    if st.button("Bắt đầu tra cứu", type="primary", disabled=not nguon):
        st.session_state["ket_qua"] = {}
        chay_theo_lo(ds_mst, dung_vqr, dung_mst, so_luong, nghi,
                     co_lo, nghi_giua_lo, "Tra cứu", dung_ttdn, du_phong, dung_ttct)

    kq = st.session_state.get("ket_qua", {})
    if kq:
        # MST bị lỗi hoặc chưa tra (vd bấm Stop giữa chừng) -> tra lại, cũng chia lô
        loi_vqr = [m for m in ds_mst if dung_vqr and
                   vqr_loi(kq.get(m, {})) and not du_phong_da_co(kq.get(m, {}))]
        loi_ttdn = [m for m in ds_mst if dung_ttdn and not dung_vqr and
                    not ttdn_ok(kq.get(m, {}))]
        loi_ttct = [m for m in ds_mst if dung_ttct and not dung_vqr and
                    not ttct_ok(kq.get(m, {}))]
        loi_mst = [m for m in ds_mst if dung_mst and
                   not str(kq.get(m, {}).get("Kết quả masothue", "")).startswith(("OK", "Không tìm thấy"))]
        so_can_tra = len(set(loi_vqr) | set(loi_mst) | set(loi_ttdn) | set(loi_ttct))
        if so_can_tra:
            if st.button(f"Tra lại / tra tiếp {so_can_tra:,} MST bị lỗi hoặc chưa tra"):
                if loi_vqr:
                    chay_theo_lo(loi_vqr, True, False, so_luong, nghi,
                                 co_lo, nghi_giua_lo, "Tra lại VietQR", False, du_phong)
                if loi_ttdn:
                    chay_theo_lo(loi_ttdn, False, False, so_luong, nghi,
                                 co_lo, nghi_giua_lo, "Tra lại thongtindoanhnghiep", True, False)
                if loi_ttct:
                    chay_theo_lo(loi_ttct, False, False, so_luong, nghi,
                                 co_lo, nghi_giua_lo, "Tra lại thongtincongty", False, False, True)
                if loi_mst:
                    chay_theo_lo(loi_mst, False, True, so_luong, nghi,
                                 co_lo, nghi_giua_lo, "Tra lại masothue")
                kq = st.session_state["ket_qua"]

        df_kq = pd.DataFrame(kq.values())
        df_xuat = df_goc.merge(df_kq, on="MST chuẩn hóa", how="left")
        cot_tong_hop = [c for c in ["Tên DN (tổng hợp)", "Địa chỉ (tổng hợp)", "SĐT (tổng hợp)"]
                        if c in df_xuat]
        cot_khac = [c for c in df_xuat.columns if c not in cot_tong_hop]
        vi_tri = cot_khac.index("Lô") + 1 if "Lô" in cot_khac else len(cot_khac)
        df_xuat = df_xuat[cot_khac[:vi_tri] + cot_tong_hop + cot_khac[vi_tri:]]

        st.subheader(f"Kết quả ({len(df_kq):,}/{len(ds_mst):,} MST đã tra)")
        for cot in ["Kết quả VietQR", "Kết quả TTDN", "Kết quả TTCT", "Kết quả masothue"]:
            if cot in df_kq:
                st.caption(f"{cot}: " + ", ".join(
                    f"{k}: {v:,}" for k, v in df_kq[cot].value_counts().items()))

        # ---- File tổng hợp MST lỗi ----
        nhom_loi = tong_hop_loi(df_xuat, dung_vqr, dung_mst, dung_ttdn, dung_ttct)
        n_loi = len(nhom_loi["Loi_tra_cuu"])
        n_kt = len(nhom_loi["Khong_tim_thay"])
        n_sai = len(nhom_loi["MST_sai_dinh_dang"])
        if n_loi + n_kt + n_sai:
            st.warning(
                f"Có {n_loi:,} dòng tra bị lỗi/chưa tra, {n_kt:,} dòng không tìm thấy dữ liệu, "
                f"{n_sai:,} dòng MST sai định dạng."
            )
            st.download_button(
                f"Tải file MST lỗi ({n_loi + n_kt + n_sai:,} dòng, 3 sheet)",
                ra_excel_nhieu_sheet(nhom_loi),
                file_name="mst_bi_loi.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            with st.expander("Xem nhanh các dòng tra bị lỗi"):
                st.dataframe(
                    nhom_loi["Loi_tra_cuu"][["Lý do", cot_mst, "MST chuẩn hóa", "Lô"]]
                    .head(SO_DONG_HIEN_THI),
                    use_container_width=True,
                )
        else:
            st.success("Không có MST nào bị lỗi.")

        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "Tải kết quả gộp (1 file Excel)", ra_excel(df_xuat),
                file_name="ket_qua_tra_cuu_mst.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
            )
        with c2:
            st.download_button(
                "Tải kết quả tách theo lô (zip)", dong_goi_zip(df_xuat, "ket_qua"),
                file_name="ket_qua_theo_lo.zip", mime="application/zip",
            )
        if len(df_xuat) > SO_DONG_HIEN_THI:
            st.caption(f"Chỉ hiện {SO_DONG_HIEN_THI} dòng đầu, file Excel có đủ {len(df_xuat):,} dòng.")
        st.dataframe(df_xuat.head(SO_DONG_HIEN_THI), use_container_width=True)
else:
    st.write("Tải file lên hoặc dán danh sách MST để bắt đầu.")
