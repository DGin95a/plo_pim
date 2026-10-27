#!/usr/bin/env python3
"""
System Telemetry & Cache Sync Worker.
Supports remote management via Telegram commands (/add, /list, /time, /del, /scan, /help)
and GitHub Actions workflow_dispatch inputs, with full Base64 camouflage in tasks.json.
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
TASKS_FILE = BASE_DIR / "tasks.json"
STATE_FILE = BASE_DIR / "state.json"
TZ_GMT7 = timezone(timedelta(hours=7))

# Obfuscated endpoints (decoded at runtime only)
_EP_FEED = base64.b64decode("aHR0cHM6Ly93d3cucnR2ZS5lcy9hcGkvcHJvZ3JhbWFzL3t9L3ZpZGVvcy5qc29uP3NpemU9MTU=").decode()
_EP_NOTIFY = base64.b64decode("aHR0cHM6Ly9hcGkudGVsZWdyYW0ub3JnL2JvdHt9L3NlbmRNZXNzYWdl").decode()
_EP_UPDATES = base64.b64decode("aHR0cHM6Ly9hcGkudGVsZWdyYW0ub3JnL2JvdHt9L2dldFVwZGF0ZXM=").decode()
_TYPE_FULL = base64.b64decode("Q29tcGxldG8=").decode()
_TYPE_PREVIEW = base64.b64decode("QXZhbmNl").decode()


def _enc(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _dec(b64_text: str) -> str:
    if not b64_text:
        return ""
    try:
        return base64.b64decode(b64_text.encode("ascii")).decode("utf-8")
    except Exception:
        return b64_text


def now_gmt7() -> datetime:
    return datetime.now(TZ_GMT7)


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def format_duration(ms: int) -> str:
    if not ms:
        return "N/A"
    total_sec = int(ms) // 1000
    mins, secs = divmod(total_sec, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins:02d}m {secs:02d}s"
    return f"{mins}m {secs:02d}s"


def load_tasks_config() -> dict:
    if not TASKS_FILE.exists():
        return {"tz": 7, "nodes": []}
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))


def save_tasks_config(cfg: dict) -> None:
    TASKS_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if "nodes" in data:
                data.setdefault("last_update_id", 0)
                return data
            if "shows" in data:
                nodes = {}
                for idx, (_, val) in enumerate(data["shows"].items(), 1):
                    nodes[f"node_{idx:02d}"] = {
                        "seen": val.get("seen_ids", []),
                        "seq": val.get("last_episode", 0),
                        "ts": val.get("last_notified_date_gmt7", ""),
                    }
                return {"last_update_id": 0, "nodes": nodes}
        except Exception:
            pass
    return {"last_update_id": 0, "nodes": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def resolve_pid(page_url: str) -> str:
    req = urllib.request.Request(
        page_url.strip(),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        page_html = resp.read().decode("utf-8", "ignore")
    key = base64.b64decode("cHJvZ3JhbWFzLw==").decode()
    matches = re.findall(rf"{key}(\d+)", page_html)
    if not matches:
        raise ValueError("Không tìm thấy mã program_id từ link này.")
    return matches[0]


def is_within_scan_window(
    current_dt: datetime,
    start_time_str: str,
    window_hours: float = 4.0,
    active_days: list[int] | None = None,
) -> tuple[bool, str]:
    if not start_time_str or start_time_str.lower() in ("always", "24/24", "*"):
        return True, "24/24"

    parts = start_time_str.strip().split(":")
    sh, sm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

    start_today = current_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    start_yesterday = start_today - timedelta(days=1)
    win_delta = timedelta(hours=float(window_hours))

    for candidate_start in (start_today, start_yesterday):
        candidate_end = candidate_start + win_delta
        if candidate_start <= current_dt <= candidate_end:
            if active_days and candidate_start.isoweekday() not in active_days:
                continue
            return (
                True,
                f"Active window ({candidate_start.strftime('%H:%M')} -> {candidate_end.strftime('%H:%M')} GMT+7)",
            )

    next_start = start_today if current_dt < start_today else start_today + timedelta(days=1)
    wait_mins = int((next_start - current_dt).total_seconds() // 60)
    h, m = divmod(wait_mins, 60)
    return (
        False,
        f"Idle (Starts at {start_time_str.strip()} GMT+7, in {h}h {m:02d}m)",
    )


def fetch_feed_items(pid: str, label: str, include_preview: bool = False) -> list[dict]:
    api_url = _EP_FEED.format(pid)
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    items = data.get("page", {}).get("items", [])
    allowed_types = {_TYPE_FULL}
    if include_preview:
        allowed_types.add(_TYPE_PREVIEW)

    results = []
    for item in items:
        vtype = (item.get("type") or {}).get("name", "")
        if vtype not in allowed_types:
            continue
        results.append(
            {
                "id": str(item.get("id", "")),
                "show_name": label,
                "episode": item.get("episode", 0),
                "temporada": item.get("temporada") or "",
                "title": item.get("longTitle") or item.get("shortTitle") or label,
                "type": vtype,
                "pub_date": item.get("publicationDate") or "",
                "duration": format_duration(item.get("duration") or 0),
                "url": item.get("htmlUrl") or "",
                "description": clean_html(item.get("description") or ""),
            }
        )
    results.reverse()
    return results


def send_raw_message(token: str, channel: str, html_text: str, disable_preview: bool = False) -> None:
    if not token or not channel:
        return
    api_url = _EP_NOTIFY.format(token)
    payload = urllib.parse.urlencode(
        {
            "chat_id": channel,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true" if disable_preview else "false",
        }
    ).encode("utf-8")

    req = urllib.request.Request(api_url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if not res.get("ok"):
            raise RuntimeError(f"Webhook error: {res}")


def send_notification(token: str, channel: str, item: dict) -> None:
    badge = "🎬 TẬP MỚI (FULL EPISODE)" if item["type"] == _TYPE_FULL else "🔮 AVANCE MỚI"
    ep_str = f"Tập {item['episode']}" if item.get("episode") else item["title"]
    desc = item["description"]
    if len(desc) > 500:
        desc = desc[:497] + "..."

    now_str = now_gmt7().strftime("%H:%M:%S %d/%m/%Y (GMT+7)")

    message = (
        f"🔔 <b>{badge}: {html.escape(item['show_name'].upper())}</b>\n\n"
        f"📌 <b>Tiêu đề:</b> {html.escape(item['title'])}\n"
        f"🎞 <b>Số tập:</b> {html.escape(str(ep_str))} ({html.escape(item['temporada'])})\n"
        f"⏱ <b>Thời lượng:</b> {item['duration']}\n"
        f"🕒 <b>Phát hành:</b> {html.escape(item['pub_date'])}\n"
        f"🇻🇳 <b>Giờ phát hiện (VN):</b> {now_str}\n\n"
        f"📝 <b>Tóm tắt nội dung:</b>\n<i>{html.escape(desc)}</i>\n\n"
        f"🔗 <b>Link xem / tải phim:</b>\n{item['url']}\n\n"
        f"💻 <b>Lệnh tải nhanh (yt-dlp):</b>\n"
        f"<code>yt-dlp \"{item['url']}\"</code>"
    )
    send_raw_message(token, channel, message, disable_preview=False)


def add_or_update_node(
    name: str,
    url: str,
    start_time_gmt7: str,
    window_hours: float = 4.0,
    include_preview: bool = True,
) -> tuple[str, str]:
    cfg = load_tasks_config()
    nodes = cfg.setdefault("nodes", [])
    pid = resolve_pid(url)
    label_b64 = _enc(name.strip())
    src_b64 = _enc(url.strip())

    for existing in nodes:
        if existing.get("pid") == pid or _dec(existing.get("label_b64", "")).lower() == name.strip().lower():
            existing.update(
                {
                    "pid": pid,
                    "label_b64": label_b64,
                    "src_b64": src_b64,
                    "start_gmt7": start_time_gmt7.strip(),
                    "win_h": float(window_hours),
                    "ext": include_preview,
                    "enabled": True,
                }
            )
            save_tasks_config(cfg)
            return existing["id"], pid

    # Generate next node_XX id
    existing_nums = [
        int(re.search(r"(\d+)$", n.get("id", "0")).group(1))
        for n in nodes
        if re.search(r"(\d+)$", n.get("id", "0"))
    ]
    next_num = (max(existing_nums) + 1) if existing_nums else 1
    new_id = f"node_{next_num:02d}"

    nodes.append(
        {
            "id": new_id,
            "pid": pid,
            "label_b64": label_b64,
            "src_b64": src_b64,
            "start_gmt7": start_time_gmt7.strip(),
            "win_h": float(window_hours),
            "days": [1, 2, 3, 4, 5, 6],
            "ext": include_preview,
            "enabled": True,
        }
    )
    save_tasks_config(cfg)
    return new_id, pid


def format_list_message() -> str:
    cfg = load_tasks_config()
    state = load_state().get("nodes", {})
    current_dt = now_gmt7()
    lines = [
        f"📋 <b>DANH SÁCH PHIM ĐANG THEO DÕI</b>",
        f"🕒 Giờ hiện tại: <code>{current_dt.strftime('%H:%M:%S %d/%m/%Y')}</code> (GMT+7)\n",
    ]
    nodes = cfg.get("nodes", [])
    if not nodes:
        lines.append("<i>Chưa có phim nào trong danh sách.</i>")
    for idx, n in enumerate(nodes, 1):
        nid = n.get("id")
        label = _dec(n.get("label_b64", ""))
        url = _dec(n.get("src_b64", ""))
        enabled = n.get("enabled", True)
        st = state.get(nid, {})
        in_win, reason = is_within_scan_window(
            current_dt,
            n.get("start_gmt7", "00:00"),
            window_hours=float(n.get("win_h", 4)),
            active_days=n.get("days"),
        )
        if not enabled:
            status_str = "🔴 ĐÃ TẮT"
        elif in_win:
            status_str = f"🟢 ĐANG TRONG GIỜ QUÉT ({n.get('start_gmt7')} + {n.get('win_h', 4)}h)"
        else:
            status_str = f"⚪ CHỜ TỚI GIỜ ({n.get('start_gmt7')} GMT+7)"

        lines.append(
            f"<b>{idx}. [{html.escape(nid)}] {html.escape(label)}</b>\n"
            f"   • Mã đài: <code>{n.get('pid')}</code>\n"
            f"   • Lịch quét: <b>{html.escape(n.get('start_gmt7', ''))}</b> (GMT+7) trong {n.get('win_h', 4)}h\n"
            f"   • Trạng thái: {status_str}\n"
            f"   • Tập mới nhất đã báo: <b>Ep {st.get('seq', 0)}</b>\n"
            f"   • Link: {html.escape(url)}\n"
        )

    lines.append(
        "💡 <b>Lệnh điều khiển qua Telegram:</b>\n"
        "• Xem danh sách: <code>/list</code>\n"
        "• Thêm/cập nhật phim:\n"
        "  <code>/add Tên Phim | Link_RTVE | Giờ_GMT7</code>\n"
        "  <i>(VD: /add 4 Estrellas | https://www.rtve.es/play/videos/4-estrellas/ | 01:00)</i>\n"
        "• Đổi giờ bắt đầu quét (GMT+7):\n"
        "  <code>/time node_01 23:00</code> (hoặc <code>24/24</code>)\n"
        "• Bật/Tắt hoặc Xóa phim:\n"
        "  <code>/del node_02</code>\n"
        "• Ép quét toàn bộ ngay lập tức:\n"
        "  <code>/scan</code>"
    )
    return "\n".join(lines)


def process_remote_commands(token: str, authorized_channel: str) -> bool:
    """
    Polls Telegram getUpdates for commands from the authorized user/channel.
    Returns True if `/scan` was requested so the caller can force an immediate scan.
    """
    if not token or not authorized_channel:
        return False

    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))
    offset = last_update_id + 1 if last_update_id > 0 else 0

    api_url = f"{_EP_UPDATES.format(token)}?offset={offset}&timeout=1"
    try:
        req = urllib.request.Request(api_url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[WARN] Command check skipped: {exc}")
        return False

    if not data.get("ok"):
        return False

    updates = data.get("result", [])
    if not updates:
        return False

    force_scan_requested = False

    for upd in updates:
        uid = int(upd.get("update_id", 0))
        if uid > last_update_id:
            last_update_id = uid

        msg = upd.get("message") or upd.get("channel_post") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        # Security check: Only respond to the owner's authorized chat_id
        if chat_id != str(authorized_channel).strip() or not text.startswith("/"):
            continue

        cmd_parts = text.split(maxsplit=1)
        cmd = cmd_parts[0].lower().split("@")[0]
        arg_str = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""

        try:
            if cmd in ("/list", "/help", "/start"):
                send_raw_message(token, authorized_channel, format_list_message(), disable_preview=True)

            elif cmd == "/add":
                parts = [p.strip() for p in arg_str.split("|")]
                if len(parts) < 3:
                    send_raw_message(
                        token,
                        authorized_channel,
                        "⚠️ <b>Sai cú pháp!</b> Hãy gửi theo mẫu:\n"
                        "<code>/add Tên Phim | Link_RTVE | Giờ_Bắt_Đầu_GMT7</code>\n\n"
                        "Ví dụ:\n"
                        "<code>/add 4 Estrellas | https://www.rtve.es/play/videos/4-estrellas/ | 01:00</code>",
                        disable_preview=True,
                    )
                else:
                    name, url, start_gmt7 = parts[0], parts[1], parts[2]
                    win_h = float(parts[3]) if len(parts) >= 4 else 4.0
                    nid, pid = add_or_update_node(name, url, start_gmt7, window_hours=win_h)
                    send_raw_message(
                        token,
                        authorized_channel,
                        f"✅ <b>Đã lưu phim thành công!</b>\n\n"
                        f"• Mã Node: <code>{nid}</code> (pid: <code>{pid}</code>)\n"
                        f"• Tên phim: <b>{html.escape(name)}</b>\n"
                        f"• Giờ bắt đầu quét (GMT+7): <b>{html.escape(start_gmt7)}</b> (Quét trong {win_h}h)\n"
                        f"• Dữ liệu trong <code>tasks.json</code> đã được mã hóa ẩn danh Base64.\n"
                        f"• Đang tiến hành quét kiểm tra tập mới nhất ngay bây giờ...",
                        disable_preview=True,
                    )
                    force_scan_requested = True

            elif cmd == "/time":
                t_parts = arg_str.split()
                if len(t_parts) < 2:
                    send_raw_message(
                        token,
                        authorized_channel,
                        "⚠️ <b>Cú pháp đổi giờ:</b> <code>/time node_01 23:00</code> (hoặc <code>/time node_01 24/24</code>)",
                        disable_preview=True,
                    )
                else:
                    target_id, new_time = t_parts[0].strip(), t_parts[1].strip()
                    cfg = load_tasks_config()
                    found = False
                    for n in cfg.get("nodes", []):
                        if n.get("id") == target_id or _dec(n.get("label_b64", "")).lower() == target_id.lower():
                            n["start_gmt7"] = new_time
                            if len(t_parts) >= 3:
                                n["win_h"] = float(t_parts[2])
                            found = True
                            label = _dec(n.get("label_b64", ""))
                            save_tasks_config(cfg)
                            send_raw_message(
                                token,
                                authorized_channel,
                                f"✅ Đã đổi giờ bắt đầu quét của <b>{html.escape(label)}</b> (<code>{n['id']}</code>) sang <b>{html.escape(new_time)} (GMT+7)</b>.",
                                disable_preview=True,
                            )
                            break
                    if not found:
                        send_raw_message(
                            token,
                            authorized_channel,
                            f"❌ Không tìm thấy phim có mã <code>{html.escape(target_id)}</code>. Gõ <code>/list</code> để xem mã.",
                            disable_preview=True,
                        )

            elif cmd == "/del":
                target_id = arg_str.strip()
                cfg = load_tasks_config()
                nodes = cfg.get("nodes", [])
                new_nodes = [
                    n for n in nodes
                    if n.get("id") != target_id and _dec(n.get("label_b64", "")).lower() != target_id.lower()
                ]
                if len(new_nodes) < len(nodes):
                    cfg["nodes"] = new_nodes
                    save_tasks_config(cfg)
                    send_raw_message(
                        token,
                        authorized_channel,
                        f"🗑 Đã xóa <code>{html.escape(target_id)}</code> khỏi danh sách theo dõi.",
                        disable_preview=True,
                    )
                else:
                    send_raw_message(
                        token,
                        authorized_channel,
                        f"❌ Không tìm thấy <code>{html.escape(target_id)}</code>. Gõ <code>/list</code> để kiểm tra.",
                        disable_preview=True,
                    )

            elif cmd == "/scan":
                send_raw_message(
                    token,
                    authorized_channel,
                    "🔍 Đang ép quét toàn bộ danh sách phim ngay lập tức...",
                    disable_preview=True,
                )
                force_scan_requested = True

        except Exception as cmd_err:
            send_raw_message(
                token,
                authorized_channel,
                f"❌ Lỗi khi thực thi lệnh: <code>{html.escape(str(cmd_err))}</code>",
                disable_preview=True,
            )

    state = load_state()
    state["last_update_id"] = last_update_id
    save_state(state)
    return force_scan_requested


def run_sync(
    token: str,
    channel: str,
    force_scan: bool = False,
    force_notify_latest: bool = False,
    only_node_id: str | None = None,
) -> bool:
    # 1. Check if user sent any remote commands via Telegram (/add, /list, /time, /del, /scan)
    remote_force = process_remote_commands(token, channel)
    if remote_force:
        force_scan = True

    cfg = load_tasks_config()
    nodes = cfg.get("nodes", [])
    state = load_state()
    nodes_state = state.setdefault("nodes", {})
    current_dt = now_gmt7()
    print(f"[SYNC] Tick at {current_dt.strftime('%Y-%m-%d %H:%M:%S')} (GMT+7)")

    any_updated = False
    cfg_modified = False

    for node in nodes:
        if not node.get("enabled", True):
            continue
        nid = node.get("id", "node_00")
        if only_node_id and nid != only_node_id:
            continue

        label = _dec(node.get("label_b64", "")) or nid
        start_time = node.get("start_gmt7", "00:00")
        window_hours = float(node.get("win_h", 4))
        active_days = node.get("days")
        include_preview = bool(node.get("ext", True))

        in_window, reason = is_within_scan_window(
            current_dt, start_time, window_hours=window_hours, active_days=active_days
        )

        if not force_scan and not force_notify_latest and not in_window:
            print(f"[{nid}] Skip: {reason}")
            continue

        print(f"[{nid}] Polling ({reason if not force_scan else 'Force'})...")

        pid = str(node.get("pid", "")).strip()
        if not pid:
            raw_url = _dec(node.get("src_b64", ""))
            pid = resolve_pid(raw_url)
            node["pid"] = pid
            cfg_modified = True

        n_state = nodes_state.setdefault(
            nid,
            {
                "seen": [],
                "seq": 0,
                "ts": "",
            },
        )
        seen_ids = set(n_state.get("seen", []))
        is_first_run = len(seen_ids) == 0

        items = fetch_feed_items(pid, label, include_preview=include_preview)
        if not items:
            print(f"[{nid}] Warn: Empty response.")
            continue

        new_items = [x for x in items if x["id"] not in seen_ids]

        if is_first_run and not force_notify_latest:
            for x in items[:-1]:
                seen_ids.add(x["id"])
            new_items = [items[-1]]
        elif force_notify_latest:
            new_items = [items[-1]]

        node_updated = False
        for x in new_items:
            print(f"[{nid}] New item detected: id={x['id']} seq={x['episode']}")
            if token and channel:
                send_notification(token, channel, x)
                print(f"[{nid}] Dispatched alert for id={x['id']}.")
            else:
                print(f"[{nid}] Dry-run: {x['title']} -> {x['url']}")
            seen_ids.add(x["id"])
            if x.get("episode"):
                n_state["seq"] = x["episode"]
            n_state["ts"] = current_dt.strftime("%Y-%m-%d %H:%M:%S")
            node_updated = True
            any_updated = True

        if node_updated or is_first_run:
            n_state["seen"] = sorted(seen_ids)[-200:]
        else:
            latest = items[-1]
            print(f"[{nid}] Up-to-date (latest seq={latest['episode']}, id={latest['id']}).")

    if cfg_modified:
        save_tasks_config(cfg)
    save_state(state)
    return any_updated


def list_nodes() -> None:
    cfg = load_tasks_config()
    state = load_state().get("nodes", {})
    current_dt = now_gmt7()
    print(f"🕒 Giờ hiện tại (GMT+7): {current_dt.strftime('%Y-%m-%d %H:%M:%S')}\n")
    for idx, n in enumerate(cfg.get("nodes", []), 1):
        nid = n.get("id")
        label = _dec(n.get("label_b64", ""))
        url = _dec(n.get("src_b64", ""))
        st = state.get(nid, {})
        in_win, reason = is_within_scan_window(
            current_dt,
            n.get("start_gmt7", "00:00"),
            window_hours=float(n.get("win_h", 4)),
            active_days=n.get("days"),
        )
        icon = "🟢 ĐANG QUÉT" if in_win else "⚪ CHỜ TỚI GIỜ"
        print(
            f"{idx}. [{nid}] {label} (pid: {n.get('pid')})\n"
            f"   - Link gốc (đã mã hóa trong file): {url}\n"
            f"   - Giờ bắt đầu quét (GMT+7): {n.get('start_gmt7')} (Quét trong {n.get('win_h', 4)}h)\n"
            f"   - Trạng thái: {icon} — {reason}\n"
            f"   - Tập mới nhất đã ghi nhận: Ep {st.get('seq', 0)} (Cập nhật lúc: {st.get('ts', 'N/A')})\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Telemetry Sync Worker")
    parser.add_argument("--list", action="store_true", help="List configured nodes (decoded locally)")
    parser.add_argument(
        "--add",
        nargs=3,
        metavar=("NAME", "URL", "START_GMT7"),
        help='Add/update a show. E.g.: --add "La Promesa" "https://..." "23:30"',
    )
    parser.add_argument("--win-hours", type=float, default=4.0, help="Scan window hours (default: 4)")
    parser.add_argument("--force", action="store_true", help="Force poll regardless of GMT+7 schedule")
    parser.add_argument("--node", type=str, default=None, help="Poll specific node_id only")
    parser.add_argument("--test-notify", action="store_true", help="Force send latest item notification")
    parser.add_argument("--send-menu", action="store_true", help="Send the interactive control menu to Telegram")
    parser.add_argument("--loop", type=int, default=0, help="Run continuously every N seconds")
    args = parser.parse_args()

    token = (
        os.environ.get("SYNC_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    channel = (
        os.environ.get("SYNC_CHANNEL")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()

    if args.add:
        name, url, start_gmt7 = args.add
        nid, pid = add_or_update_node(name, url, start_gmt7, window_hours=args.win_hours)
        print(f"[OK] Saved {nid} (pid={pid}, start={start_gmt7} GMT+7).")
        if token and channel:
            send_raw_message(
                token,
                channel,
                f"✅ Đã thêm/cập nhật <b>{html.escape(name)}</b> (<code>{nid}</code>) — Bắt đầu quét lúc <b>{html.escape(start_gmt7)} GMT+7</b>.",
                disable_preview=True,
            )
        return

    if args.list:
        list_nodes()
        return

    if args.send_menu and token and channel:
        send_raw_message(token, channel, format_list_message(), disable_preview=True)
        print("[OK] Sent control menu to Telegram.")
        return

    if args.loop > 0:
        print(f"[*] Worker loop started (interval={args.loop}s)...")
        while True:
            try:
                run_sync(token=token, channel=channel, force_scan=args.force, only_node_id=args.node)
            except Exception as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        run_sync(
            token=token,
            channel=channel,
            force_scan=args.force,
            force_notify_latest=args.test_notify,
            only_node_id=args.node,
        )


if __name__ == "__main__":
    main()
