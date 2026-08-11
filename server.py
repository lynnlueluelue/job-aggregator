# -*- coding: utf-8 -*-
"""本地服务：提供网页 + 数据接口 + 3小时定时抓取 + 桌面通知"""
import datetime
import json
import os
import re
import threading
import time
import webbrowser

from flask import Flask, jsonify, send_from_directory, request

from crawler import (
    run as run_crawler,
    fetch as fetch_page,
    extract_text,
    extract_attachments,
    content_judge,
    classify_item,
    screen_item,
    fingerprint,
    to_absolute,
)
from config import PRIORITY_KEYWORDS, OTHER_KEYWORDS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "data.json")
TRACKED_FILE = os.path.join(BASE_DIR, "data", "tracked.json")
MARKS_FILE = os.path.join(BASE_DIR, "data", "marks.json")
CUSTOM_FILE = os.path.join(BASE_DIR, "data", "custom.json")
REFRESH_INTERVAL = 3 * 60 * 60  # 3 小时

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")


def notify(title, message):
    """Windows 桌面通知，失败时静默忽略"""
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name="招聘聚合",
            timeout=8,
        )
    except Exception:
        pass


def do_crawl(notify_wanted):
    fresh, items = run_crawler()
    if notify_wanted:
        hot = [it for it in fresh if it.get("tier") == 1]
        if hot:
            top = "\n".join(f"· {it['title'][:26]}" for it in hot[:3])
            more = f" 等 {len(hot)} 条" if len(hot) > 3 else ""
            notify("招聘聚合 · 第一梯队新公告", f"{top}{more}")
    return items


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/data")
def api_data():
    payload = {"updated": None, "channels": [], "items": []}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            payload = json.load(f)
    custom = load_custom()
    if custom:
        payload["items"] = list(payload.get("items", [])) + custom
        payload["channels"] = list(payload.get("channels", [])) + [
            {"id": "custom", "name": "自定义", "tier": 1}
        ]
    payload["marks"] = load_marks()
    return jsonify(payload)


@app.route("/api/keywords")
def api_keywords():
    return jsonify({"priority": PRIORITY_KEYWORDS, "other": OTHER_KEYWORDS})


def load_marks():
    """读取用户标记：{fp: "todo"|"done"|"hidden"}"""
    if os.path.exists(MARKS_FILE):
        try:
            with open(MARKS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_marks(marks):
    os.makedirs(os.path.dirname(MARKS_FILE), exist_ok=True)
    with open(MARKS_FILE, "w", encoding="utf-8") as f:
        json.dump(marks, f, ensure_ascii=False, indent=1)


@app.route("/api/marks", methods=["GET"])
def api_marks_get():
    return jsonify({"marks": load_marks()})


@app.route("/api/marks", methods=["POST"])
def api_marks_save():
    data = request.get_json(silent=True) or {}
    marks = load_marks()
    fp = data.get("fp") or ""
    action = data.get("action")
    if action == "set" and fp and data.get("mark") in ("todo", "done", "hidden"):
        marks[fp] = data["mark"]
    elif action == "clear" and fp:
        marks.pop(fp, None)
    save_marks(marks)
    return jsonify({"ok": True, "marks": marks})


def load_custom():
    """读取用户手动添加的招聘信息（复制链接添加）"""
    if os.path.exists(CUSTOM_FILE):
        try:
            with open(CUSTOM_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else data.get("items", [])
        except Exception:
            return []
    return []


def save_custom(items):
    os.makedirs(os.path.dirname(CUSTOM_FILE), exist_ok=True)
    with open(CUSTOM_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def guess_date_from_html(html, title):
    """从详情页 HTML 猜测发布日期（meta/正文中的日期），失败返回今天"""
    patterns = [
        r'date[^\d]{0,10}(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
        r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html[:8000]):
            try:
                d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if datetime.date(2015, 1, 1) <= d <= datetime.date.today() + datetime.timedelta(days=1):
                    return d.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return datetime.date.today().strftime("%Y-%m-%d")


@app.route("/api/custom", methods=["POST"])
def api_custom_add():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "请输入完整的 http(s) 链接"}), 400
    title_override = (data.get("title") or "").strip()
    try:
        html = fetch_page(url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"抓取失败: {e}"}), 400

    title = title_override
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        # 去掉站点后缀
        title = re.sub(r"[-_|_]\s*[^-_|]{2,30}$", "", title).strip()
    if not title or len(title) < 6:
        title = url.split("/")[-1].split(".")[0] or "自定义公告"

    date = guess_date_from_html(html, title)
    text = extract_text(html)
    attachments = extract_attachments(html)
    fp = "c-" + fingerprint(title, date)
    items = load_custom()
    if any(it.get("fp") == fp for it in items):
        return jsonify({"ok": False, "error": "该链接已在自定义列表中"}), 400

    kind, js, ns, ok, attach = content_judge(title, text, "custom", attachments)
    cls = classify_item(title, "custom")
    item = {
        "fp": fp,
        "title": title,
        "url": url,
        "date": date,
        "channel": "custom",
        "channel_name": "自定义",
        "tier": 1,
        "score": js,
        "keywords": [],
        "method": cls["method"],
        "is_result": cls["is_result"],
        "org": cls["org"],
        "region": cls["region"],
        "is_school_unit": cls["is_school_unit"],
        "is_university": cls["is_university"],
        "is_doctor": cls["is_doctor"],
        "blocked": False,
        "block_reason": "",
        "recruit": cls["recruit"],
        "seen": 0,
        "kind": "job",
        "job_score": js,
        "noise_score": ns,
        "attach_hit": attach[:8],
        "screen": screen_item(title, text, attachments),
        "classify_version": 999,
        "custom": True,
    }
    items.append(item)
    save_custom(items)
    return jsonify({"ok": True, "item": item})


@app.route("/api/custom", methods=["DELETE"])
def api_custom_del():
    data = request.get_json(silent=True) or {}
    fp = data.get("fp") or ""
    items = [it for it in load_custom() if it.get("fp") != fp]
    save_custom(items)
    return jsonify({"ok": True})


def load_tracked():
    """读取求职追踪列表（用户手动添加，用于精确显示对应聘用人员公示）"""
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else data.get("exams", [])
        except Exception:
            return []
    return []


def save_tracked(exams):
    os.makedirs(os.path.dirname(TRACKED_FILE), exist_ok=True)
    with open(TRACKED_FILE, "w", encoding="utf-8") as f:
        json.dump(exams, f, ensure_ascii=False, indent=1)


@app.route("/api/tracked")
def api_tracked():
    return jsonify({"exams": load_tracked()})


@app.route("/api/tracked", methods=["POST"])
def api_tracked_save():
    data = request.get_json(silent=True) or {}
    exams = load_tracked()
    name = (data.get("name") or "").strip()
    if data.get("action") == "add" and name and name not in exams:
        exams.append(name)
    elif data.get("action") == "remove":
        exams = [e for e in exams if e != name]
    save_tracked(exams)
    return jsonify({"ok": True, "exams": exams})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    def job():
        try:
            do_crawl(False)
        except Exception as e:
            print("刷新失败:", e)
    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True})


def schedule_loop():
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            do_crawl(True)
        except Exception as e:
            print("定时抓取失败:", e)


def main():
    print("启动招聘聚合服务: http://127.0.0.1:8765")
    do_crawl(False)
    threading.Thread(target=schedule_loop, daemon=True).start()
    webbrowser.open("http://127.0.0.1:8765")
    app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
