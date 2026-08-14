#!/usr/bin/env python3
"""部署预检脚本（跨平台：Windows PowerShell / macOS / Linux）。

功能：
1. 环境检测：识别当前操作系统与 shell（PowerShell / bash / cmd），
   并据此选择端口排查命令（Windows 用 netstat+tasklist，POSIX 用 lsof）。
2. 端口冲突排查：检查 Docker Compose 部署所需端口是否被本机占用
   （默认 8000 / 5432 / 6379 / 30000，可 --ports 自定义），
   并定位占用进程、给出处理建议。
3. MinerU 补丁（--patch）：把 MinerU 3.4.4 的 fast_api.py 补丁通过
   `docker exec -i <容器> python3 -` 以 stdin 注入方式执行，
   兼容 PowerShell（不使用 bash heredoc），流程幂等可重复执行。

用法：
    python scripts/deploy_preflight.py                      # 仅端口检查
    python scripts/deploy_preflight.py --patch              # 端口检查 + MinerU 补丁
    python scripts/deploy_preflight.py --ports 8000,5432    # 自定义检查端口
    python scripts/deploy_preflight.py --host 0.0.0.0       # 自定义探测地址
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys

# ============ 默认检查端口（与 docker-compose.yml 发布端口一致） ============
DEFAULT_PORTS = [
    (8000, "应用端口（app：8000:8000）"),
    (5432, "PostgreSQL（postgres：127.0.0.1:5432:5432）"),
    (6379, "Redis（redis：127.0.0.1:6379:6379）"),
    (30000, "MinerU OCR（mineru：30000:30000）"),
]

# ============ MinerU 补丁脚本（与 DEPLOY.md 第五章第 5 条逐字一致） ============
MINERU_PATCH_SCRIPT = """import pathlib
p = pathlib.Path('/usr/local/lib/python3.12/dist-packages/mineru/cli/fast_api.py')
s = p.read_text()
marker = '    parse_kwargs = dict(\\n'
ins = ("    _reserved = {'backend','device','output_dir','pdf_file_names','pdf_bytes_list',"
       "'p_lang_list','parse_method','effort','formula_enable','table_enable','image_analysis',"
       "'server_url','f_draw_layout_bbox','f_draw_span_bbox','f_dump_md','f_dump_middle_json',"
       "'f_dump_model_output','f_dump_orig_pdf','f_dump_content_list','start_page_id','end_page_id',"
       "'client_side_output_generation'}\\n"
       "    config = {k: v for k, v in config.items() if k not in _reserved}\\n\\n")
if '_reserved' not in s:
    p.write_text(s.replace(marker, ins + marker, 1))
    print('patched')
else:
    print('already patched')
"""

MINERU_FAST_API = "/usr/local/lib/python3.12/dist-packages/mineru/cli/fast_api.py"

# ============ 环境检测 ============

def detect_env() -> dict:
    """识别操作系统与 shell 环境。"""
    is_windows = sys.platform.startswith("win")
    if is_windows:
        # PowerShell 运行时会注入 PSModulePath 环境变量
        if "powershell" in os.environ.get("PSModulePath", "").lower():
            shell = "PowerShell"
        elif "powershell" in (os.environ.get("COMSPEC") or "").lower():
            shell = "PowerShell"
        elif "cmd" in (os.environ.get("COMSPEC") or "").lower():
            shell = "cmd.exe"
        else:
            shell = "PowerShell（默认）"
        os_label = "Windows"
    else:
        shell = (os.environ.get("SHELL") or "bash").rsplit("/", 1)[-1]
        os_label = "macOS" if sys.platform == "darwin" else "Linux"
    return {"is_windows": is_windows, "os": os_label, "shell": shell}

# ============ 端口冲突排查 ============

def _run(cmd: list) -> str:
    """执行命令并返回合并输出；命令不存在或超时返回空串。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return out + ("\n" + err if err else "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def port_in_use(host: str, port: int) -> bool:
    """探测本机端口是否已被占用（纯 Python socket，跨平台）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()

def _windows_owner(port: int) -> str:
    """Windows：netstat 取 PID + tasklist 查进程名。"""
    out = _run(["netstat", "-ano"])
    pids = []
    for ln in out.splitlines():
        parts = ln.split()
        if (len(parts) >= 5 and f":{port}" in parts[1]
                and "LISTENING" in ln and parts[-1].isdigit()):
            pids.append(parts[-1])
    if not pids:
        return "(netstat 未定位到，可执行 `netstat -ano | findstr :%d` 手动查看)" % port
    pid = pids[0]
    tasklist = _run(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"])
    for line in tasklist.splitlines():
        name = line.split(",")[0].strip().strip('"')
        if name:
            return "%s（PID %s）" % (name, pid)
    return "PID %s" % pid


def _posix_owner(port: int) -> str:
    """macOS / Linux：lsof 定位监听进程。"""
    out = _run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"])
    lines = out.splitlines()
    if len(lines) < 2:
        return "(lsof 未定位到)"
    return "、".join(" ".join(ln.split()[:2]) for ln in lines[1:][:5])


def find_port_owner(host: str, port: int, is_windows: bool) -> str:
    """定位占用端口的进程（按平台切换命令）。"""
    if is_windows:
        return _windows_owner(port)
    return _posix_owner(port)
def check_ports(host: str, ports: list, is_windows: bool) -> int:
    """逐一探测端口并输出结果；有冲突返回 1。"""
    print("\n========== 端口冲突排查（%s） ==========" % host)
    conflicts = []
    for port, label in ports:
        if port_in_use(host, port):
            owner = find_port_owner(host, port, is_windows)
            print("  [✗] 端口 %-6d %-42s 被占用：%s" % (port, label, owner))
            conflicts.append(port)
        else:
            print("  [✓] 端口 %-6d %-42s 空闲" % (port, label))
    if conflicts:
        print("\n⚠️  以下端口被占用，`docker compose up -d` 会报 port is already allocated：")
        for p in conflicts:
            print("    - %d" % p)
        print("处理建议：")
        print("    1. 停掉占用进程；或")
        print("    2. 修改 docker-compose.yml 的 ports 映射（如 8001:8000）；或")
        print("    3. 数据库/Redis 端口冲突时，改 .env 的 PG_PORT / REDIS_URL 并同步 compose 映射。")
        return 1
    print("\n端口检查全部通过。")
    return 0

# ============ MinerU 补丁 ============

def find_mineru_container():
    """自动发现运行中的 mineru 容器（兼容 case-mgmt-mineru 与 mineru-dev）。"""
    if shutil.which("docker") is None:
        print("  [✗] 未检测到 docker 命令，请先安装并启动 Docker Desktop / Docker Engine。")
        return None
    out = _run(["docker", "ps", "--format", "{{.Names}}"])
    for name in out.splitlines():
        if "mineru" in name.strip().lower():
            return name.strip()
    return None
def apply_mineru_patch(container: str) -> int:
    """备份 → stdin 注入补丁 → 语法校验 → 重启容器。"""
    print("\n========== MinerU fast_api.py 补丁（容器：%s） ==========" % container)
    fast_api = MINERU_FAST_API
    # 1. 幂等备份（cp -n：已存在备份时返回非 0 且无输出，属预期）
    subprocess.run(["docker", "exec", container, "cp", "-n", fast_api, fast_api + ".bak"],
                   capture_output=True, text=True, timeout=30)
    # 2. 通过 stdin 注入补丁脚本（PowerShell / bash 通用，无 heredoc）
    proc = subprocess.run(["docker", "exec", "-i", container, "python3", "-"],
                          input=MINERU_PATCH_SCRIPT, capture_output=True,
                          text=True, timeout=60)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or "patched" not in out:
        print("  [✗] 补丁执行失败（exit=%s）" % proc.returncode)
        if out:
            print(out)
        if err:
            print(err)
        return 1
    print("  [✓] %s" % out)  # patched / already patched
    # 3. 语法校验
    proc = subprocess.run(["docker", "exec", container, "python3", "-m", "py_compile", fast_api],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        print("  [✗] py_compile 语法校验失败：%s" % ((proc.stderr or proc.stdout).strip()))
        return 1
    print("  [✓] py_compile 语法校验通过")
    # 4. 重启容器生效
    proc = subprocess.run(["docker", "restart", container], capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        print("  [✗] 容器重启失败：%s" % ((proc.stderr or proc.stdout).strip()))
        return 1
    print("  [✓] 已重启容器 %s" % container)
    print("补丁完成。注意：补丁只写入运行中的容器，容器重建后需重新执行本脚本。")
    return 0

# ============ 主流程 ============

def main() -> int:
    parser = argparse.ArgumentParser(
        description="案件管理系统部署预检：端口冲突排查 + MinerU 补丁（跨平台）")
    parser.add_argument("--patch", action="store_true",
                        help="在端口检查通过后自动应用 MinerU fast_api.py 补丁")
    parser.add_argument("--ports", default="",
                        help="自定义检查端口（逗号分隔，默认 8000,5432,6379,30000）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="探测地址（默认 127.0.0.1）")
    args = parser.parse_args()
    env = detect_env()
    print("========== 环境检测 ==========")
    print("  操作系统 : %s" % env["os"])
    print("  Shell    : %s" % env["shell"])
    if env["is_windows"]:
        print("  [提示] 检测到 Windows 环境：端口排查使用 netstat+tasklist，"
              "MinerU 补丁使用 stdin 注入方式（兼容 PowerShell）。")
    else:
        print("  [提示] POSIX 环境：端口排查使用 lsof。")
    if args.ports:
        ports = [(int(p.strip()), "自定义") for p in args.ports.split(",") if p.strip().isdigit()]
    else:
        ports = DEFAULT_PORTS
    rc = check_ports(args.host, ports, env["is_windows"])
    if args.patch:
        if rc != 0:
            print("\n⚠️  存在端口冲突，请先解决后再执行 MinerU 补丁（--patch）。")
            return rc
        container = find_mineru_container()
        if container is None:
            print("\n[✗] 未检测到运行中的 MinerU 容器。")
            print("    请先启动 OCR 服务：`docker compose --profile ocr up -d`，")
            print("    确认容器（case-mgmt-mineru）运行后再执行：")
            print("        python scripts/deploy_preflight.py --patch")
            return 1
        rc = apply_mineru_patch(container)
    return rc


if __name__ == "__main__":
    sys.exit(main())
