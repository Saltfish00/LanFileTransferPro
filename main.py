import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import ipaddress
from datetime import datetime
from pathlib import Path
from typing import List

import qrcode
from PIL import Image, ImageDraw, ImageTk
from flask import Flask, jsonify, redirect, render_template_string, request, send_from_directory, url_for
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Lan File Transfer Pro"


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
BASE_DIR = APP_DIR
CONFIG_PATH = APP_DIR / "config.json"
ASSETS_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "assets"
DEFAULTS = {
    "bind_ip": "0.0.0.0",
    "display_ip": "auto",
    "port": 8000,
    "subnet_mask": "255.255.255.0",
    "upload_dir": str(BASE_DIR / "uploads"),
    "download_dir": str(BASE_DIR / "shared_files"),
    "max_file_mb": 1024,
    "allow_extensions": "*",
}
CONFIG = DEFAULTS.copy()
APP_STATE = {"server": None, "running": False, "server_url": "", "upload_logs": []}


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        sock.close()
    return ip


def normalize_dir(path_value: str, default_dir: Path) -> str:
    raw = (path_value or "").strip()
    if not raw:
        return str(default_dir)
    path = Path(raw)
    if not path.is_absolute():
        path = APP_DIR / path
    return str(path.resolve())


def get_display_ip() -> str:
    value = str(CONFIG.get("display_ip", "auto") or "auto").strip()
    if value.lower() in ("", "auto"):
        return get_local_ip()
    return value


def load_config() -> None:
    CONFIG.clear()
    CONFIG.update(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                CONFIG.update(data)
        except Exception:
            pass

    CONFIG["upload_dir"] = normalize_dir(CONFIG.get("upload_dir", ""), APP_DIR / "uploads")
    CONFIG["download_dir"] = normalize_dir(CONFIG.get("download_dir", ""), APP_DIR / "shared_files")

    display_ip = str(CONFIG.get("display_ip", "auto") or "auto").strip()
    CONFIG["display_ip"] = "auto" if display_ip.lower() in ("", "auto") else display_ip

    ensure_dir(CONFIG["upload_dir"])
    ensure_dir(CONFIG["download_dir"])
    save_config()


def save_config() -> None:
    CONFIG_PATH.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


def build_network(ip: str, mask: str) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Interface(f"{ip}/{mask}").network


def client_ip_from_request() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or request.remote_addr or "0.0.0.0"


def is_same_lan(client_ip: str, server_ip: str, mask: str) -> bool:
    try:
        return ipaddress.ip_address(client_ip) in build_network(server_ip, mask)
    except Exception:
        return False


def allowed_file(filename: str) -> bool:
    rule = CONFIG["allow_extensions"].strip()
    if rule in ("", "*"):
        return True
    ext = Path(filename).suffix.lower().lstrip(".")
    allow_set = {part.strip().lower().lstrip(".") for part in rule.split(",") if part.strip()}
    return ext in allow_set


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def list_shared_files() -> List[dict]:
    ensure_dir(CONFIG["download_dir"])
    items = []
    for path in sorted(Path(CONFIG["download_dir"]).iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if path.is_file():
            stat = path.stat()
            items.append({
                "name": path.name,
                "size": format_size(stat.st_size),
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return items


def save_uploaded_files(files) -> List[str]:
    ensure_dir(CONFIG["upload_dir"])
    saved = []
    for file_obj in files:
        if not file_obj or not file_obj.filename:
            continue
        if not allowed_file(file_obj.filename):
            raise ValueError(f"不允许的文件类型：{file_obj.filename}")
        safe_name = secure_filename(file_obj.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        final_name = f"{timestamp}_{safe_name}"
        save_path = Path(CONFIG["upload_dir"]) / final_name
        file_obj.save(save_path)
        saved.append(str(save_path))
        APP_STATE["upload_logs"].append({
            "filename": safe_name,
            "saved_as": final_name,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "size": format_size(save_path.stat().st_size),
            "client_ip": client_ip_from_request(),
        })
    return saved


def generate_qr_pil(url: str) -> Image.Image:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def open_dir(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


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
      --bg:#f4f7fb; --panel:#ffffff; --soft:#f8fbff; --line:#e7edf5; --text:#18212f;
      --muted:#6f7c91; --primary:#4d7cff; --primary2:#6aa8ff; --ok:#1f9f6d; --bad:#d85252;
      --shadow:0 16px 40px rgba(31,50,81,.08); --radius:24px;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:"Segoe UI",Arial,sans-serif;background:linear-gradient(180deg,#f9fbfe,#f3f7fb);color:var(--text)}
    .shell{max-width:1120px;margin:0 auto;padding:28px 22px 36px}
    .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:18px}
    .brand h1{margin:0;font-size:30px;letter-spacing:.2px}
    .brand p{margin:8px 0 0;color:var(--muted);line-height:1.7}
    .status-chip{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border:1px solid var(--line);border-radius:999px;background:#fff;box-shadow:var(--shadow);font-size:14px}
    .dot{width:10px;height:10px;border-radius:999px;background:var(--bad)}
    .ok .dot{background:var(--ok)}
    .hero{display:grid;grid-template-columns:1.15fr .85fr;gap:18px;margin-bottom:18px}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px}
    .soft-card{background:linear-gradient(180deg,#ffffff,#fbfdff)}
    .title{font-size:18px;font-weight:700;margin:0 0 12px}
    .subtitle{font-size:14px;color:var(--muted);line-height:1.7;margin:0}
    .hero-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:18px}
    .metric{padding:16px;border-radius:18px;background:var(--soft);border:1px solid var(--line)}
    .metric .label{font-size:13px;color:var(--muted)}
    .metric .value{margin-top:6px;font-size:17px;font-weight:700;word-break:break-all}
    .grid{display:grid;grid-template-columns:1.15fr .85fr;gap:18px}
    .dropzone{border:1.8px dashed #bfd1ee;background:linear-gradient(180deg,#fbfdff,#f5f9ff);border-radius:22px;padding:34px 24px;text-align:center;transition:.2s}
    .dropzone.dragover{border-color:#7ba2ff;background:#eef4ff;transform:translateY(-1px)}
    .dropzone input{display:none}
    .upload-icon{width:60px;height:60px;border-radius:18px;background:linear-gradient(135deg,#edf4ff,#f8fbff);border:1px solid var(--line);display:flex;align-items:center;justify-content:center;margin:0 auto 14px;font-size:28px;color:#4d7cff}
    .btn{display:inline-flex;align-items:center;justify-content:center;padding:11px 18px;border:none;border-radius:14px;background:linear-gradient(135deg,var(--primary2),var(--primary));color:#fff;font-weight:600;cursor:pointer;text-decoration:none}
    .btn.secondary{background:#fff;color:#2f436d;border:1px solid var(--line)}
    .muted{color:var(--muted)}
    .tips{margin-top:14px;padding:16px;border-radius:18px;background:var(--soft);border:1px solid var(--line);font-size:14px;color:var(--muted);line-height:1.8}
    .progress-wrap{margin-top:18px;display:none}
    .progress-bar{height:12px;background:#eef3f9;border-radius:999px;overflow:hidden;border:1px solid var(--line)}
    .progress-inner{height:100%;width:0%;background:linear-gradient(90deg,#79c8ff,#567dff)}
    .progress-text{margin-top:8px;font-size:14px;color:#516078}
    .file-list{display:flex;flex-direction:column;gap:12px}
    .file-item{display:flex;justify-content:space-between;gap:12px;padding:15px;border-radius:18px;background:var(--soft);border:1px solid var(--line)}
    .name{font-weight:700;word-break:break-all}
    .meta{font-size:13px;color:var(--muted);margin-top:6px}
    .empty,.alert{padding:16px;border-radius:18px;border:1px solid var(--line);line-height:1.7}
    .empty{background:var(--soft);color:var(--muted)}
    .alert{background:#fff7f7;border-color:#ffd7d7;color:#b44e4e}
    .foot{margin-top:14px;font-size:13px;color:var(--muted)}
    @media (max-width: 920px){.hero,.grid{grid-template-columns:1fr}.topbar{flex-direction:column}}
  </style>
</head>
<body>
  <div class="shell">
    <div class="topbar">
      <div class="brand">
        <h1>{{ app_name }}</h1>
        <p>手机扫码后，如果与电脑连接在同一局域网，就可以直接上传文件到电脑，或下载电脑共享文件。</p>
      </div>
      <div class="status-chip {{ 'ok' if same_lan else '' }}"><span class="dot"></span>{{ '同一局域网，允许访问' if same_lan else '请连接同一局域网后访问' }}</div>
    </div>

    <div class="hero">
      <div class="card soft-card">
        <div class="title">更轻松的局域网文件传输</div>
        <p class="subtitle">不需要安装手机 App。扫码即可进入网页，上传文件到电脑，或下载电脑提前共享好的文件。</p>
        <div class="hero-stats">
          <div class="metric"><div class="label">手机 IP</div><div class="value">{{ client_ip }}</div></div>
          <div class="metric"><div class="label">电脑网段</div><div class="value">{{ network }}</div></div>
          <div class="metric"><div class="label">大小限制</div><div class="value">{{ max_file_mb }} MB</div></div>
        </div>
      </div>
      <div class="card">
        <div class="title">当前访问说明</div>
        {% if same_lan %}
          <p class="subtitle">已确认手机和电脑处于同一局域网。现在可以上传文件，也可以查看右侧的共享文件列表。</p>
        {% else %}
          <div class="alert">当前手机 IP 为 <b>{{ client_ip }}</b>，不在电脑所在网段 <b>{{ network }}</b> 中。请把手机连接到与电脑相同的 Wi‑Fi，再刷新页面。</div>
        {% endif %}
        <div class="tips">
          上传目录：{{ upload_dir }}<br>
          允许扩展名：{{ allow_extensions }}
        </div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="title">上传到电脑</div>
        {% if same_lan %}
          <div class="dropzone" id="dropzone">
            <div class="upload-icon">↑</div>
            <p style="font-size:18px;margin:0 0 8px;font-weight:700">拖拽文件到这里，或点击选择文件</p>
            <p class="muted" style="margin:0 0 14px">支持多文件上传，网页会显示实时进度。</p>
            <input type="file" id="fileInput" multiple>
            <button class="btn" onclick="document.getElementById('fileInput').click()">选择文件</button>
          </div>
          <div class="progress-wrap" id="progressWrap">
            <div class="progress-bar"><div class="progress-inner" id="progressInner"></div></div>
            <div class="progress-text" id="progressText">准备上传...</div>
          </div>
        {% else %}
          <div class="empty">连接到相同 Wi‑Fi 后，这里会显示上传入口。</div>
        {% endif %}
      </div>

      <div class="card">
        <div class="title">下载电脑共享文件</div>
        {% if same_lan %}
          {% if shared_files %}
            <div class="file-list">
              {% for f in shared_files %}
                <div class="file-item">
                  <div>
                    <div class="name">{{ f.name }}</div>
                    <div class="meta">{{ f.size }} · {{ f.mtime }}</div>
                  </div>
                  <a class="btn secondary" href="{{ url_for('download_file', filename=f.name) }}">下载</a>
                </div>
              {% endfor %}
            </div>
          {% else %}
            <div class="empty">共享目录目前为空。先在电脑端把需要分享的文件放进去。</div>
          {% endif %}
        {% else %}
          <div class="empty">连接同一局域网后，才能查看并下载电脑共享文件。</div>
        {% endif %}
        <div class="foot">建议只把需要分享给手机的文件放到共享目录。</div>
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
  for(const f of files){ formData.append('files', f); }
  progressWrap.style.display = 'block';
  progressInner.style.width = '0%';
  progressText.innerText = '准备上传...';
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload', true);
  xhr.upload.onprogress = function(e){
    if(e.lengthComputable){
      const p = Math.round((e.loaded / e.total) * 100);
      progressInner.style.width = p + '%';
      progressText.innerText = '上传中：' + p + '%';
    }
  };
  xhr.onload = function(){
    try {
      const res = JSON.parse(xhr.responseText);
      progressText.innerText = res.message || (xhr.status === 200 ? '上传成功' : '上传失败');
    } catch(err) {
      progressText.innerText = xhr.status === 200 ? '上传成功' : '上传失败';
    }
    if(xhr.status === 200){
      progressInner.style.width = '100%';
      setTimeout(() => location.reload(), 700);
    }
  };
  xhr.onerror = function(){ progressText.innerText = '网络错误，上传失败'; };
  xhr.send(formData);
}
if(fileInput){ fileInput.addEventListener('change', function(){ bindUpload(this.files); }); }
if(dropzone){
  ['dragenter','dragover'].forEach(evt => dropzone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); dropzone.classList.add('dragover'); }));
  ['dragleave','drop'].forEach(evt => dropzone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); dropzone.classList.remove('dragover'); }));
  dropzone.addEventListener('drop', e => bindUpload(e.dataTransfer.files));
}
</script>
</body>
</html>
"""


@app.before_request
def apply_max_size():
    app.config["MAX_CONTENT_LENGTH"] = int(CONFIG["max_file_mb"]) * 1024 * 1024


@app.route("/")
def index():
    display_ip = get_display_ip()
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


@app.route("/api/upload", methods=["POST"])
def api_upload():
    display_ip = get_display_ip()
    client_ip = client_ip_from_request()
    if not is_same_lan(client_ip, display_ip, CONFIG["subnet_mask"]):
        return jsonify({"ok": False, "message": "请连接与电脑相同的局域网。"}), 403
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "message": "没有选择文件。"}), 400
    try:
        saved = save_uploaded_files(files)
        if not saved:
            return jsonify({"ok": False, "message": "没有成功保存任何文件。"}), 400
        preview = "、".join(Path(p).name for p in saved[:3])
        suffix = "" if len(saved) <= 3 else f" 等 {len(saved)} 个文件"
        return jsonify({"ok": True, "message": f"上传成功：{preview}{suffix}"})
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"上传失败：{exc}"}), 500


@app.route("/download/<path:filename>")
def download_file(filename):
    display_ip = get_display_ip()
    if not is_same_lan(client_ip_from_request(), display_ip, CONFIG["subnet_mask"]):
        return redirect(url_for("index"))
    return send_from_directory(CONFIG["download_dir"], filename, as_attachment=True)


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"ok": False, "message": f"文件过大，当前限制为 {CONFIG['max_file_mb']} MB。"}), 413


class FlaskServerThread(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.server = make_server(host, port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()


class DesktopApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1160x780")
        self.root.minsize(1040, 700)
        self.qr_label_image = None

        self.bind_ip_var = tk.StringVar(value=CONFIG["bind_ip"])
        self.display_ip_var = tk.StringVar(value=CONFIG["display_ip"])
        self.port_var = tk.StringVar(value=str(CONFIG["port"]))
        self.mask_var = tk.StringVar(value=CONFIG["subnet_mask"])
        self.upload_dir_var = tk.StringVar(value=CONFIG["upload_dir"])
        self.download_dir_var = tk.StringVar(value=CONFIG["download_dir"])
        self.max_mb_var = tk.StringVar(value=str(CONFIG["max_file_mb"]))
        self.ext_var = tk.StringVar(value=CONFIG["allow_extensions"])
        self.url_var = tk.StringVar(value="未启动")
        self.status_var = tk.StringVar(value="服务未启动")
        self.config_visible = False

        self.build_style()
        self.build_ui()
        self.refresh_network_hint()
        self.refresh_shared_files_box()

    def build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg="#f4f7fb")
        style.configure("TFrame", background="#f4f7fb")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("Hero.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f4f7fb", foreground="#344256", font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background="#ffffff", foreground="#344256", font=("Segoe UI", 10))
        style.configure("Header.TLabel", background="#f4f7fb", foreground="#111827", font=("Segoe UI", 24, "bold"))
        style.configure("Muted.TLabel", background="#f4f7fb", foreground="#748297")
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#162132", font=("Segoe UI", 12, "bold"))
        style.configure("PanelMuted.TLabel", background="#ffffff", foreground="#748297")
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Secondary.TButton", font=("Segoe UI", 10))

    def build_ui(self) -> None:
        self.shell = ttk.Frame(self.root, padding=20)
        self.shell.pack(fill="both", expand=True)

        header = ttk.Frame(self.shell)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(anchor="w")
        ttk.Label(header, text=" ", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(self.shell)
        actions.pack(fill="x", pady=(0, 14))
        ttk.Button(actions, text="启动服务", command=self.start_server, style="Primary.TButton").pack(side="left")
        ttk.Button(actions, text="停止服务", command=self.stop_server, style="Secondary.TButton").pack(side="left", padx=8)
        ttk.Button(actions, text="配置", command=self.toggle_config, style="Secondary.TButton").pack(side="left")
        ttk.Button(actions, text="生成二维码", command=self.generate_qr, style="Secondary.TButton").pack(side="left", padx=8)
        ttk.Button(actions, text="打开上传目录", command=lambda: self.safe_open(self.upload_dir_var.get()), style="Secondary.TButton").pack(side="left")
        ttk.Button(actions, text="打开共享目录", command=lambda: self.safe_open(self.download_dir_var.get()), style="Secondary.TButton").pack(side="left", padx=8)

        self.config_wrap = ttk.Frame(self.shell)
        self.config_card = tk.Frame(self.config_wrap, bg="#ffffff", bd=1, highlightthickness=1, highlightbackground="#e7edf5")
        self._build_config_contents(self.config_card)

        hero = tk.Frame(self.shell, bg="#ffffff", bd=1, highlightthickness=1, highlightbackground="#e7edf5")
        hero.pack(fill="x", pady=(0, 14))
        left_hero = tk.Frame(hero, bg="#ffffff")
        left_hero.pack(side="left", fill="both", expand=True, padx=18, pady=18)
        right_hero = tk.Frame(hero, bg="#f8fbff", bd=0, highlightthickness=1, highlightbackground="#e7edf5")
        right_hero.pack(side="right", fill="y", padx=(0,18), pady=18)

        tk.Label(left_hero, text="服务状态", bg="#ffffff", fg="#162132", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(left_hero, text=" ", bg="#ffffff", fg="#748297", font=("Segoe UI", 10)).pack(anchor="w", pady=(6, 14))

        stats = tk.Frame(left_hero, bg="#ffffff")
        stats.pack(fill="x")
        self.stat_url = self._mini_stat(stats, "访问地址", self.url_var.get())
        self.stat_status = self._mini_stat(stats, "服务状态", self.status_var.get())
        self.stat_network = self._mini_stat(stats, "网络判断", "等待配置")

        tk.Label(right_hero, text="操作建议", bg="#f8fbff", fg="#162132", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(16,8))
        tk.Label(right_hero, text="1. 点击启动服务\n2. 用手机扫码访问\n3. 同网段即可上传或下载", justify="left", bg="#f8fbff", fg="#607086", font=("Segoe UI", 10), wraplength=220).pack(anchor="w", padx=16, pady=(0,16))

        content = ttk.Frame(self.shell)
        content.pack(fill="both", expand=True)

        left = tk.Frame(content, bg="#ffffff", bd=1, highlightthickness=1, highlightbackground="#e7edf5")
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(content, bg="#ffffff", bd=1, highlightthickness=1, highlightbackground="#e7edf5")
        right.pack(side="right", fill="y", padx=(16, 0))

        topbar = tk.Frame(left, bg="#ffffff")
        topbar.pack(fill="x", padx=18, pady=(18, 10))
        tk.Label(topbar, text="运行日志", bg="#ffffff", fg="#162132", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(topbar, text=" ", bg="#ffffff", fg="#748297", font=("Segoe UI", 10)).pack(anchor="w", pady=(4,0))

        url_row = tk.Frame(left, bg="#ffffff")
        url_row.pack(fill="x", padx=18, pady=(0, 10))
        tk.Label(url_row, text="访问地址", bg="#ffffff", fg="#607086", font=("Segoe UI", 10)).pack(anchor="w")
        self.url_entry = tk.Entry(url_row, textvariable=self.url_var, relief="flat", bd=0, bg="#f8fbff", fg="#1b2535", font=("Segoe UI", 11))
        self.url_entry.pack(fill="x", pady=(6,0), ipady=10)

        self.network_hint = tk.Label(left, text="", bg="#ffffff", fg="#748297", font=("Segoe UI", 10), wraplength=720, justify="left")
        self.network_hint.pack(fill="x", padx=18, pady=(0, 12))

        self.log_text = tk.Text(left, height=18, bg="#f8fbff", fg="#1f2937", insertbackground="#1f2937", relief="flat", bd=0, font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True, padx=18, pady=(0,18))

        tk.Label(right, text="二维码", bg="#ffffff", fg="#162132", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=18, pady=(18,8))
        self.qr_label = tk.Label(right, text="服务启动后显示二维码", bg="#ffffff", fg="#748297")
        self.qr_label.pack(padx=18)
        self.qr_text = tk.Label(right, text="", justify="center", wraplength=280, bg="#ffffff", fg="#607086", font=("Segoe UI", 10))
        self.qr_text.pack(padx=18, pady=(8, 14))
        tk.Label(right, text="共享文件", bg="#ffffff", fg="#162132", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=18, pady=(0,8))
        self.shared_box = tk.Listbox(right, height=18, bg="#f8fbff", fg="#1f2937", relief="flat", bd=0, font=("Segoe UI", 10), selectbackground="#dce8ff")
        self.shared_box.pack(fill="both", expand=True, padx=18)
        side_actions = ttk.Frame(right)
        side_actions.pack(fill="x", padx=18, pady=14)
        ttk.Button(side_actions, text="刷新列表", command=self.refresh_shared_files_box).pack(side="left")
        ttk.Button(side_actions, text="导入共享文件", command=self.add_files_to_share).pack(side="left", padx=8)

    def _mini_stat(self, parent, label, value):
        box = tk.Frame(parent, bg="#f8fbff", bd=0, highlightthickness=1, highlightbackground="#e7edf5")
        box.pack(side="left", fill="x", expand=True, padx=(0,10), ipadx=10, ipady=10)
        tk.Label(box, text=label, bg="#f8fbff", fg="#748297", font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10,2))
        value_label = tk.Label(box, text=value, bg="#f8fbff", fg="#162132", font=("Segoe UI", 11, "bold"), wraplength=220, justify="left")
        value_label.pack(anchor="w", padx=12, pady=(0,10))
        return value_label

    def _build_config_contents(self, parent) -> None:
        tk.Label(parent, text="配置面板", bg="#ffffff", fg="#162132", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=18, pady=(16,4))
        tk.Label(parent, text="二维码显示 IP 可填 auto，程序每次启动会自动读取当前电脑 IP。", bg="#ffffff", fg="#748297", font=("Segoe UI", 10)).grid(row=1, column=0, columnspan=3, sticky="w", padx=18, pady=(0,12))
        rows = [
            ("服务绑定 IP", self.bind_ip_var, None),
            ("二维码显示 IP", self.display_ip_var, None),
            ("端口", self.port_var, None),
            ("子网掩码", self.mask_var, None),
            ("上传目录", self.upload_dir_var, "dir"),
            ("共享下载目录", self.download_dir_var, "dir"),
            ("大小限制(MB)", self.max_mb_var, None),
            ("允许扩展名", self.ext_var, None),
        ]
        for idx, (label, variable, mode) in enumerate(rows, start=2):
            tk.Label(parent, text=label, bg="#ffffff", fg="#425066", font=("Segoe UI", 10)).grid(row=idx, column=0, sticky="w", padx=18, pady=8)
            entry = tk.Entry(parent, textvariable=variable, relief="flat", bd=0, bg="#f8fbff", fg="#1b2535", font=("Segoe UI", 10))
            entry.grid(row=idx, column=1, sticky="ew", padx=(10, 8), pady=8, ipady=8)
            if mode == "dir":
                ttk.Button(parent, text="选择", command=lambda v=variable: self.choose_dir(v)).grid(row=idx, column=2, sticky="w", padx=(0,18))
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_columnconfigure(0, minsize=120)

    def toggle_config(self) -> None:
        if self.config_visible:
            self.config_wrap.pack_forget()
            self.config_visible = False
        else:
            self.config_wrap.pack(fill="x", pady=(0, 14), after=self.shell.winfo_children()[1])
            self.config_card.pack(fill="x")
            self.config_visible = True

    def choose_dir(self, variable: tk.StringVar) -> None:
        initial_dir = variable.get().strip() or str(APP_DIR)
        path = filedialog.askdirectory(initialdir=initial_dir)
        if path:
            variable.set(path)
            self.refresh_shared_files_box()

    def safe_open(self, path: str) -> None:
        ensure_dir(path)
        try:
            open_dir(path)
            self.log(f"已打开目录：{path}")
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def log(self, msg: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{now}] {msg}\n")
        self.log_text.see("end")

    def refresh_network_hint(self) -> None:
        try:
            display_ip = self.display_ip_var.get().strip()
            if display_ip.lower() in ("", "auto"):
                display_ip = get_local_ip()
            network = build_network(display_ip, self.mask_var.get().strip())
            tip = f"程序将按局域网 {network} 判断手机与电脑是否在同一网络。"
            self.network_hint.config(text=tip)
            self.stat_network.config(text=str(network))
        except Exception as exc:
            self.network_hint.config(text=f"网络参数有误：{exc}")
            self.stat_network.config(text="参数错误")

    def refresh_shared_files_box(self) -> None:
        self.shared_box.delete(0, "end")
        CONFIG["download_dir"] = normalize_dir(self.download_dir_var.get(), APP_DIR / "shared_files")
        self.download_dir_var.set(CONFIG["download_dir"])
        ensure_dir(CONFIG["download_dir"])
        for item in list_shared_files():
            self.shared_box.insert("end", f"{item['name']}    [{item['size']}]")

    def add_files_to_share(self) -> None:
        files = filedialog.askopenfilenames(title="选择要共享给手机下载的文件")
        if not files:
            return
        target_dir = Path(self.download_dir_var.get())
        ensure_dir(str(target_dir))
        for file_path in files:
            src = Path(file_path)
            dst = target_dir / src.name
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
        self.refresh_shared_files_box()
        self.log(f"已导入 {len(files)} 个文件到共享目录")

    def validate_and_apply_config(self) -> bool:
        try:
            display_ip = self.display_ip_var.get().strip() or "auto"
            if display_ip.lower() not in ("auto",):
                ipaddress.ip_address(display_ip)
                network_ip = display_ip
            else:
                network_ip = get_local_ip()
            build_network(network_ip, self.mask_var.get().strip())
            port = int(self.port_var.get().strip())
            max_mb = int(self.max_mb_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError("端口必须在 1~65535 之间")
            if max_mb <= 0:
                raise ValueError("大小限制必须大于 0")
            bind_ip = self.bind_ip_var.get().strip() or "0.0.0.0"
            CONFIG.update({
                "bind_ip": bind_ip,
                "display_ip": "auto" if display_ip.lower() in ("", "auto") else display_ip,
                "port": port,
                "subnet_mask": self.mask_var.get().strip(),
                "upload_dir": normalize_dir(self.upload_dir_var.get(), APP_DIR / "uploads"),
                "download_dir": normalize_dir(self.download_dir_var.get(), APP_DIR / "shared_files"),
                "max_file_mb": max_mb,
                "allow_extensions": self.ext_var.get().strip() or "*",
            })
            self.bind_ip_var.set(CONFIG["bind_ip"])
            self.display_ip_var.set(CONFIG["display_ip"])
            self.upload_dir_var.set(CONFIG["upload_dir"])
            self.download_dir_var.set(CONFIG["download_dir"])
            ensure_dir(CONFIG["upload_dir"])
            ensure_dir(CONFIG["download_dir"])
            save_config()
            self.refresh_network_hint()
            self.refresh_shared_files_box()
            return True
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return False

    def start_server(self) -> None:
        if APP_STATE["running"]:
            messagebox.showinfo("提示", "服务已经在运行")
            return
        if not self.validate_and_apply_config():
            return
        try:
            server = FlaskServerThread(CONFIG["bind_ip"], CONFIG["port"])
            server.start()
            APP_STATE.update({
                "server": server,
                "running": True,
                "server_url": f"http://{get_display_ip()}:{CONFIG['port']}/",
            })
            self.url_var.set(APP_STATE["server_url"])
            self.status_var.set("服务运行中")
            self.stat_url.config(text=self.url_var.get())
            self.stat_status.config(text=self.status_var.get())
            self.log(f"服务启动成功：{APP_STATE['server_url']}")
            self.generate_qr()
        except OSError as exc:
            messagebox.showerror("启动失败", f"端口可能已被占用：{exc}")
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))

    def stop_server(self) -> None:
        server = APP_STATE.get("server")
        if not server or not APP_STATE["running"]:
            messagebox.showinfo("提示", "服务未启动")
            return
        try:
            server.shutdown()
            APP_STATE.update({"server": None, "running": False, "server_url": ""})
            self.url_var.set("未启动")
            self.status_var.set("服务已停止")
            self.stat_url.config(text=self.url_var.get())
            self.stat_status.config(text=self.status_var.get())
            self.qr_label.config(image="", text="服务已停止")
            self.qr_text.config(text="")
            self.log("服务已停止")
        except Exception as exc:
            messagebox.showerror("停止失败", str(exc))

    def generate_qr(self) -> None:
        if not self.validate_and_apply_config():
            return
        url = f"http://{get_display_ip()}:{CONFIG['port']}/"
        APP_STATE["server_url"] = url
        self.url_var.set(url)
        self.stat_url.config(text=self.url_var.get())
        img = generate_qr_pil(url).resize((260, 260))
        tk_img = ImageTk.PhotoImage(img)
        self.qr_label_image = tk_img
        self.qr_label.config(image=tk_img, text="")
        self.qr_text.config(text=f"手机扫码访问\n{url}")
        self.log(f"二维码已生成：{url}")

def create_app_icon() -> None:
    icon_path = ASSETS_DIR / "app.ico"
    ensure_dir(str(icon_path.parent))
    if icon_path.exists():
        return
    image = Image.new("RGBA", (256, 256), (245, 248, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, 232, 232), radius=52, fill=(74, 119, 255, 255))
    draw.rounded_rectangle((62, 62, 194, 194), radius=28, fill=(255, 255, 255, 255))
    draw.rounded_rectangle((92, 96, 164, 160), radius=14, fill=(74, 119, 255, 255))
    draw.polygon([(128, 178), (84, 136), (106, 136), (106, 112), (150, 112), (150, 136), (172, 136)], fill=(74, 119, 255, 255))
    image.save(icon_path, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


def main() -> None:
    load_config()
    create_app_icon()
    root = tk.Tk()
    app_ui = DesktopApp(root)

    def on_close() -> None:
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
