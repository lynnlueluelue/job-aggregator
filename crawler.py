# -*- coding: utf-8 -*-
"""抓取、解析、去重、关键词打分，结果写入 data/data.json"""
import json
import os
import re
import sys
import time
import hashlib
import tempfile
import zipfile
import datetime
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

from config import (
    CHANNELS, PRIORITY_KEYWORDS, OTHER_KEYWORDS,
    EXCLUDE_KEYWORDS, MAX_ITEMS_PER_CHANNEL, KEEP_DAYS, BOOKMARKS,
    SCHOOL_UNIT_KEYWORDS, UNIVERSITY_KEYWORDS, DOCTOR_KEYWORDS,    METHOD_EXAM, METHOD_DIRECT, METHOD_RESULT,
    ORG_RULES, REGION_RULES, CHANNEL_DEFAULT_REGION,
    FIT_KEYWORDS, UNFIT_MAJOR_KEYWORDS,
    RECRUIT_SCHOOL_KEYWORDS, RECRUIT_SOCIAL_KEYWORDS,
    CONTENT_JOB_KEYWORDS, CONTENT_NOISE_KEYWORDS,
    CONTENT_JOB_THRESHOLD, CONTENT_NOISE_OVERWEIGHT,
    TITLE_JOB_STRONG, TITLE_NOISE_STRONG,
    LIST_MAX_PAGES,
    ATTACH_JOB_KEYWORDS, CLASSIFY_VERSION,
    PARSE_ATTACH_MAJOR, PARSE_ATTACH_MAX_FILES,
    USER_PROFILE, MAJOR_FIT_KEYWORDS, MAJOR_RELATED_KEYWORDS,
    IGUOPIN_KEYWORDS, IGUOPIN_NOISE, IGUOPIN_REGION_PREFIX, IGUOPIN_DEFAULT_REGION,
    NCSS_AREA_CODE, UNFIT_MAJOR_EXAMPLES,
)

# Windows 控制台默认 GBK 无法输出 ✓/✗ 等字符，统一改为 UTF-8，避免日志打印崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "data.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def norm_date(m):
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def log(msg):
    text = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


# ---------------- 报考条件逐条核对（screen）----------------
# 依据 config.USER_PROFILE 对每份公告核对报考条件，产出"能否投递"综合判定：
#   年龄（按用户学历档口径，上限低于用户年龄 → 排除）
#   党员（需党员且用户非党员 → 排除；党员优先保留）
#   专业（对口/相关/未解析/未标注 保留；明确不符 → 排除）
#   招聘对象（用户应届+有工作经历均可投，仅作标签与排序，不作排除）
# 岗位表在附件且解析不出专业时 → "未解析"保留标注，不误杀。
_SCREEN_JIEYE = {
    "含择业期": [r"择业期", r"择业期内", r"(?:毕业|离校)[^。；]{0,3}两?年内",
                 r"未落实工作单位", r"离校未就业", r"暂未就业"],
    "社招": [r"面向社会", r"社会人员", r"社会在职", r"在职人员", r"不限应届"],
    "仅应届": [r"应届", r"202[4-8]届", r"当届", r"应往届毕业生"],
}
_SCREEN_AGE_NO = [r"年龄不限", r"不限年龄", r"无年龄限制", r"无年龄要求"]
# 年龄上限："30周岁以下" / "30周岁及以下" / "30周岁（含）以下" / "不超过30周岁" / "35岁以下" / "放宽至32周岁"
_SCREEN_AGE_CAP = re.compile(
    r"(?:(?:不超过|不大于|不高于)(\d{1,2})周岁)|"
    r"(?:放宽(?:至|到))(\d{1,2})周岁|"
    r"(\d{1,2})周岁[（(]?含?[)）]?(?:以下|及以下|以内)|"
    r"(\d{1,2})岁(?:以下|及以下)")
# 出生日期上限："1998年1月1日以后出生"（近似年龄上限 = 今年 - 出生年）
_SCREEN_BIRTH_CAP = re.compile(r"(\d{4})年\d{1,2}月\d{1,2}日[以之]?后出生")
_SCREEN_PARTY_REQ = [
    r"中共正式党员", r"正式党员", r"预备党员", r"党员身份", r"政治面貌",
    r"必须.{0,4}党员", r"须.{0,4}党员", r"限.{0,4}党员",
    r"(?:报考|应聘|招录|录用|招聘)(?:人员|对象|者)?[^。；，]{0,8}(?:须|要求|应|要)[^。；，]{0,4}党员",
    r"(?:是|为|属于)中共(?:正式|预备)?党员", r"中共党员[（(]含预备党员[)）]",
]
_SCREEN_PARTY_PRIOR = [r"党员优先", r"中共党员优先", r"优先.{0,2}中共党员"]
# 明确专业限制的语境词（配合下一条才判"专业不符"，避免正文误伤）
_SCREEN_MAJOR_CTX = [
    r"专业要求", r"招聘专业", r"报考专业", r"限.{0,8}专业", r"仅限.{0,6}专业",
    r"专业范围", r"专业为", r"以下专业", r"专业条件", r"专业[：:]",
    r"所学专业", r"专业方向", r"岗位专业", r"专业包括", r"专业含",
    r"专业毕业生", r"专业应届", r"专业技术人员", r"专业人才",
    r"(?:招聘|招录|招考|报考|选聘|选调|引进)[^。；\n]{0,14}(?:专业|岗位)",
]
# 非用户专业类词（"明确不符"证据）：由 config.UNFIT_MAJOR_EXAMPLES 提供


def _age_tier(win):
    """判断年龄线前的学历档（本科/硕士/博士/通用）。win 为数字前 15 字。"""
    if "博士" in win:
        return "博士"
    if any(w in win for w in ("硕士", "研究生", "硕")):
        return "硕士"
    if any(w in win for w in ("本科", "学士")):
        return "本科"
    return "通用"


def _extract_age_caps(blk, birth_year):
    """提取所有年龄线 (cap, tier)；出生日期线归为通用档。"""
    caps = []
    for m in _SCREEN_AGE_CAP.finditer(blk):
        n = int(m.group(1) or m.group(2) or m.group(3) or m.group(4))
        if 16 <= n <= 70:
            caps.append((n, _age_tier(blk[max(0, m.start() - 15):m.start()])))
    if birth_year:
        caps.append((datetime.date.today().year - birth_year, "通用"))
    return caps


def _resolve_age_cap(caps, degree=None):
    """按用户学历取年龄上限：用户学历档优先，其次通用，最后低一档。
    degree 取值为 博士/硕士/本科/专科（来自 config.USER_PROFILE）。"""
    if not caps:
        return None
    tier = degree if degree in ("博士", "硕士", "本科", "专科") else "硕士"
    tiers = {}
    for c, t in caps:
        tiers.setdefault(t, []).append(c)
    if tiers.get(tier):
        return min(tiers[tier])
    if tiers.get("通用"):
        return min(tiers["通用"])
    fallback = {"博士": ["硕士", "本科", "专科"],
                "硕士": ["本科", "专科"],
                "本科": ["专科"],
                "专科": []}.get(tier, [])
    for t in fallback:
        if tiers.get(t):
            return min(tiers[t])
    return None


def screen_major_state(blk, has_zhaobiao=False):
    """专业判定四态：对口 / 相关 / 不符 / 未标注（有岗位表附件且未命中 → 未解析由调用方处理）。
    返回 (state, hits)。"""
    if re.search(r"专业不限|不限专业", blk):
        return "对口", ["专业不限"]
    hits = [k for k in MAJOR_FIT_KEYWORDS if k in blk]
    if hits:
        return "对口", hits[:6]
    hits = [k for k in MAJOR_RELATED_KEYWORDS if k in blk]
    if hits:
        return "相关", hits[:6]
    if not has_zhaobiao and any(re.search(p, blk) for p in _SCREEN_MAJOR_CTX) \
            and any(k in blk for k in UNFIT_MAJOR_EXAMPLES):
        return "不符", []
    return "未标注", []


def screen_item(title, text, attachments=None):
    """逐条核对报考条件，返回 screen dict：含四个标签 + 可投性 ok/reasons。"""
    blk = " ".join([title, text or "", " ".join(attachments or [])])[:30000]
    screen = {"jieye": "未标注", "age": "未标注", "age_cap": None, "age_ok": True,
              "party": "未标注", "party_ok": True,
              "major": "未标注", "major_hit": [], "major_ok": True,
              "ok": True, "reasons": []}
    # ① 招聘对象（仅标签与排序，不作排除）
    if any(re.search(p, blk) for p in _SCREEN_JIEYE["含择业期"]):
        screen["jieye"] = "含择业期"
    elif any(re.search(p, blk) for p in _SCREEN_JIEYE["社招"]):
        screen["jieye"] = "社招"
    elif any(re.search(p, blk) for p in _SCREEN_JIEYE["仅应届"]):
        screen["jieye"] = "仅应届"
    # ② 年龄（硕士档口径 + 出生日期线，上限低于用户年龄 → 不可投）
    if any(re.search(p, blk) for p in _SCREEN_AGE_NO):
        screen["age"] = "不限"
    else:
        birth_years = [int(m.group(1)) for m in _SCREEN_BIRTH_CAP.finditer(blk)]
        cap = _resolve_age_cap(_extract_age_caps(blk, max(birth_years) if birth_years else 0),
                               USER_PROFILE.get("degree"))
        if cap:
            screen["age"] = "≤%d" % cap
            screen["age_cap"] = cap
            screen["age_ok"] = USER_PROFILE["age"] <= cap
    # ③ 党员（需党员且用户非党员 → 不可投；党员优先保留）
    has_prior = any(re.search(p, blk) for p in _SCREEN_PARTY_PRIOR)
    has_req = any(re.search(p, blk) for p in _SCREEN_PARTY_REQ)
    if not has_req and not has_prior and re.search(r"中共党员|党员", blk) \
            and not re.search(r"优秀共产党员|党员先锋|主题党日|党员教育|党员同志", blk):
        has_req = True
    if has_req:
        screen["party"] = "需党员"
        screen["party_ok"] = USER_PROFILE["party"] == "党员"
    elif has_prior:
        screen["party"] = "党员优先"
    # ④ 专业（对口/相关/未解析/未标注 保留；明确不符 → 不可投）
    has_zb = any(any(k in n for k in ("岗位", "职位", "专业", "计划表", "招聘岗位"))
                 for n in (attachments or []))
    state, hits = screen_major_state(blk, has_zhaobiao=has_zb)
    if state == "未标注" and has_zb:
        state = "未解析"
    if state in ("对口", "相关"):
        screen["major"] = state
        screen["major_hit"] = hits
    elif state == "未解析":
        screen["major"] = "未解析"
    elif state == "不符":
        screen["major"] = "不符"
        screen["major_ok"] = False
    # 综合可投性
    if screen["age_ok"] is False:
        screen["ok"] = False
        screen["reasons"].append("年龄卡%d" % screen["age_cap"])
    if screen["party_ok"] is False:
        screen["ok"] = False
        screen["reasons"].append("需党员")
    if screen["major_ok"] is False:
        screen["ok"] = False
        screen["reasons"].append("专业不符")
    return screen


# 高校人才网：JS 种 cookie 型 WAF，Session 带 cookie 重访可通过；全局会话避免重复握手
GAOXIAOJOB_SESSION = requests.Session()
GAOXIAOJOB_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.gaoxiaojob.com/",
})
GAOXIAOJOB_COOKIED = False


def _gaoxiaojob_get(url, timeout=25):
    """高校人才网专用请求：首次 403 种 cookie，重访返回真实内容；限速防 503"""
    global GAOXIAOJOB_COOKIED
    r = GAOXIAOJOB_SESSION.get(url, timeout=timeout, verify=False)
    if r.status_code == 403 and not GAOXIAOJOB_COOKIED:
        GAOXIAOJOB_COOKIED = True
        time.sleep(0.8)
        r = GAOXIAOJOB_SESSION.get(url, timeout=timeout, verify=False)
    if r.status_code == 503:
        time.sleep(2)
        r = GAOXIAOJOB_SESSION.get(url, timeout=timeout, verify=False)
    r.raise_for_status()
    return r


def _gaoxiaojob_joblist(detail_url):
    """高校人才网职位列表页：/announcement/job-list?id={id}。
    每行职位含 职位名(内嵌学历) + 需求专业列，比正文文本更精准。
    返回 (degrees_txt, majors_list, ok)，抓取失败 ok=False。
    degrees_txt: 所有职位学历标签拼接；majors_list: 所有需求专业去重。"""
    m = re.search(r"/announcement/detail/(\d+)\.html", detail_url)
    if not m:
        return "", [], False
    url = "https://www.gaoxiaojob.com/announcement/job-list?id=%s" % m.group(1)
    try:
        html = _gaoxiaojob_get(url).text
    except Exception:
        return "", [], False
    degrees, majors, seen = [], [], set()
    for row in re.findall(r'<tr data-href="[^"]*">(.*?)</tr>', html, re.S):
        name_td = re.search(r'class="data-name[^"]*">(.*?)</td>', row, re.S)
        major_td = re.search(r'class="data-major[^"]*">(.*?)</td>', row, re.S)
        if name_td:
            tds = name_td.group(1)
            for s in re.findall(r'<span>(.*?)</span>', tds, re.S):
                s = re.sub(r"<[^>]+>", "", s).strip()
                if s and re.search(r"硕士|博士|本科|专科|研究生|学士", s):
                    degrees.append(s)
        if major_td:
            raw = re.sub(r"<[^>]+>", " ", major_td.group(1))
            for s in re.split(r"[,，、;；\s]+", raw):
                s = s.strip()
                if s and s not in seen:
                    seen.add(s)
                    majors.append(s)
    return " ".join(degrees), majors, True


def _degree_ok_for_user(degrees_txt):
    """职位列表中的学历是否对用户可投（用户学历 ≥ 岗位要求学历）。
    USER_PROFILE.degree 取 博士/硕士/本科/专科。
    岗位学历：博士>硕士>本科>专科；岗位学历不高于用户学历 → 可投。"""
    degree = USER_PROFILE.get("degree", "硕士")
    order = {"博士": 4, "硕士": 3, "本科": 2, "专科": 1, "大专": 1}
    user_lv = order.get(degree, 3)
    for label, lv in (("博士", 4), ("硕士", 3), ("本科", 2), ("专科", 1), ("大专", 1)):
        if lv <= user_lv and re.search(label, degrees_txt):
            return True
    return False


def _apply_gxj_joblist_screen(it, degrees_txt, majors):
    """用高校人才网职位列表的结构化数据覆盖 ②专业/③学历 判定。
    学历：岗位含用户学历可投档→保留（即使标题含博士）；岗位全部高于用户学历→博士岗排除。
    专业：命中对口词→对口；命中相关词→相关（推荐度由前端色标区分）；
          无对口/相关且需求专业以理工科为主→专业不符（排除）。"""
    sc = it["screen"]
    # ③ 学历：标题含"博士"但职位列表有用户学历可投档 → 解除博士岗排除
    if it.get("is_doctor") and _degree_ok_for_user(degrees_txt) and it.get("block_reason") == "博士岗":
        it["is_doctor"] = False
        it["blocked"] = False
        it["block_reason"] = ""
    # 全部职位学历均高于用户学历 → 保留博士岗排除
    if degrees_txt and not _degree_ok_for_user(degrees_txt):
        it["is_doctor"] = True
        it["blocked"] = True
        it["block_reason"] = "博士岗"
    if not majors:
        return
    blk = " ".join(majors)
    state, hits = screen_major_state(blk, has_zhaobiao=True)
    if state in ("对口", "相关"):
        sc["major"] = state
        sc["major_hit"] = hits
        sc["major_ok"] = True
        # 结构化需求专业确认专业可投 → 清除正文层"专业不符"误判（需党员/年龄为独立条件，不覆盖）
        if "专业不符" in sc.get("reasons") or []:
            sc["reasons"] = [r for r in sc.get("reasons") if r != "专业不符"]
        if not sc["age_ok"] or sc.get("party_ok") is False:
            sc["ok"] = False
        else:
            sc["ok"] = True
        # 解除标题层"专业不符"误杀（若仍被排除）
        if it.get("block_reason") == "专业不符":
            it["blocked"] = False
            it["block_reason"] = ""
        return
    # 无对口/相关：需求专业以理工科为主 → 专业不符
    unfit_hits = [k for k in UNFIT_MAJOR_KEYWORDS if k in blk]
    if len(unfit_hits) >= 2 and len(unfit_hits) >= len(majors) * 0.6:
        sc["major"] = "不符"
        sc["major_hit"] = unfit_hits[:6]
        sc["major_ok"] = False
        sc["ok"] = False
        if "专业不符" not in sc["reasons"]:
            sc["reasons"].append("专业不符")


# 本地宝：高频会触发拼图验证/503，走 sz 站桌面端点；全局会话 + 限速
BENDIBAO_SESSION = requests.Session()
BENDIBAO_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://sz.bendibao.com/",
})
_LAST_BDB = [0.0]  # 限速：相邻请求至少间隔 3 秒


def _bendibao_get(url, timeout=40):
    """本地宝专用请求：限速 + 失败重试（该站间歇性连接超时/拼图验证）"""
    last_exc = None
    for attempt in range(3):
        wait = 3.0 - (time.time() - _LAST_BDB[0])
        if wait > 0:
            time.sleep(wait)
        try:
            r = BENDIBAO_SESSION.get(url, timeout=timeout, verify=False)
            _LAST_BDB[0] = time.time()
            if r.status_code in (503, 429) or "拼图验证" in r.text[:500]:
                time.sleep(6)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            last_exc = e
            _LAST_BDB[0] = time.time()
            time.sleep(6)
    raise last_exc if last_exc else RuntimeError("本地宝连续请求失败（限流/超时）")


def fetch(url):
    requests.packages.urllib3.disable_warnings()
    if "gaoxiaojob.com" in url:
        r = _gaoxiaojob_get(url)
        body = r.content
        ch = "utf-8"
    elif "bendibao.com" in url:
        r = _bendibao_get(url)
        body = r.content
        ch = "utf-8"
    else:
        r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
        r.raise_for_status()
        body = r.content
        m = re.search(rb'charset=["\']?([\w-]+)', body[:4000], re.I)
        ch = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return body.decode(ch, "replace")
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", "replace")


CHANNEL_BASE = {
    "sz_hrss": "http://hrss.sz.gov.cn/gzryzk/",
    "sz_szksy": "http://hrss.sz.gov.cn/szksy/",
    "sz_gzw": "http://gzw.sz.gov.cn/gzrc/",
    "gd_sydwzp": "https://hrss.gd.gov.cn/zwgk/sydwzp/",
    "gd_zz": "https://www.gdzz.gov.cn/gwygz/lypytzgg/",
    "gd_rsks": "https://rsks.gd.gov.cn/",
    "gz_rsj": "https://rsj.gz.gov.cn/zwdt/tzgg",
    "dg_hrss": "http://dghrss.dg.gov.cn/xwzx/gsgg/gkzp/",
    "gxj_gd": "https://www.gaoxiaojob.com/",
    "gxj_sz": "https://www.gaoxiaojob.com/",
    "bdb_sydw": "https://sz.bendibao.com/",
    "bdb_gq": "https://sz.bendibao.com/",
}


# 本地宝列表噪音：兼职/小时工/FAQ类资讯等与用户背景无关
BDB_NOISE_KEYWORDS = [
    "兼职", "小时工", "临时工", "暑期工", "暑假工", "寒假工", "店员",
    "理货", "分拣", "洗碗", "服务员", "收银", "保洁", "保安", "厨师",
    "帮工", "仓管", "搬运", "小时", "钟点工", "外卖", "快递", "配送",
    "辅警招聘考试", "辅警报名", "辅警培训", "常见问答", "报考指南",
    "最新消息（更新中）", "最新消息(更新中)", "报名费", "学费", "补贴", "考试大纲",
    "生活老师招聘最新消息",
]

# 本地宝 FAQ/问答型资讯标题特征：非真实岗位公告（"有编制吗/报名入口/工资待遇"等）
# 真实公告形如 "XX单位招聘X人公告"；问答型形如 "XX招聘报名入口/有编制吗/待遇"
BDB_FAQ_PATTERNS = [
    "有编制吗", "是多少", "是劳务派遣吗", "有哪些岗位", "有哪些",
    "怎么报名", "报名入口", "报名条件", "报名材料", "报名时间", "报名官网",
    "报名流程", "报名费", "报名方式", "报考条件", "报考流程",
    "招聘条件", "招聘要求", "招聘流程", "招聘岗位表", "招聘学历",
    "招聘工资", "薪资待遇", "工资和福利", "福利待遇", "待遇如何",
    "岗位表", "学历要求", "要求条件", "招录流程", "招录条件",
    "招录时间", "体测", "体能测试", "考试规则", "考场规则", "笔试规则",
    "什么时候", "如何", "吗$", "多少", "最新$", "官网",
]


def _is_bdb_faq(title):
    return any(re.search(p, title) for p in BDB_FAQ_PATTERNS)


def parse_bendibao_items(html):
    """本地宝列表：提取公告链接 + 日期。
    sz 站形如 <li><a href="/job/20268x/{id}.shtm">标题</a>...日期</li>
    m 站形如 <a href="/show{id}.html"><dl><dt>标题</dt><dd>日期</dd></dl></a>
    返回 [{title,url,date}]。
    """
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = re.search(r"/(?:job/\d+/\d+|show\d+)\.(?:shtm|html)$", href)
        if not m:
            continue
        dt = a.find("dt")
        title = ""
        if dt:
            title = dt.get_text(" ", strip=True)
        else:
            title = a.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) < 8:
            continue
        if any(kw in title for kw in EXCLUDE_KEYWORDS + BDB_NOISE_KEYWORDS):
            continue
        if _is_bdb_faq(title):
            continue
        # 日期：优先 a 内 dd，否则父容器（li）文本
        md = DATE_RE.search(dt.get_text() if dt else "")
        if not md:
            parent = a.find_parent(["li", "tr", "div"])
            md = DATE_RE.search(parent.get_text(" ", strip=True)) if parent else None
        date = norm_date(md) if md else None
        if href in seen:
            continue
        seen.add(href)
        items.append({"title": title, "url": href, "date": date})
    return items


def parse_gaoxiaojob_items(html):
    """高校人才网栏目页：提取 /announcement/detail/{id}.html 招聘公告链接。
    栏目页不直接带日期，日期留待详情页解析（详情抓取阶段由 guess 逻辑补）。
    返回 [{title,url,date:None}]。
    """
    soup = BeautifulSoup(html, "lxml")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = re.search(r"/announcement/detail/(\d+)\.html$", href)
        if not m:
            continue
        title = (a.get("title") or "").strip() or a.get_text(" ", strip=True).strip()
        title = re.sub(r"\s+", " ", title)
        if not title or len(title) < 8:
            continue
        if any(ex in title for ex in EXCLUDE_KEYWORDS):
            continue
        if href in seen:
            continue
        seen.add(href)
        items.append({
            "title": title,
            "url": urljoin("https://www.gaoxiaojob.com", href),
            "date": None,  # 详情页解析
            "_gxj": True,
        })
    return items


# ---------------- 国企/央企校招 API 渠道 ----------------
# 国聘（gp-api.iguopin.com）匿名 API；目标岗位关键词与地区前缀在 config.py 中配置
def _is_local_region(district_list):
    """国聘 district_list 中省份编码以配置的地区前缀开头（如广东 440）才算本地岗位。"""
    return any(str(d.get("province", "")).startswith(IGUOPIN_REGION_PREFIX)
               for d in (district_list or []))


def _gp_post(path, payload, retries=2):
    """国聘 API POST（带限流退避）。"""
    url = "https://gp-api.iguopin.com" + path
    for i in range(retries + 1):
        try:
            r = requests.post(url, json=payload, headers=HEADERS, timeout=25, verify=False)
            if r.status_code == 429:
                time.sleep(3 + i * 3)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == retries:
                raise
            time.sleep(2 * (i + 1))
    return {}


def _gp_get(path, retries=2):
    """国聘 API GET（带限流退避）。"""
    url = "https://gp-api.iguopin.com" + path
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
            if r.status_code == 429:
                time.sleep(3 + i * 3)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == retries:
                raise
            time.sleep(2 * (i + 1))
    return {}



def parse_iguopin_items():
    """国聘·央企校招：按目标岗位关键词搜索，仅保留本地（config.IGUOPIN_REGION_PREFIX）职位。
    返回 [{title,url,date:None,_region,_gp_id}]。"""
    out, seen = [], set()
    for kw in IGUOPIN_KEYWORDS:
        try:
            data = _gp_post("/api/jobs/v1/list", {"keyword": kw, "page": 1, "page_size": 20})
            for it in (data.get("data") or {}).get("list") or []:
                job_id = str(it.get("job_id") or "")
                if not job_id or job_id in seen:
                    continue
                if not _is_local_region(it.get("district_list")):
                    continue
                seen.add(job_id)
                title = re.sub(r"\s+", " ", it.get("job_name") or "").strip()
                if not title or len(title) < 4:
                    continue
                if any(ex in title for ex in EXCLUDE_KEYWORDS):
                    continue
                if any(nz in title for nz in IGUOPIN_NOISE):
                    continue
                region = ""
                for d in (it.get("district_list") or []):
                    acn = d.get("area_cn") or ""
                    if "-" in acn:
                        region = acn.split("-")[0]
                        break
                out.append({
                    "title": title,
                    "url": f"https://www.iguopin.com/job/detail?id={job_id}",
                    "date": None,
                    "_region": region or IGUOPIN_DEFAULT_REGION,
                    "_gp_id": job_id,
                    "_gp_company": (it.get("company_name") or "").strip(),
                    "_gp_recruit": "校招" if it.get("recruitment_type_cn") == "校园招聘" else "社招",
                })
        except Exception:
            continue
        time.sleep(0.6)
    return out


def _ncss_degree_ok(degree):
    """24365 岗位学历是否对用户可投（用户学历 ≥ 岗位要求）。
    专科/中专/高中岗位对本科及以上用户可投，博士仅限对非博士用户排除。"""
    user_deg = USER_PROFILE.get("degree", "硕士")
    if not degree:
        return True
    if user_deg == "博士":
        return True
    if "博士" in degree:
        return False
    return True


def parse_ncss_items():
    """24365 大学生就业服务平台：目标地区（config.NCSS_AREA_CODE）岗位，匿名 API。
    过滤掉实习/校园大使、学历高于用户的职位（博士岗对硕士用户不可投等）。
    返回 [{title,url,date,_ncss_degree}]。"""
    out, seen = [], set()
    for off in range(1, 101, 20):
        try:
            r = requests.get(
                "https://www.ncss.cn/student/jobs/jobslist/ajax/",
                params={"jobType": "03", "areaCode": NCSS_AREA_CODE, "offset": off, "limit": 20},
                headers=HEADERS, timeout=25, verify=False)
            r.raise_for_status()
            lst = (r.json().get("data") or {}).get("list") or []
        except Exception:
            break
        if not lst:
            break
        for it in lst:
            jid = str(it.get("jobId") or "")
            if not jid or jid in seen:
                continue
            title = re.sub(r"\s+", " ", it.get("jobName") or "").strip()
            degree = it.get("degreeName") or ""
            if not title or len(title) < 4:
                continue
            if "实习" in title or "校园大使" in title:
                continue
            if not _ncss_degree_ok(degree):
                continue
            seen.add(jid)
            date = None
            pd = str(it.get("publishDate") or "")
            md = DATE_RE.search(pd)
            if md:
                date = norm_date(md)
            out.append({
                "title": title,
                "url": f"https://www.ncss.cn/student/jobs/{jid}/detail.html",
                "date": date,
                "_ncss_degree": degree,
            })
        if len(lst) < 20:
            break
        time.sleep(0.4)
    return out


def to_absolute(url, channel_id):
    """相对链接补全为完整 URL"""
    if re.match(r"https?://", url):
        return url
    base = CHANNEL_BASE.get(channel_id)
    if not base:
        return url
    if url.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base)
        return (m.group(1) + url) if m else url
    return base.rstrip("/") + "/" + url.lstrip("/")


# 详情页正文容器候选 class 关键词
_CONTENT_HINTS = ["content", "article", "detail", "TRS_Editor", "con_text",
                  "xl_content", "newscontent", "zwxl", "mainContent",
                  "article-content", "content_shareicon", "view", "txt", "body"]


def extract_text(html):
    """提取详情页正文文本（去掉脚本/导航，取最可能的正文块）"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "iframe", "noscript"]):
        tag.decompose()
    candidates = []
    for c in soup.find_all(True):
        cl = " ".join(c.get("class", [])) + " " + (c.get("id") or "")
        cl = cl.lower()
        if not any(h in cl for h in _CONTENT_HINTS):
            continue
        t = c.get_text("\n")
        t = re.sub(r"\n{2,}", "\n", t).strip()
        if 300 < len(t) < 60000:
            candidates.append(t)
    if candidates:
        # 取最长候选作为正文（一般正文块最长）
        text = max(candidates, key=len)
    else:
        text = soup.get_text("\n")
    return re.sub(r"\s+", " ", text).strip()


# 附件链接常见文件扩展名
_ATTACH_EXTS = ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "wps",
                "et", "dps", "rar", "zip", "7z", "jpg", "png", "txt")


def extract_attachments(html):
    """提取详情页底部附件名称（链接文本/文件名）。

    常见 gov.cn 公告底部以附件形式挂招聘岗位表、职位表、报名表等，
    附件名含"招聘/岗位/专业参考目录"等词时，说明正文大概率是招聘信息。
    返回附件名列表。
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    names = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip().lower()
        ext = href.split("?")[0].rsplit(".", 1)[-1] if "." in href else ""
        if ext not in _ATTACH_EXTS:
            continue
        name = (a.get("title") or "").strip() or a.get_text(" ", strip=True)
        if not name or len(name) > 120:
            name = href.split("/")[-1].split("?")[0]
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            names.append(name)
    # 去重保序
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def extract_attachment_links(html):
    """提取附件 (名称, 原始href) 对，用于条件④下载岗位表解析专业。"""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        ext = href.split("?")[0].rsplit(".", 1)[-1].lower() if "." in href else ""
        if ext not in _ATTACH_EXTS:
            continue
        name = (a.get("title") or "").strip() or a.get_text(" ", strip=True)
        if not name or len(name) > 120:
            name = href.split("/")[-1].split("?")[0]
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            continue
        if (name, href) not in seen:
            seen.add((name, href))
            links.append((name, href))
    return links[:12]


def _extract_xlsx_text(path):
    """zip 解包 xlsx，拼接共享字符串与各工作表文本（免依赖）"""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            parts = []
            if "xl/sharedStrings.xml" in names:
                xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                parts += re.findall(r"<t[^>]*>(.*?)</t>", xml, re.S)
            for s in sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
                xml = z.read(s).decode("utf-8", "replace")
                parts += re.findall(r"<t[^>]*>(.*?)</t>", xml, re.S)
        return " ".join(p for p in parts if p and p.strip())
    except Exception:
        return ""


def _extract_docx_text(path):
    """zip 解包 docx 正文（免依赖）"""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", "", xml)
    except Exception:
        return ""


def _fetch_attachment_text(url):
    """下载附件并抽取文本（岗位表多为 xlsx/docx/pdf），供条件④解析专业。
    失败或文件过大返回 ""。"""
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    try:
        if "bendibao.com" in url:
            r = _bendibao_get(url, timeout=15)
        elif "gaoxiaojob.com" in url:
            r = _gaoxiaojob_get(url, timeout=15)
        else:
            r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
            r.raise_for_status()
        if r.status_code != 200 or not r.content or len(r.content) > 3 * 1024 * 1024:
            return ""
        raw = r.content
    except Exception:
        return ""
    path = os.path.join(tempfile.gettempdir(),
                        "att_%s.%s" % (hashlib.md5(url.encode("utf-8")).hexdigest()[:12], ext))
    try:
        with open(path, "wb") as f:
            f.write(raw)
        if ext in ("xlsx", "xlsm"):
            return _extract_xlsx_text(path)
        if ext == "docx":
            return _extract_docx_text(path)
        if ext == "pdf":
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                return " ".join((pg.extract_text() or "") for pg in pdf.pages)
    except Exception:
        pass
    return ""


def content_judge(title, text, channel_id, attachments=None):
    """基于标题 + 正文 + 附件判断该公告是否含真实招聘岗位。

    返回 (kind, job_score, noise_score, detail_ok, attach_hit)
    kind: job=真实岗位公告 / notice=政策新闻通知 / result=结果公示(由标题层决定)
    attach_hit: 命中的附件名列表（含招聘关键词），用于展示与复核
    """
    attachments = attachments or []
    attach_hit = [a for a in attachments if any(kw in a for kw in ATTACH_JOB_KEYWORDS)]
    # 标题强噪音：即使正文有招聘词也判为通知/宣传（附件命中时除外）
    if any(kw in title for kw in TITLE_NOISE_STRONG) and not attach_hit:
        return "notice", 0, 0, bool(text), []
    # 附件含招聘关键词：强招聘信号，直接判 job（防止漏项）
    if attach_hit:
        return "job", 2, 0, bool(text), attach_hit
    # 标题强招聘信号：直接判 job，正文抓取失败也保留
    if any(kw in title for kw in TITLE_JOB_STRONG):
        job = sum(1 for kw in CONTENT_JOB_KEYWORDS if text and kw in text)
        noise = sum(1 for kw in CONTENT_NOISE_KEYWORDS if text and kw in text)
        return "job", job, noise, bool(text), []
    # 详情不可用：标题兜底（含招聘动作词判 job，否则 notice）
    if not text:
        if any(kw in title for kw in ["招聘", "招录", "选聘", "选调", "招募"]):
            return "job", 0, 0, False, []
        return "notice", 0, 0, False, []
    job = sum(1 for kw in CONTENT_JOB_KEYWORDS if kw in text)
    noise = sum(1 for kw in CONTENT_NOISE_KEYWORDS if kw in text)
    score = job - noise * CONTENT_NOISE_OVERWEIGHT
    if job >= CONTENT_JOB_THRESHOLD and score >= 1:
        return "job", job, noise, True, []
    if job >= 1 and score >= 0:
        return "job", job, noise, True, []
    return "notice", job, noise, True, []


def parse_items(html, channel):
    """通用 li 解析：每个 li 内取标题链接 + 日期。无日期条目跳过。"""
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        # 只收公告详情链接（content/post_xxx.html 这类）
        if "content/post_" not in href and "post_" not in href:
            continue
        m = DATE_RE.search(li.get_text())
        if not m:
            continue
        date = norm_date(m)
        # 优先取专属标题元素，避免把日期拼进标题（如 gzw 的 span 结构）
        sp = a.find("span", class_=lambda c: c and ("title" in c or "text" in c))
        title = ""
        if sp:
            title = sp.get_text(strip=True)
        if not title:
            title = (a.get("title") or "").strip() or a.get_text(strip=True)
        # 清理 gd_rsks 等页面的前缀/后缀噪音
        title = re.sub(r"^标题[:：]\s*", "", title)
        title = re.sub(r"(发表时间|发布时间|发布日期).*$", "", title)
        title = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}$", "", title).strip()
        if not title or len(title) < 6:
            continue
        if any(ex in title for ex in EXCLUDE_KEYWORDS):
            continue
        if href in seen:
            continue
        seen.add(href)
        items.append({"title": title, "url": href, "date": date})
    return items


def score_item(title):
    keywords = []
    score = 0
    for kw in PRIORITY_KEYWORDS:
        if kw in title:
            keywords.append(kw)
            score += 3
    for kw in OTHER_KEYWORDS:
        if kw in title:
            keywords.append(kw)
            score += 1
    return score, keywords


def classify_item(title, channel_id):
    """给公告打标签：投递方式 / 单位类型 / 地区 / 中小学岗 / 博士岗 / 可投递性 / 校招社招"""
    # 按招聘单位判断中小学岗（高校教师/高校行政保留）；博士岗单独判断
    is_school_unit = any(kw in title for kw in SCHOOL_UNIT_KEYWORDS)
    if not is_school_unit:
        # "XX附属学校"通配：附属的"XX学校"仍是中小学（如"香港中文大学(深圳)附属礼文学校"）
        if re.search(r"附属.{0,8}学校", title):
            is_school_unit = True
    if not is_school_unit:
        # "XX学校"通配：含"学校"但不含大学/学院/高校等字样的单位视为中小学
        if "学校" in title and not any(k in title for k in UNIVERSITY_KEYWORDS):
            is_school_unit = True
    is_university = any(kw in title for kw in UNIVERSITY_KEYWORDS)
    if is_school_unit:
        is_university = False
    is_doctor = any(kw in title for kw in DOCTOR_KEYWORDS)

    # 投递方式：结果公示 > 考试公告 > 直接投递
    if any(kw in title for kw in METHOD_RESULT):
        method = "结果公示"
    elif any(kw in title for kw in METHOD_EXAM):
        method = "考试公告"
    else:
        method = "直接投递"

    # 是否聘用/招录结果公示（默认不显示，除非匹配考试追踪中的考试）
    is_result = method == "结果公示"

    # 单位类型（按规则顺序取首个命中；中小学岗优先标事业单位，高校岗标高校）
    org = "其他"
    if is_school_unit:
        org = "事业单位"
    else:
        for org_name, include, exclude in ORG_RULES:
            if any(kw in title for kw in include) and not any(kw in title for kw in exclude):
                org = org_name
                break
    if is_university and org in ("事业单位", "其他"):
        org = "高校"

    # 地区：标题关键词优先，未命中用渠道默认
    region = CHANNEL_DEFAULT_REGION.get(channel_id, "其他")
    for region_name, kws in REGION_RULES:
        if any(kw in title for kw in kws):
            region = region_name
            break

    # 可投递性：中小学岗（按单位）直接排除；博士岗排除；专业硬性不符排除
    # 注意：高校教师/高校行政岗位保留，仅排除小学/初中/高中/中学等中小学单位
    blocked = False
    block_reason = ""
    if is_school_unit:
        blocked, block_reason = True, "中小学岗"
    elif is_doctor:
        blocked, block_reason = True, "博士岗"
    else:
        has_fit = any(kw in title for kw in FIT_KEYWORDS)
        has_unfit = any(kw in title for kw in UNFIT_MAJOR_KEYWORDS)
        has_major_ctx = any(kw in title for kw in
                            ["专业", "类", "学科", "限", "岗位", "技术"])
        if has_unfit and not has_fit and has_major_ctx:
            blocked, block_reason = True, "专业不符"

    # 校招/社招标签
    is_school = any(kw in title for kw in RECRUIT_SCHOOL_KEYWORDS)
    is_social = any(kw in title for kw in RECRUIT_SOCIAL_KEYWORDS)
    if is_school and is_social:
        recruit = "校招/社招"
    elif is_school:
        recruit = "校招"
    elif is_social:
        recruit = "社招"
    else:
        recruit = "未标注"

    return {
        "method": method,
        "is_result": is_result,
        "org": org,
        "region": region,
        "is_school_unit": is_school_unit,
        "is_university": is_university,
        "is_doctor": is_doctor,
        "blocked": blocked,
        "block_reason": block_reason,
        "recruit": recruit,
    }


def fingerprint(title, date):
    key = re.sub(r"[\s\u3000【】\[\]（）()]|关于|公布|公告|通知", "", title) + "|" + (date or "")
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def norm_dedup_key(title):
    """跨渠道去重键：去掉年份/标点/通用后缀，保留单位、岗位与月份差异。
    用于把"深圳市XX医院2026年招聘公告"与"2026深圳市XX医院8月招聘公告"
    这类同源公告（不同渠道措辞不同）识别为同一条；但"8月"与"9月"招聘保持区分。"""
    t = re.sub(r"20\d{2}年?", "", title)
    t = re.sub(r"[\s\u3000【】\[\]（）()《》〈〉、，。！？·\-—…'\"\"：:]+", "", t)
    for suf in ["关于", "公布", "公告", "通知", "招聘简章", "招聘信息", "招聘启事",
                "诚聘英才", "诚聘", "急聘", "热招", "招募", "招聘", "启事", "简章"]:
        if t.endswith(suf):
            t = t[: -len(suf)]
    return t


def load_old():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", [])
    except Exception:
        return []


def save(items, channels_meta):
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channels": channels_meta,
        "bookmarks": BOOKMARKS,
        "items": items,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def guess_pub_date(html):
    """从详情页 HTML 猜测发布日期（meta/正文日期），失败返回 None"""
    patterns = [
        r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?',
        r'date[^\d]{0,10}(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html[:8000]):
            try:
                d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if datetime.date(2015, 1, 1) <= d <= datetime.date.today() + datetime.timedelta(days=1):
                    return d.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def fetch_detail(url, need_date=False):
    """抓取详情页正文 + 附件名 + 附件链接 + 发布日期。
    国聘详情是 SPA 壳，改走 info 接口取 JSON 拼正文。
    失败返回 (None, [], [], None)。
    need_date=True 时额外尝试解析发布日期（用于无日期列表的渠道）。
    """
    if "iguopin.com/job/detail" in url:
        m = re.search(r"[?&]id=(\d+)", url)
        if not m:
            return None, [], [], None
        try:
            j = _gp_get(f"/api/jobs/v1/info?id={m.group(1)}")
            d = (j.get("data") or {})
            if not d:
                return None, [], [], None
            contents = re.sub(r"<[^>]+>", " ", d.get("contents") or "")
            contents = re.sub(r"\s+", " ", contents).strip()
            kw = lambda v: f"学历:{v}" if v else ""
            parts = [
                d.get("job_name") or "", d.get("company_name") or "",
                d.get("recruitment_type_cn") or d.get("nature_cn") or "",
                kw(d.get("education_cn")),
                f"专业:{d.get('major_cn')}" if d.get("major_cn") else "",
                f"类别:{d.get('category_cn')}" if d.get("category_cn") else "",
                f"薪资:{d.get('min_wage')}-{d.get('max_wage')}{d.get('wage_unit_cn')}"
                if d.get("min_wage") else "",
                f"经验:{d.get('experience_cn')}" if d.get("experience_cn") else "",
                contents,
            ]
            text = " ".join(p for p in parts if p)
            return text, [], [], None
        except Exception:
            return None, [], [], None
    try:
        html = fetch(url)
        text = extract_text(html)
        atts = extract_attachments(html)
        att_links = extract_attachment_links(html)
        pub = guess_pub_date(html) if need_date else None
        return text, atts, att_links, pub
    except Exception:
        return None, [], [], None


def fetch_list(ch, max_pages=5):
    """抓取渠道列表页（含分页），返回去重后的条目列表"""
    if ch["id"] == "gp_iguopin":
        return parse_iguopin_items()
    if ch["id"] == "ncss_gd":
        return parse_ncss_items()
    if ch["id"] in ("gxj_gd", "gxj_sz"):
        html = fetch(ch["url"])
        return parse_gaoxiaojob_items(html)
    if ch["id"] in ("bdb_sydw", "bdb_gq"):
        html = fetch(ch["url"])
        return parse_bendibao_items(html)
    pages = [ch["url"]]
    base = ch["url"]
    for n in range(2, max_pages + 1):
        if "index.html" in base:
            pages.append(base.replace("index.html", f"index_{n}.html"))
        else:
            pages.append(base.rstrip("/") + f"/index_{n}.html")
    seen, out = set(), []
    for u in pages:
        try:
            html = fetch(u)
            for p in parse_items(html, ch):
                key = (p["title"], p["date"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(p)
        except Exception:
            break
    return out


def screen_major_from_attachments(new_items):
    """条件④兜底：正文未命中专业时，解析附件岗位表（xlsx/docx/pdf）补查。
    只处理 kind=job 且未排除、专业为 未标注/未解析 的条目，受 PARSE_ATTACH_MAX_FILES 上限约束。"""
    if not PARSE_ATTACH_MAJOR:
        return
    cands = [it for it in new_items
             if it.get("kind") == "job" and not it.get("blocked")
             and (it.get("screen") or {}).get("major") in ("未标注", "未解析")
             and it.get("_att_links")]
    done = got = 0
    for it in cands:
        if done >= PARSE_ATTACH_MAX_FILES:
            break
        base = it.get("_att_base") or to_absolute(it["url"], it["channel"])
        for name, url in it["_att_links"]:
            ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
            if ext not in ("xlsx", "xlsm", "docx", "pdf"):
                continue
            if not any(k in name for k in ("岗位", "职位", "专业", "招聘",
                                           "报名表", "附件", "需求", "计划表")):
                continue
            txt = _fetch_attachment_text(urljoin(base, url))
            if not txt:
                continue
            done += 1
            state, hits = screen_major_state(txt[:30000], has_zhaobiao=True)
            if state in ("对口", "相关"):
                it["screen"]["major"] = state
                it["screen"]["major_hit"] = hits
                it["screen"]["major_ok"] = True
                got += 1
            elif state == "不符":
                pass  # 有岗位表附件时不按附件判不符（可能含其他岗位）
            break  # 每条只解析第一个合适的附件
        if done >= PARSE_ATTACH_MAX_FILES:
            break
    if done:
        log(f"  附件岗位表解析：{done} 个文件，补命中专业 {got} 条")


def run(notify=False):
    log("开始抓取...")
    old = load_old()
    old_by_fp = {it["fp"]: it for it in old}
    new_items = []
    channels_meta = []
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=KEEP_DAYS)

    for ch in CHANNELS:
        try:
            parsed = fetch_list(ch, LIST_MAX_PAGES)
            count = 0
            for p in parsed:
                count += 1
                if count > MAX_ITEMS_PER_CHANNEL:
                    break
                score, kws = score_item(p["title"])
                fp = fingerprint(p["title"], p["date"])
                cls = classify_item(p["title"], ch["id"])
                if p.get("_region"):
                    cls["region"] = p["_region"]
                if p.get("_gp_recruit"):
                    cls["recruit"] = p["_gp_recruit"]
                item = {
                    "fp": fp,
                    "title": p["title"],
                    "url": p["url"],
                    "date": p["date"],
                    "channel": ch["id"],
                    "channel_name": ch["name"],
                    "tier": ch["tier"],
                    "score": score,
                    "keywords": kws,
                    "method": cls["method"],
                    "is_result": cls["is_result"],
                    "org": cls["org"],
                    "region": cls["region"],
                    "is_school_unit": cls["is_school_unit"],
                    "is_university": cls["is_university"],
                    "is_doctor": cls["is_doctor"],
                    "blocked": cls["blocked"],
                    "block_reason": cls["block_reason"],
                    "recruit": cls["recruit"],
                    "seen": old_by_fp[fp]["seen"] if fp in old_by_fp else 0,
                    "kind": None,
                    "job_score": None,
                    "noise_score": None,
                    "attach_hit": [],
                    "classify_version": CLASSIFY_VERSION,
                    "_ncss_degree": p.get("_ncss_degree"),
                }
                # 结果公示直接用 is_result 标记，跳过详情深筛
                if cls["is_result"]:
                    item["kind"] = "result"
                new_items.append(item)
            log(f"  ✓ {ch['name']}: {len(parsed)} 条")
            channels_meta.append({"id": ch["id"], "name": ch["name"], "tier": ch["tier"], "ok": True})
        except Exception as e:
            log(f"  ✗ {ch['name']}: {e}")
            # 渠道抓取失败时保留旧条目，避免不稳定渠道导致数据回退；
            # 只要有旧数据可用即视为"ok"（前端不依赖此字段，仅语义准确）
            kept = [oldit for oldit in old if oldit.get("channel") == ch["id"]]
            for oldit in kept:
                new_items.append(oldit)
            channels_meta.append({"id": ch["id"], "name": ch["name"], "tier": ch["tier"],
                                  "ok": bool(kept), "stale": True})

    # ---- 内容深筛：并发抓详情页，对非结果公示条目判定 kind ----
    need_detail = [it for it in new_items if it["kind"] is None]
    if need_detail:
        # 已有缓存的 kind 直接复用，避免重复请求（分类逻辑升级后自动重判）
        def classify_one(it):
            prev = old_by_fp.get(it["fp"])
            if prev and prev.get("kind") and prev.get("classify_version") == CLASSIFY_VERSION:
                it["kind"] = prev.get("kind")
                it["job_score"] = prev.get("job_score")
                it["noise_score"] = prev.get("noise_score")
                it["attach_hit"] = prev.get("attach_hit") or []
                it["screen"] = prev.get("screen") or {
                    "jieye": "未标注", "age": "未标注", "age_cap": None, "age_ok": True,
                    "party": "未标注", "party_ok": True,
                    "major": "未标注", "major_hit": [], "major_ok": True,
                    "ok": True, "reasons": []}
                it["date"] = it["date"] or prev.get("date")  # 复用已解析日期
                return it
            abs_url = to_absolute(it["url"], it["channel"])
            need_date = it["channel"] in ("gxj_gd", "gxj_sz", "bdb_sydw", "bdb_gq")
            text, attachments, att_links, pub = fetch_detail(abs_url, need_date=need_date)
            if need_date and pub and not it["date"]:
                it["date"] = pub
            if it["channel"] in ("gp_iguopin", "ncss_gd"):
                # 岗位级平台：抓到的就是岗位，直接判 job，不走内容评分；
                # 24365 详情页需登录，用标题+学历做内容与筛选，避免登录墙文本污染判定
                if it["channel"] == "ncss_gd":
                    text = it["title"] + " " + (it.get("_ncss_degree") or "")
                kind, js, ns, ok, attach = "job", None, None, True, []
            else:
                kind, js, ns, ok, attach = content_judge(it["title"], text, it["channel"], attachments)
            it["kind"] = kind
            it["job_score"] = js
            it["noise_score"] = ns
            it["attach_hit"] = attach[:8]
            it["screen"] = screen_item(it["title"], text, attachments)
            # 高校人才网：职位列表页有结构化 学历+需求专业，覆盖正文级判定（②③）
            # 仅在"可能有改善空间"时抓取职位列表，避免对 WAF 站高负载
            if it["channel"] in ("gxj_gd", "gxj_sz") and it["kind"] == "job":
                sc0 = it["screen"]
                if (it.get("blocked") and it.get("block_reason") in ("博士岗", "专业不符")
                        or sc0.get("major") in ("未标注", "未解析")
                        or (sc0.get("major") in ("对口", "相关") and it.get("block_reason") == "专业不符")):
                    gdeg, gmajors, gok = _gaoxiaojob_joblist(abs_url)
                    if gok:
                        _apply_gxj_joblist_screen(it, gdeg, gmajors)
            it["_att_base"] = abs_url
            it["_att_links"] = att_links
            return it

        with ThreadPoolExecutor(max_workers=6) as pool:
            for done in pool.map(classify_one, need_detail):
                pass

        # 条件④兜底：正文未命中专业时，尝试解析附件岗位表
        screen_major_from_attachments(new_items)
        for it in new_items:
            it.pop("_att_links", None)
            it.pop("_att_base", None)

        # 报考条件硬排除：kind=job 且 screen 判定不可投 → 并入 blocked（前端默认隐藏）
        # 原因同时含类型级原因时，保留原 block_reason（触发排除开关），详情级原因放 screen_reasons
        for it in new_items:
            if it["kind"] == "job" and it.get("screen") and it["screen"].get("ok") is False:
                reasons = it["screen"].get("reasons") or []
                if reasons:
                    it["blocked"] = True
                    if it.get("block_reason"):
                        it["screen_reasons"] = reasons
                    else:
                        it["block_reason"] = "、".join(reasons)

    # 按 (score desc, date desc) 排序后去重：优先保留高得分条目
    # 跨渠道去重：不同渠道同一公告措辞不同（如"2026年招聘公告"vs"8月招聘公告"），
    # 用 norm_dedup_key 识别为同一条，保留得分高者；得分相同保留官方渠道(tier低)
    new_items.sort(key=lambda x: (-x["score"], x["tier"], x["date"] or "0000", x["title"]))
    deduped, seen_fp, seen_key = [], set(), set()
    for it in new_items:
        nk = norm_dedup_key(it["title"])
        if it["fp"] in seen_fp:
            continue
        if len(nk) >= 4 and nk in seen_key:
            continue
        seen_fp.add(it["fp"])
        if len(nk) >= 4:
            seen_key.add(nk)
        if it["date"]:
            try:
                if datetime.date.fromisoformat(it["date"]) < cutoff:
                    continue
            except ValueError:
                pass
        deduped.append(it)

    save(deduped, channels_meta)
    kinds = {k: sum(1 for i in deduped if i["kind"] == k) for k in ("job", "notice", "result", "unknown")}
    log(f"完成：共 {len(deduped)} 条公告 类别={kinds}")

    # 新公告（首次见到）用于通知
    fresh = [it for it in deduped if it["fp"] not in old_by_fp]
    log(f"新增 {len(fresh)} 条")
    return fresh, deduped


if __name__ == "__main__":
    run()
