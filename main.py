import os
import sys
import time
import json
import socket
import shutil
import threading
import platform
import subprocess
import ipaddress
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import qrcode
from PIL import Image, ImageTk
from flask import (
    Flask,
    request,
    render_template_string,
    redirect,
    url_for,
    flash,
    send_from_directory,
    jsonify,
)
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


APP_NAME = "局域网文件传输 Pro"
BASE_DIR = Path.cwd()
DEFAULT_UPLOAD_DIR = BASE_DIR / "uploads"
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "shared_files"

CONFIG = {
    "bind_ip": "0.0.0.0",
    "display_ip": "",
    "port": 8000,
    "subnet_mask": "255.255.255.0",
    "upload_dir": str(DEFAULT_UPLOAD_DIR),
    "download_dir": str(DEFAULT_DOWNLOAD_DIR),
    "max_file_mb": 1024,
    "allow_extensions": "*",
    "theme_name": "professional",
}

APP_STATE = {
    "server": None,
    "running": False,
    "server_url": "",
    "last_qr_pil": None,
    "upload_logs": [],
}


# =========================
# 工具函数
# =========================
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def ensure_dir(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)


def build_network(ip: str, mask: str) -> ipaddress.IPv4Network:
    interface = ipaddress.IPv4Interface(f"{ip}/{mask}")
    return interface.network


def client_ip_from_request() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.remote_addr or "0.0.0.0"


def is_same_lan(client_ip: str, server_ip: str, mask: str) -> bool:
    try:
        network = build_network(server_ip, mask)
        return ipaddress.ip_address(client_ip) in network
    except Exception:
        return False


def allowed_file(filename: str) -> bool:
    rule = CONFIG["allow_extensions"].strip()
    if rule in ("", "*"):
        return True
    ext = Path(filename).suffix.lower().lstrip(".")
    allow_set = {x.strip().lower().lstrip(".") for x in rule.split(",") if x.strip()}
    return ext in allow_set


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def list_shared_files() -> List[dict]:
    shared_dir = Path(CONFIG["download_dir"])
    ensure_dir(str(shared_dir))
    items = []
    for p in sorted(shared_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            stat = p.stat()
            items.append(
                {
                    "name": p.name,
                    "size": format_size(stat.st_size),
                    "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    return items


def save_uploaded_files(files) -> List[str]:
    upload_dir = CONFIG["upload_dir"]
    ensure_dir(upload_dir)
    saved_paths = []

    for f in files:
        if not f or not f.filename:
            continue
        if not allowed_file(f.filename):
            raise ValueError(f"不允许的文件类型：{f.filename}")

        safe_name = secure_filename(f.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        final_name = f"{timestamp}_{safe_name}"
        save_path = Path(upload_dir) / final_name
        f.save(save_path)
        saved_paths.append(str(save_path))

        APP_STATE["upload_logs"].append(
            {
                "filename": safe_name,
                "saved_as": final_name,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "size": format_size(save_path.stat().st_size),
                "client_ip": client_ip_from_request(),
            }
        )

    return saved_paths


def generate_qr_pil(url: str) -> Image.Image:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def open_dir(path: str):
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# =========================
# Flask Web
# =========================
app = Flask(__name__)
app.secret_key = "lan-file-transfer-pro"

INDEX_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }}</title>
  <style>
    :root{
      --bg:#0b1220;
      --panel:#121a2b;
      --panel2:#192338;
      --text:#eef4ff;
      --muted:#9fb0d1;
      --line:#2a3957;
      --primary:#4f8cff;
      --primary2:#326cf0;
      --danger:#ff6b6b;
      --success:#27c281;
      --warn:#f5b942;
      --shadow:0 20px 50px rgba(0,0,0,.25);
      --radius:20px;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:Segoe UI,Arial,Helvetica,sans-serif;background:linear-gradient(135deg,#0b1220,#111a2f 45%,#17243d);color:var(--text)}
    .shell{max-width:1080px;margin:0 auto;padding:22px}
    .hero{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:18px}
    .hero h1{margin:0;font-size:30px;font-weight:700}
    .hero p{margin:8px 0 0;color:var(--muted)}
    .grid{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}
    .card{background:rgba(18,26,43,.9);border:1px solid rgba(255,255,255,.06);box-shadow:var(--shadow);border-radius:var(--radius);padding:22px;backdrop-filter: blur(8px)}
    .section-title{font-size:18px;font-weight:700;margin:0 0 14px}
    .muted{color:var(--muted)}
    .pill{display:inline-flex;align-items:center;padding:7px 12px;border-radius:999px;background:#15223b;border:1px solid var(--line);color:#d9e7ff;font-size:13px}
    .status-ok{color:#9cf4c9}
    .status-bad{color:#ffb0b0}
    .dropzone{border:2px dashed #42639f;border-radius:18px;padding:26px;text-align:center;background:linear-gradient(180deg,rgba(79,140,255,.07),rgba(79,140,255,.02));transition:.2s}
    .dropzone.dragover{border-color:#7aa7ff;background:linear-gradient(180deg,rgba(79,140,255,.17),rgba(79,140,255,.06));transform:translateY(-1px)}
    .dropzone input{display:none}
    .btn{display:inline-flex;align-items:center;justify-content:center;padding:11px 18px;border:none;border-radius:14px;background:linear-gradient(135deg,var(--primary),var(--primary2));color:white;font-weight:600;cursor:pointer;text-decoration:none}
    .btn.secondary{background:#203150;border:1px solid #30466f}
    .btn.danger{background:#6d2b39}
    .row{display:flex;gap:10px;flex-wrap:wrap}
    .progress-wrap{margin-top:18px;display:none}
    .progress-bar{height:14px;background:#0f1628;border-radius:999px;overflow:hidden;border:1px solid #2a3957}
    .progress-inner{height:100%;width:0%;background:linear-gradient(90deg,#59d2ff,#5c85ff)}
    .progress-text{margin-top:8px;color:#dbe6ff;font-size:14px}
    .tips{margin-top:14px;font-size:14px;color:var(--muted)}
    .alert{padding:14px 16px;border-radius:14px;margin-bottom:14px;border:1px solid}
    .alert-success{background:rgba(39,194,129,.12);border-color:rgba(39,194,129,.35);color:#bff3da}
    .alert-error{background:rgba(255,107,107,.12);border-color:rgba(255,107,107,.3);color:#ffd0d0}
    .file-list{display:flex;flex-direction:column;gap:12px}
    .file-item{display:flex;justify-content:space-between;gap:14px;padding:14px;border:1px solid var(--line);background:var(--panel2);border-radius:16px}
    .file-item .meta{font-size:13px;color:var(--muted);margin-top:4px}
    .file-item .name{word-break:break-all;font-weight:600}
    .footer-note{margin-top:16px;color:var(--muted);font-size:13px}
    @media (max-width: 900px){.grid{grid-template-columns:1fr}.hero{flex-direction:column;align-items:flex-start}}
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <div>
        <h1>{{ app_name }}</h1>
        <p>扫码进入网页，手机与电脑在同一局域网时可上传文件，也可下载电脑共享文件。</p>
      </div>
      <div class="pill {{ 'status-ok' if same_lan else 'status-bad' }}">
        {{ '同一局域网，允许访问' if same_lan else '未连接同一局域网' }}
      </div>
    </div>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="alert {{ 'alert-success' if category == 'success' else 'alert-error' }}">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="grid">
      <div class="card">
        <div class="section-title">手机上传到电脑</div>
        {% if same_lan %}
          <div class="dropzone" id="dropzone">
            <p style="font-size:18px;margin-top:0;font-weight:700">拖拽文件到这里，或点击选择文件</p>
            <p class="muted">支持多文件上传。电脑端会自动保存到指定目录。</p>
            <input type="file" id="fileInput" multiple>
            <div class="row" style="justify-content:center;margin-top:12px">
              <button class="btn" onclick="document.getElementById('fileInput').click()">选择文件</button>
            </div>
          </div>

          <div class="progress-wrap" id="progressWrap">
            <div class="progress-bar"><div class="progress-inner" id="progressInner"></div></div>
            <div class="progress-text" id="progressText">准备上传...</div>
          </div>

          <div class="tips">
            手机 IP：{{ client_ip }}<br>
            电脑局域网：{{ network }}<br>
            上传目录：{{ upload_dir }}<br>
            大小限制：{{ max_file_mb }} MB<br>
            允许扩展名：{{ allow_extensions }}
          </div>
        {% else %}
          <div class="alert alert-error">
            检测到当前手机 IP 为 <b>{{ client_ip }}</b>，不在电脑所在网段 <b>{{ network }}</b> 中。<br>
            请把手机连接到与电脑相同的 Wi‑Fi，再重新扫码或刷新页面。
          </div>
        {% endif %}
      </div>

      <div class="card">
        <div class="section-title">电脑共享文件下载</div>
        {% if same_lan %}
          {% if shared_files %}
            <div class="file-list">
              {% for f in shared_files %}
                <div class="file-item">
                  <div>
                    <div class="name">{{ f.name }}</div>
                    <div class="meta">{{ f.size }} · {{ f.mtime }}</div>
                  </div>
                  <div>
                    <a class="btn secondary" href="{{ url_for('download_file', filename=f.name) }}">下载</a>
                  </div>
                </div>
              {% endfor %}
            </div>
          {% else %}
            <div class="muted">当前没有可下载文件。请先把文件放到电脑共享目录。</div>
          {% endif %}
        {% else %}
          <div class="muted">连接同一局域网后，才能查看并下载电脑共享文件。</div>
        {% endif %}
        <div class="footer-note">下载目录由桌面端设置。建议只放需要共享给手机的文件。</div>
      </div>
    </div>
  </div>

<script>
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const progressWrap = document.getElementById('progressWrap');
const progressInner = document.getElementById('progressInner');
const progressText = document.getElementById('progressText');

function bindUpload(files){
  if(!files || files.length === 0) return;
  const formData = new FormData();
  for(const f of files){
    formData.append('files', f);
  }

  progressWrap.style.display = 'block';
  progressInner.style.width = '0%';
  progressText.innerText = '准备上传...';

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload', true);

  xhr.upload.onprogress = function(e){
    if(e.lengthComputable){
      const percent = Math.round((e.loaded / e.total) * 100);
      progressInner.style.width = percent + '%';
      progressText.innerText = '上传中：' + percent + '%';
    }
  };

  xhr.onload = function(){
    if(xhr.status === 200){
      progressInner.style.width = '100%';
      try{
        const res = JSON.parse(xhr.responseText);
        progressText.innerText = res.message || '上传成功';
      }catch(err){
        progressText.innerText = '上传成功';
      }
      setTimeout(()=>location.reload(), 700);
    }else{
      try{
        const res = JSON.parse(xhr.responseText);
        progressText.innerText = res.message || '上传失败';
      }catch(err){
        progressText.innerText = '上传失败';
      }
    }
  };

  xhr.onerror = function(){
    progressText.innerText = '网络错误，上传失败';
  };

  xhr.send(formData);
}

if(fileInput){
  fileInput.addEventListener('change', function(){
    bindUpload(this.files);
  });
}

if(dropzone){
  ['dragenter','dragover'].forEach(evt => {
    dropzone.addEventListener(evt, function(e){
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add('dragover');
    });
  });

  ['dragleave','drop'].forEach(evt => {
    dropzone.addEventListener(evt, function(e){
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove('dragover');
    });
  });

  dropzone.addEventListener('drop', function(e){
    const files = e.dataTransfer.files;
    bindUpload(files);
  });
}
</script>
</body>
</html>
"""


@app.before_request
def apply_max_size():
    app.config["MAX_CONTENT_LENGTH"] = int(CONFIG["max_file_mb"]) * 1024 * 1024


@app.route("/", methods=["GET"])
def index():
    display_ip = CONFIG["display_ip"] or get_local_ip()
    client_ip = client_ip_from_request()
    network = build_network(display_ip, CONFIG["subnet_mask"])
    same_lan = is_same_lan(client_ip, display_ip, CONFIG["subnet_mask"])
    return render_template_string(
        INDEX_HTML,
        app_name=APP_NAME,
        same_lan=same_lan,
        client_ip=client_ip,
        network=str(network),
        upload_dir=CONFIG["upload_dir"],
        max_file_mb=CONFIG["max_file_mb"],
        allow_extensions=CONFIG["allow_extensions"],
        shared_files=list_shared_files(),
    )


@app.route("/upload", methods=["POST"])
def upload_form():
    return redirect(url_for("index"))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    display_ip = CONFIG["display_ip"] or get_local_ip()
    client_ip = client_ip_from_request()
    if not is_same_lan(client_ip, display_ip, CONFIG["subnet_mask"]):
        return jsonify({"ok": False, "message": "上传失败：请连接与电脑相同的局域网。"}), 403

    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "message": "没有选择文件。"}), 400

    try:
        saved = save_uploaded_files(files)
        if not saved:
            return jsonify({"ok": False, "message": "没有成功保存任何文件。"}), 400
        names = "、".join(Path(p).name for p in saved[:3])
        more = "" if len(saved) <= 3 else f" 等 {len(saved)} 个文件"
        return jsonify({"ok": True, "message": f"上传成功：{names}{more}"})
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"上传失败：{e}"}), 500


@app.route("/download/<path:filename>")
def download_file(filename):
    display_ip = CONFIG["display_ip"] or get_local_ip()
    client_ip = client_ip_from_request()
    if not is_same_lan(client_ip, display_ip, CONFIG["subnet_mask"]):
        flash("下载失败：请连接与电脑相同的局域网。", "error")
        return redirect(url_for("index"))
    return send_from_directory(CONFIG["download_dir"], filename, as_attachment=True)


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "running": APP_STATE["running"],
            "server_url": APP_STATE["server_url"],
            "upload_count": len(APP_STATE["upload_logs"]),
            "shared_count": len(list_shared_files()),
        }
    )


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "message": f"文件过大，当前限制为 {CONFIG['max_file_mb']} MB。"}), 413


# =========================
# Web Server 封装
# =========================
class FlaskServerThread(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.server = make_server(host, port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


# =========================
# Tkinter GUI
# =========================
class LanTransferGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1180x760")
        self.root.minsize(1080, 700)
        self.qr_label_image = None

        self.bind_ip_var = tk.StringVar(value="0.0.0.0")
        self.display_ip_var = tk.StringVar(value=get_local_ip())
        self.port_var = tk.StringVar(value="8000")
        self.mask_var = tk.StringVar(value="255.255.255.0")
        self.upload_dir_var = tk.StringVar(value=str(DEFAULT_UPLOAD_DIR))
        self.download_dir_var = tk.StringVar(value=str(DEFAULT_DOWNLOAD_DIR))
        self.max_mb_var = tk.StringVar(value="1024")
        self.ext_var = tk.StringVar(value="*")
        self.url_var = tk.StringVar(value="未启动")
        self.status_var = tk.StringVar(value="服务未启动")

        self.build_style()
        self.build_ui()
        self.refresh_network_hint()
        self.refresh_shared_files_box()

    def build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg="#0f172a")
        style.configure("TFrame", background="#0f172a")
        style.configure("Card.TLabelframe", background="#111827", foreground="#e5e7eb")
        style.configure("Card.TLabelframe.Label", background="#111827", foreground="#f8fafc", font=("Segoe UI", 11, "bold"))
        style.configure("TLabel", background="#0f172a", foreground="#e5e7eb", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 22, "bold"), foreground="#f8fafc")
        style.configure("Sub.TLabel", foreground="#94a3b8")
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", fieldbackground="#ffffff")

    def build_ui(self):
        shell = ttk.Frame(self.root, padding=18)
        shell.pack(fill="both", expand=True)

        top = ttk.Frame(shell)
        top.pack(fill="x", pady=(0, 14))
        ttk.Label(top, text=APP_NAME, style="Header.TLabel").pack(anchor="w")
        ttk.Label(top, text="电脑端设置参数、启动服务、生成二维码；手机端支持上传与下载。", style="Sub.TLabel").pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="right", fill="y", padx=(16, 0))

        config_card = ttk.LabelFrame(left, text="参数设置", style="Card.TLabelframe", padding=16)
        config_card.pack(fill="x")

        self._add_row(config_card, 0, "服务绑定 IP", self.bind_ip_var)
        self._add_row(config_card, 1, "二维码显示 IP", self.display_ip_var)
        self._add_row(config_card, 2, "端口", self.port_var)
        self._add_row(config_card, 3, "子网掩码", self.mask_var)
        self._add_row(config_card, 4, "上传目录", self.upload_dir_var, mode="dir")
        self._add_row(config_card, 5, "共享下载目录", self.download_dir_var, mode="dir")
        self._add_row(config_card, 6, "大小限制(MB)", self.max_mb_var)
        self._add_row(config_card, 7, "允许扩展名", self.ext_var)

        ttk.Label(
            config_card,
            text="说明：手机上传到“上传目录”；手机下载的是“共享下载目录”中的文件。扩展名示例：jpg,png,pdf,zip，全部允许填 *",
            wraplength=760,
            foreground="#94a3b8",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))

        btn_bar = ttk.Frame(left)
        btn_bar.pack(fill="x", pady=(12, 0))
        ttk.Button(btn_bar, text="启动服务", command=self.start_server, style="Accent.TButton").pack(side="left")
        ttk.Button(btn_bar, text="停止服务", command=self.stop_server).pack(side="left", padx=8)
        ttk.Button(btn_bar, text="生成二维码", command=self.generate_qr).pack(side="left")
        ttk.Button(btn_bar, text="打开上传目录", command=lambda: self.safe_open(self.upload_dir_var.get())).pack(side="left", padx=8)
        ttk.Button(btn_bar, text="打开共享目录", command=lambda: self.safe_open(self.download_dir_var.get())).pack(side="left")

        status_card = ttk.LabelFrame(left, text="运行状态与日志", style="Card.TLabelframe", padding=16)
        status_card.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(status_card, text="访问地址：").grid(row=0, column=0, sticky="nw")
        ttk.Entry(status_card, textvariable=self.url_var, width=80).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(status_card, text="状态：").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        ttk.Label(status_card, textvariable=self.status_var).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        self.network_hint = ttk.Label(status_card, text="", wraplength=760, foreground="#94a3b8")
        self.network_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.log_text = tk.Text(status_card, height=15, bg="#0b1220", fg="#e5e7eb", insertbackground="#e5e7eb", relief="flat")
        self.log_text.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(14, 0))
        status_card.columnconfigure(1, weight=1)
        status_card.rowconfigure(3, weight=1)

        qr_card = ttk.LabelFrame(right, text="二维码与共享文件", style="Card.TLabelframe", padding=16)
        qr_card.pack(fill="both", expand=True)
        self.qr_label = ttk.Label(qr_card, text="服务启动后显示二维码")
        self.qr_label.pack()
        self.qr_text = ttk.Label(qr_card, text="", wraplength=280, justify="center")
        self.qr_text.pack(pady=(8, 12))

        ttk.Label(qr_card, text="共享目录文件列表", foreground="#cbd5e1").pack(anchor="w")
        self.shared_box = tk.Listbox(qr_card, height=18, bg="#0b1220", fg="#e5e7eb", relief="flat")
        self.shared_box.pack(fill="both", expand=True, pady=(8, 0))

        mini_bar = ttk.Frame(qr_card)
        mini_bar.pack(fill="x", pady=(10, 0))
        ttk.Button(mini_bar, text="刷新列表", command=self.refresh_shared_files_box).pack(side="left")
        ttk.Button(mini_bar, text="导入文件到共享目录", command=self.add_files_to_share).pack(side="left", padx=8)

    def _add_row(self, parent, row, label, var, mode=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=var, width=56).grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=6)
        if mode == "dir":
            ttk.Button(parent, text="选择", command=lambda v=var: self.choose_dir(v)).grid(row=row, column=2, sticky="w")
        parent.columnconfigure(1, weight=1)

    def choose_dir(self, var):
        path = filedialog.askdirectory(initialdir=var.get() or str(BASE_DIR))
        if path:
            var.set(path)
            self.refresh_shared_files_box()

    def safe_open(self, path: str):
        ensure_dir(path)
        try:
            open_dir(path)
            self.log(f"已打开目录：{path}")
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def log(self, msg: str):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{now}] {msg}\n")
        self.log_text.see("end")

    def refresh_network_hint(self):
        try:
            network = build_network(self.display_ip_var.get().strip(), self.mask_var.get().strip())
            self.network_hint.config(text=f"当前将按局域网 {network} 判断手机是否与电脑在同一网络。")
        except Exception as e:
            self.network_hint.config(text=f"网络参数有误：{e}")

    def refresh_shared_files_box(self):
        self.shared_box.delete(0, "end")
        ensure_dir(self.download_dir_var.get())
        for item in list_shared_files():
            self.shared_box.insert("end", f"{item['name']}    [{item['size']}]")

    def add_files_to_share(self):
        files = filedialog.askopenfilenames(title="选择要共享给手机下载的文件")
        if not files:
            return
        target_dir = Path(self.download_dir_var.get())
        ensure_dir(str(target_dir))
        count = 0
        for fp in files:
            src = Path(fp)
            dst = target_dir / src.name
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
            count += 1
        self.refresh_shared_files_box()
        self.log(f"已导入 {count} 个文件到共享目录")

    def validate_and_apply_config(self) -> bool:
        try:
            bind_ip = self.bind_ip_var.get().strip()
            display_ip = self.display_ip_var.get().strip()
            port = int(self.port_var.get().strip())
            mask = self.mask_var.get().strip()
            upload_dir = self.upload_dir_var.get().strip()
            download_dir = self.download_dir_var.get().strip()
            max_mb = int(self.max_mb_var.get().strip())
            exts = self.ext_var.get().strip() or "*"

            ipaddress.ip_address(display_ip)
            build_network(display_ip, mask)
            if not 1 <= port <= 65535:
                raise ValueError("端口必须在 1~65535 之间")
            if max_mb <= 0:
                raise ValueError("大小限制必须大于 0")
            if not upload_dir or not download_dir:
                raise ValueError("目录不能为空")

            ensure_dir(upload_dir)
            ensure_dir(download_dir)

            CONFIG.update(
                {
                    "bind_ip": bind_ip,
                    "display_ip": display_ip,
                    "port": port,
                    "subnet_mask": mask,
                    "upload_dir": upload_dir,
                    "download_dir": download_dir,
                    "max_file_mb": max_mb,
                    "allow_extensions": exts,
                }
            )
            self.refresh_network_hint()
            self.refresh_shared_files_box()
            return True
        except Exception as e:
            messagebox.showerror("参数错误", str(e))
            return False

    def start_server(self):
        if APP_STATE["running"]:
            messagebox.showinfo("提示", "服务已经在运行")
            return
        if not self.validate_and_apply_config():
            return
        try:
            server = FlaskServerThread(CONFIG["bind_ip"], CONFIG["port"])
            server.start()
            APP_STATE["server"] = server
            APP_STATE["running"] = True
            APP_STATE["server_url"] = f"http://{CONFIG['display_ip']}:{CONFIG['port']}/"
            self.url_var.set(APP_STATE["server_url"])
            self.status_var.set("服务运行中")
            self.log(f"服务启动成功：{APP_STATE['server_url']}")
            self.log(f"上传目录：{CONFIG['upload_dir']}")
            self.log(f"共享目录：{CONFIG['download_dir']}")
            self.generate_qr()
        except OSError as e:
            messagebox.showerror("启动失败", f"端口可能被占用：{e}")
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def stop_server(self):
        server = APP_STATE.get("server")
        if not server or not APP_STATE["running"]:
            messagebox.showinfo("提示", "服务未启动")
            return
        try:
            server.shutdown()
            APP_STATE["server"] = None
            APP_STATE["running"] = False
            APP_STATE["server_url"] = ""
            self.url_var.set("未启动")
            self.status_var.set("服务已停止")
            self.qr_label.config(image="", text="服务已停止")
            self.qr_text.config(text="")
            self.log("服务已停止")
        except Exception as e:
            messagebox.showerror("停止失败", str(e))

    def generate_qr(self):
        if not self.validate_and_apply_config():
            return
        url = f"http://{CONFIG['display_ip']}:{CONFIG['port']}/"
        APP_STATE["server_url"] = url
        self.url_var.set(url)
        img = generate_qr_pil(url).resize((260, 260))
        tk_img = ImageTk.PhotoImage(img)
        self.qr_label_image = tk_img
        self.qr_label.config(image=tk_img, text="")
        self.qr_text.config(text=f"手机扫码访问\n{url}")
        self.log(f"二维码已生成：{url}")


def main():
    ensure_dir(CONFIG["upload_dir"])
    ensure_dir(CONFIG["download_dir"])
    root = tk.Tk()
    gui = LanTransferGUI(root)

    def on_close():
        try:
            if APP_STATE.get("running") and APP_STATE.get("server"):
                APP_STATE["server"].shutdown()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
