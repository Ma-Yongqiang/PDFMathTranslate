"""Functions that can be used for the most common use-cases for pdf2zh.six"""

import asyncio
import io
import os
import sys
import tempfile
import urllib.request
from asyncio import CancelledError
from pathlib import Path
from typing import Any, BinaryIO, List, Optional, Dict

import numpy as np
import requests
import tqdm
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfexceptions import PDFValueError
from pdfminer.pdfinterp import PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pymupdf import Document, Font

from pdf2zh.converter import TranslateConverter
from pdf2zh.doclayout import OnnxModel
from pdf2zh.pdfinterp import PDFPageInterpreterEx

resfont_map = {
    "zh-cn": "china-ss",
    "zh-tw": "china-ts",
    "zh-hans": "china-ss",
    "zh-hant": "china-ts",
    "zh": "china-ss",
    "ja": "japan-s",
    "ko": "korea-s",
}

noto_list = [
    "am",  # Amharic
    "ar",  # Arabic
    "bn",  # Bengali
    "bg",  # Bulgarian
    "chr",  # Cherokee
    "el",  # Greek
    "gu",  # Gujarati
    "iw",  # Hebrew
    "hi",  # Hindi
    "kn",  # Kannada
    "ml",  # Malayalam
    "mr",  # Marathi
    "ru",  # Russian
    "sr",  # Serbian
    "ta",  # Tamil
    "te",  # Telugu
    "th",  # Thai
    "ur",  # Urdu
    "uk",  # Ukrainian
]

def get_system_fonts():
    """获取系统字体路径"""
    system_fonts = {}
    
    if sys.platform == "win32":
        # Windows 字体路径
        font_paths = [
            os.path.join(os.environ["WINDIR"], "Fonts"),
            os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts"),
        ]
        font_files = {
            "simsun": ["simsun.ttc", "simsun.ttf"],  # 宋体
            "simhei": ["simhei.ttf"],  # 黑体
            "msyh": ["msyh.ttc", "msyh.ttf"],  # 微软雅黑
            "simkai": ["simkai.ttf"],  # 楷体
        }
    elif sys.platform == "darwin":
        # macOS 字体路径
        font_paths = [
            "/System/Library/Fonts",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        ]
        font_files = {
            "simsun": ["Sun.ttf", "STSong.ttf"],  # 宋体
            "simhei": ["STHeiti.ttf"],  # 黑体
            "msyh": ["STHeiti Light.ttf"],  # 对应微软雅黑
            "simkai": ["STKaiti.ttf"],  # 楷体
        }
    else:
        # Linux 字体路径
        font_paths = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
        ]
        font_files = {
            "simsun": ["simsun.ttc", "simsun.ttf"],
            "simhei": ["simhei.ttf"],
            "msyh": ["msyh.ttc", "msyh.ttf"],
            "simkai": ["simkai.ttf"],
        }

    # 搜索字体文件
    for font_name, filenames in font_files.items():
        for font_path in font_paths:
            if not os.path.exists(font_path):
                continue
            for filename in filenames:
                full_path = os.path.join(font_path, filename)
                if os.path.exists(full_path):
                    system_fonts[font_name] = full_path
                    break
            if font_name in system_fonts:
                break
                
    return system_fonts

def get_fallback_font():
    """获取备用字体"""
    # 创建临时目录存放下载的字体
    temp_dir = os.path.join(tempfile.gettempdir(), "pdf2zh_fonts")
    os.makedirs(temp_dir, exist_ok=True)
    
    # 下载开源中文字体作为备用
    fallback_url = "https://github.com/adobe-fonts/source-han-serif/raw/release/OTF/SimplifiedChinese/SourceHanSerifSC-Regular.otf"
    fallback_path = os.path.join(temp_dir, "SourceHanSerifSC-Regular.otf")
    
    if not os.path.exists(fallback_path):
        try:
            print("Downloading fallback font...")
            urllib.request.urlretrieve(fallback_url, fallback_path)
        except Exception as e:
            print(f"Failed to download fallback font: {e}")
            return None
            
    return fallback_path if os.path.exists(fallback_path) else None

def check_files(files: List[str]) -> List[str]:
    files = [f for f in files if not f.startswith("http://")] # exclude online files, http
    files = [f for f in files if not f.startswith("https://")] # exclude online files, https
    missing_files = [file for file in files if not os.path.exists(file)]
    return missing_files

def translate_patch(
    inf: BinaryIO,
    pages: Optional[list[int]] = None,
    vfont: str = "",
    vchar: str = "",
    thread: int = 0,
    doc_zh: Document = None,
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    resfont: str = "",
    noto: Font = None,
    callback: object = None,
    cancellation_event: asyncio.Event = None,
    model: OnnxModel = None,
    envs: Dict = None,
    prompt: List = None,
    **kwarg: Any,
) -> None:
    rsrcmgr = PDFResourceManager()
    layout = {}
    device = TranslateConverter(
        rsrcmgr,
        vfont,
        vchar,
        thread,
        layout,
        lang_in,
        lang_out,
        service,
        resfont,
        noto,
        envs,
        prompt,
    )

    assert device is not None
    obj_patch = {}
    interpreter = PDFPageInterpreterEx(rsrcmgr, device, obj_patch)
    if pages:
        total_pages = len(pages)
    else:
        total_pages = doc_zh.page_count

    parser = PDFParser(inf)
    doc = PDFDocument(parser)
    with tqdm.tqdm(total=total_pages) as progress:
        for pageno, page in enumerate(PDFPage.create_pages(doc)):
            if cancellation_event and cancellation_event.is_set():
                raise CancelledError("task cancelled")
            if pages and (pageno not in pages):
                continue
            progress.update()
            if callback:
                callback(progress)
            page.pageno = pageno
            pix = doc_zh[page.pageno].get_pixmap()
            image = np.fromstring(pix.samples, np.uint8).reshape(
                pix.height, pix.width, 3
            )[:, :, ::-1]
            page_layout = model.predict(image, imgsz=int(pix.height / 32) * 32)[0]
            box = np.ones((pix.height, pix.width))
            h, w = box.shape
            vcls = ["abandon", "figure", "table", "isolate_formula", "formula_caption"]
            for i, d in enumerate(page_layout.boxes):
                if page_layout.names[int(d.cls)] not in vcls:
                    x0, y0, x1, y1 = d.xyxy.squeeze()
                    x0, y0, x1, y1 = (
                        np.clip(int(x0 - 1), 0, w - 1),
                        np.clip(int(h - y1 - 1), 0, h - 1),
                        np.clip(int(x1 + 1), 0, w - 1),
                        np.clip(int(h - y0 + 1), 0, h - 1),
                    )
                    box[y0:y1, x0:x1] = i + 2
            for i, d in enumerate(page_layout.boxes):
                if page_layout.names[int(d.cls)] in vcls:
                    x0, y0, x1, y1 = d.xyxy.squeeze()
                    x0, y0, x1, y1 = (
                        np.clip(int(x0 - 1), 0, w - 1),
                        np.clip(int(h - y1 - 1), 0, h - 1),
                        np.clip(int(x1 + 1), 0, w - 1),
                        np.clip(int(h - y0 + 1), 0, h - 1),
                    )
                    box[y0:y1, x0:x1] = 0
            layout[page.pageno] = box
            page.page_xref = doc_zh.get_new_xref()
            doc_zh.update_object(page.page_xref, "<<>>")
            doc_zh.update_stream(page.page_xref, b"")
            doc_zh[page.pageno].set_contents(page.page_xref)
            interpreter.process_page(page)

    device.close()
    return obj_patch

def translate_stream(
    stream: bytes,
    pages: Optional[list[int]] = None,
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    thread: int = 0,
    vfont: str = "",
    vchar: str = "",
    callback: object = None,
    cancellation_event: asyncio.Event = None,
    model: OnnxModel = None,
    envs: Dict = None,
    prompt: List = None,
    **kwarg: Any,
):
    # 获取系统字体
    system_fonts = get_system_fonts()
    
    # 选择字体顺序：宋体 -> 黑体 -> 微软雅黑 -> 楷体 -> 备用字体
    font_path = None
    for font_name in ["simsun", "simhei", "msyh", "simkai"]:
        if font_name in system_fonts:
            font_path = system_fonts[font_name]
            break
    
    # 如果没有找到系统字体，使用备用字体
    if font_path is None:
        font_path = get_fallback_font()
        if font_path is None:
            raise RuntimeError("No suitable font found and failed to get fallback font")
    
    # 初始化字体列表
    font_list = [("CustomFont", font_path)]
    noto = None
    
    if lang_out.lower() in resfont_map:  # CJK
        resfont = "CustomFont"
        # 已包含在初始字体列表中
    elif lang_out.lower() in noto_list:  # noto
        resfont = "CustomFont"
        # 已包含在初始字体列表中
    else:  # fallback
        resfont = "CustomFont"
        # 已包含在初始字体列表中

    doc_en = Document(stream=stream)
    stream = io.BytesIO()
    doc_en.save(stream)
    doc_zh = Document(stream=stream)
    page_count = doc_zh.page_count

    # 字体注册
    font_id = {}
    for page in doc_zh:
        for font in font_list:
            font_id[font[0]] = page.insert_font(font[0], font[1])

    # 处理每个 xref 的字体资源
    xreflen = doc_zh.xref_length()
    for xref in range(1, xreflen):
        for label in ["Resources/", ""]:  
            try:
                font_res = doc_zh.xref_get_key(xref, f"{label}Font")
                if font_res[0] == "dict":
                    for font in font_list:
                        font_exist = doc_zh.xref_get_key(xref, f"{label}Font/{font[0]}")
                        if font_exist[0] == "null":
                            doc_zh.xref_set_key(
                                xref,
                                f"{label}Font/{font[0]}",
                                f"{font_id[font[0]]} 0 R",
                            )
            except Exception:
                pass

    # 处理文档内容
    fp = io.BytesIO()
    doc_zh.save(fp)
    obj_patch: dict = translate_patch(fp, **locals())

    # 更新流内容
    for obj_id, ops_new in obj_patch.items():
        doc_zh.update_stream(obj_id, ops_new.encode())

    # 构建双语版本
    doc_en.insert_file(doc_zh)
    for id in range(page_count):
        doc_en.move_page(page_count + id, id * 2 + 1)

    return doc_zh.write(deflate=1), doc_en.write(deflate=1)

def convert_to_pdfa(input_path, output_path):
    """
    Convert PDF to PDF/A format

    Args:
        input_path: Path to source PDF file
        output_path: Path to save PDF/A file
    """
    from pikepdf import Dictionary, Name, Pdf

    # Open the PDF file
    pdf = Pdf.open(input_path)

    # Add PDF/A conformance metadata
    metadata = {
        "pdfa_part": "2",
        "pdfa_conformance": "B",
        "title": pdf.docinfo.get("/Title", ""),
        "author": pdf.docinfo.get("/Author", ""),
        "creator": "PDF Math Translate",
    }

    with pdf.open_metadata() as meta:
        meta.load_from_docinfo(pdf.docinfo)
        meta["pdfaid:part"] = metadata["pdfa_part"]
        meta["pdfaid:conformance"] = metadata["pdfa_conformance"]

    # Create OutputIntent dictionary
    output_intent = Dictionary(
        {
            "/Type": Name("/OutputIntent"),
            "/S": Name("/GTS_PDFA1"),
            "/OutputConditionIdentifier": "sRGB IEC61966-2.1",
            "/RegistryName": "http://www.color.org",
            "/Info": "sRGB IEC61966-2.1",
        }
    )

    # Add output intent to PDF root
    if "/OutputIntents" not in pdf.Root:
        pdf.Root.OutputIntents = [output_intent]
    else:
        pdf.Root.OutputIntents.append(output_intent)

    # Save as PDF/A
    pdf.save(output_path, linearize=True)
    pdf.close()

def translate(
    files: list[str],
    output: str = "",
    pages: Optional[list[int]] = None,
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    thread: int = 0,
    vfont: str = "",
    vchar: str = "",
    callback: object = None,
    compatible: bool = False,
    cancellation_event: asyncio.Event = None,
    model: OnnxModel = None,
    envs: Dict = None,
    prompt: List = None,
    **kwarg: Any,
):
    if not files:
        raise PDFValueError("No files to process.")

    missing_files = check_files(files)

    if missing_files:
        print("The following files do not exist:", file=sys.stderr)
        for file in missing_files:
            print(f"  {file}", file=sys.stderr)
        raise PDFValueError("Some files do not exist.")

    result_files = []

    for file in files:
        if file is str and (file.startswith("http://") or file.startswith("https://")):
            print("Online files detected, downloading...")
            try:
                r = requests.get(file, allow_redirects=True)
                if r.status_code == 200:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
                        print(f"Writing the file: {file}...")
                        tmp_file.write(r.content)
                        file = tmp_file.name
                else:
                    r.raise_for_status()
            except Exception as e:
                raise PDFValueError(
                    f"Errors occur in downloading the PDF file. Please check the link(s).\nError:\n{e}"
                )
        filename = os.path.splitext(os.path.basename(file))[0]

        # If the commandline has specified converting to PDF/A format
        if compatible:
            with tempfile.NamedTemporaryFile(suffix="-pdfa.pdf", delete=False) as tmp_pdfa:
                print(f"Converting {file} to PDF/A format...")
                convert_to_pdfa(file, tmp_pdfa.name)
                doc_raw = open(tmp_pdfa.name, "rb")
                os.unlink(tmp_pdfa.name)
        else:
            doc_raw = open(file, "rb")
        s_raw = doc_raw.read()
        doc_raw.close()

        if file.startswith(tempfile.gettempdir()):
            os.unlink(file)
        s_mono, s_dual = translate_stream(
            s_raw,
            **locals(),
        )
        file_mono = Path(output) / f"{filename}-mono.pdf"
        file_dual = Path(output) / f"{filename}-dual.pdf"
        doc_mono = open(file_mono, "wb")
        doc_dual = open(file_dual, "wb")
        doc_mono.write(s_mono)
        doc_dual.write(s_dual)
        doc_mono.close()
        doc_dual.close()
        result_files.append((str(file_mono), str(file_dual)))

    return result_files
