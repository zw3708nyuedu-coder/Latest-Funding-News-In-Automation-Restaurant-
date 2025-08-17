#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, ssl, smtplib
from datetime import datetime, timedelta

import pandas as pd
from email.message import EmailMessage

# ========= 配置（建议写到 ~/.zshrc）=========
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))  # Gmail SSL
SMTP_USER = os.getenv("SMTP_USER")              # 发件邮箱
SMTP_PASS = os.getenv("SMTP_PASS")              # 应用专用密码
MAIL_TO   = os.getenv("MAIL_TO", "")            # 多个收件人用逗号
MAIL_CC   = os.getenv("MAIL_CC", "")            # 可留空
DAYS      = int(os.getenv("ANALYSIS_DAYS", "7"))
CSV_PATH  = os.getenv("CSV_PATH", "data/funding_latest.csv")

# 预览模式（只生成 preview 文件，不发送邮件）
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# ========= 高亮规则 =========
BIG_ROUND_USD = 10_000_000  # >= 1000万 USD 视为“大额”
NOTABLE_INVESTORS = [
    "Sequoia", "Andreessen Horowitz", "a16z", "Accel", "Lightspeed",
    "SoftBank", "Tiger Global", "Temasek", "GGV", "DST", "Index Ventures",
    "General Catalyst", "Founders Fund", "Y Combinator", "YC", "Khosla"
]

# ========= 工具函数 =========
def format_money(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return ""
    if n >= 1_000_000_000: return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"${n/1_000_000:.2f}M"
    if n >= 1_000:         return f"${n/1_000:.1f}K"
    return f"${n}"

def tag_row(row) -> str:
    """为每条记录打标签：大额 / 知名投资方 / 轮次"""
    tags = []

    # 金额：必须是数字才比较
    amt = row.get("amount_usd")
    if isinstance(amt, (int, float)) and amt >= BIG_ROUND_USD:
        tags.append("大额")

    # 投资方：统一转为字符串
    inv_text = str(row.get("investors") or "")
    if any(name.lower() in inv_text.lower() for name in NOTABLE_INVESTORS):
        tags.append("知名投资方")

    # 轮次
    rnd = str(row.get("round") or "")
    if rnd:
        tags.append(rnd)

    return ", ".join(tags)

def parse_date(s):
    if s is None or (isinstance(s, float) and pd.isna(s)) or (isinstance(s, str) and s.strip() == ""):
        return None
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None

def load_and_filter(csv_path: str, days: int) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"未找到数据文件：{csv_path}")

    df = pd.read_csv(csv_path)

    # 兜底：确保关键列存在
    for col in ["title","amount_usd","round","investors","pub_date","source_domain","source_url","query","snippet"]:
        if col not in df.columns:
            df[col] = None

    # 解析日期
    df["pub_date_parsed"] = df["pub_date"].apply(parse_date)

    # 最近 N 天
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    df_recent = df[df["pub_date_parsed"].notna() & (df["pub_date_parsed"] >= cutoff)].copy()

    # 金额正规化并排序：日期(降序) -> 金额(降序)
    df_recent["amount_usd"] = pd.to_numeric(df_recent["amount_usd"], errors="coerce").fillna(0).astype(int)
    df_recent.sort_values(by=["pub_date_parsed", "amount_usd"], ascending=[False, False], inplace=True)

    # 打标签
    df_recent["tags"] = df_recent.apply(tag_row, axis=1)

    # 选择列并重命名
    cols = ["pub_date_parsed","title","amount_usd","round","investors","source_domain","source_url","tags","query","snippet"]
    df_recent = df_recent[cols].copy()
    df_recent.rename(columns={"pub_date_parsed":"date","amount_usd":"amount_usd_int"}, inplace=True)

    # 全部转安全字符串列（避免后面再出 NaN/float 问题）
    for c in ["title","round","investors","source_domain","source_url","query","snippet","tags"]:
        df_recent[c] = df_recent[c].astype(str).fillna("")

    return df_recent

def build_email(df: pd.DataFrame, days: int) -> EmailMessage:
    today = datetime.utcnow().date()
    subject = f"餐饮自动化融资周报 | 截止 {today.isoformat()}（最近{days}天）"

    header = f"""
    <p>以下为最近{days}天（按发布日期倒序）餐饮自动化/食物机器人方向的融资动态：</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;font-size:13px;">
      <tr>
        <th>日期</th><th>标题</th><th>金额</th><th>标签/轮次</th><th>投资方（节选）</th><th>来源</th>
      </tr>
    """

    rows_html = []
    for _, r in df.head(30).iterrows():  # 邮件正文最多 30 条，完整见附件
        date = r["date"]
        title = str(r.get("title") or "")[:120]
        amount = format_money(r.get("amount_usd_int", 0))
        tags = str(r.get("tags") or "")
        investors = str(r.get("investors") or "")[:120]
        domain = str(r.get("source_domain") or "")
        url = str(r.get("source_url") or "")

        rows_html.append(
            f"<tr>"
            f"<td>{date}</td>"
            f"<td><a href='{url}'>{title}</a></td>"
            f"<td>{amount}</td>"
            f"<td>{tags}</td>"
            f"<td>{investors}</td>"
            f"<td>{domain}</td>"
            f"</tr>"
        )

    footer = "</table><p>完整清单请见附件 CSV（含金额排序与全文链接）。</p>"
    html = header + "\n".join(rows_html) + footer
    text = f"见HTML版本；共{len(df)}条（附件含全部结果）。"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    if MAIL_CC:
        msg["Cc"] = MAIL_CC
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    # 附件：CSV
    out_name = f"funding_week_{today.isoformat()}.csv"
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    msg.add_attachment(buf.getvalue().encode("utf-8"), maintype="text", subtype="csv", filename=out_name)
    return msg

def send_email(msg: EmailMessage):
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def main():
    # 基础检查
    if not DRY_RUN:
        for var in ["SMTP_USER","SMTP_PASS","MAIL_TO"]:
            if not globals()[var]:
                raise SystemExit(f"请设置环境变量 {var}")

    df = load_and_filter(CSV_PATH, DAYS)

    # 即使为空，也允许出预览/空邮件（你可以按需改为直接退出）
    msg = build_email(df, DAYS)

    if DRY_RUN:
        # 生成预览文件，不发信
        today = datetime.utcnow().date().isoformat()
        # 提取 HTML 正文
        html_content = None
        for part in msg.iter_parts():
            if part.get_content_type() == "text/html":
                html_content = part.get_content()
                break
        with open(f"preview_week_{today}.html", "w", encoding="utf-8") as f:
            f.write(html_content or "<p>No HTML content.</p>")
        with open(f"preview_week_{today}.eml", "wb") as f:
            f.write(bytes(msg))
        df.to_csv(f"preview_week_{today}.csv", index=False)
        print(f"已生成预览：preview_week_{today}.html | .eml | .csv（DRY_RUN=1）")
        return

    # 真正发送邮件
    send_email(msg)
    print("周报已发送。")

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, io, ssl, smtplib
from datetime import datetime, timedelta

import pandas as pd
from email.message import EmailMessage

# ========= 配置（建议写到 ~/.zshrc）=========
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))  # Gmail SSL
SMTP_USER = os.getenv("SMTP_USER")              # 发件邮箱
SMTP_PASS = os.getenv("SMTP_PASS")              # 应用专用密码
MAIL_TO   = os.getenv("MAIL_TO", "")            # 多个收件人用逗号
MAIL_CC   = os.getenv("MAIL_CC", "")            # 可留空
DAYS      = int(os.getenv("ANALYSIS_DAYS", "7"))
CSV_PATH  = os.getenv("CSV_PATH", "data/funding_latest.csv")

# 预览模式（只生成 preview 文件，不发送邮件）
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# ========= 高亮规则 =========
BIG_ROUND_USD = 10_000_000  # >= 1000万 USD 视为“大额”
NOTABLE_INVESTORS = [
    "Sequoia", "Andreessen Horowitz", "a16z", "Accel", "Lightspeed",
    "SoftBank", "Tiger Global", "Temasek", "GGV", "DST", "Index Ventures",
    "General Catalyst", "Founders Fund", "Y Combinator", "YC", "Khosla"
]

# ========= 工具函数 =========
def format_money(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return ""
    if n >= 1_000_000_000: return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"${n/1_000_000:.2f}M"
    if n >= 1_000:         return f"${n/1_000:.1f}K"
    return f"${n}"

def tag_row(row) -> str:
    """为每条记录打标签：大额 / 知名投资方 / 轮次"""
    tags = []

    # 金额：必须是数字才比较
    amt = row.get("amount_usd")
    if isinstance(amt, (int, float)) and amt >= BIG_ROUND_USD:
        tags.append("大额")

    # 投资方：统一转为字符串
    inv_text = str(row.get("investors") or "")
    if any(name.lower() in inv_text.lower() for name in NOTABLE_INVESTORS):
        tags.append("知名投资方")

    # 轮次
    rnd = str(row.get("round") or "")
    if rnd:
        tags.append(rnd)

    return ", ".join(tags)

def parse_date(s):
    if s is None or (isinstance(s, float) and pd.isna(s)) or (isinstance(s, str) and s.strip() == ""):
        return None
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None

def load_and_filter(csv_path: str, days: int) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"未找到数据文件：{csv_path}")

    df = pd.read_csv(csv_path)

    # 兜底：确保关键列存在
    for col in ["title","amount_usd","round","investors","pub_date","source_domain","source_url","query","snippet"]:
        if col not in df.columns:
            df[col] = None

    # 解析日期
    df["pub_date_parsed"] = df["pub_date"].apply(parse_date)

    # 最近 N 天
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    df_recent = df[df["pub_date_parsed"].notna() & (df["pub_date_parsed"] >= cutoff)].copy()

    # 金额正规化并排序：日期(降序) -> 金额(降序)
    df_recent["amount_usd"] = pd.to_numeric(df_recent["amount_usd"], errors="coerce").fillna(0).astype(int)
    df_recent.sort_values(by=["pub_date_parsed", "amount_usd"], ascending=[False, False], inplace=True)

    # 打标签
    df_recent["tags"] = df_recent.apply(tag_row, axis=1)

    # 选择列并重命名
    cols = ["pub_date_parsed","title","amount_usd","round","investors","source_domain","source_url","tags","query","snippet"]
    df_recent = df_recent[cols].copy()
    df_recent.rename(columns={"pub_date_parsed":"date","amount_usd":"amount_usd_int"}, inplace=True)

    # 全部转安全字符串列（避免后面再出 NaN/float 问题）
    for c in ["title","round","investors","source_domain","source_url","query","snippet","tags"]:
        df_recent[c] = df_recent[c].astype(str).fillna("")

    return df_recent

def build_email(df: pd.DataFrame, days: int) -> EmailMessage:
    today = datetime.utcnow().date()
    subject = f"餐饮自动化融资周报 | 截止 {today.isoformat()}（最近{days}天）"

    header = f"""
    <p>以下为最近{days}天（按发布日期倒序）餐饮自动化/食物机器人方向的融资动态：</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;font-size:13px;">
      <tr>
        <th>日期</th><th>标题</th><th>金额</th><th>标签/轮次</th><th>投资方（节选）</th><th>来源</th>
      </tr>
    """

    rows_html = []
    for _, r in df.head(30).iterrows():  # 邮件正文最多 30 条，完整见附件
        date = r["date"]
        title = str(r.get("title") or "")[:120]
        amount = format_money(r.get("amount_usd_int", 0))
        tags = str(r.get("tags") or "")
        investors = str(r.get("investors") or "")[:120]
        domain = str(r.get("source_domain") or "")
        url = str(r.get("source_url") or "")

        rows_html.append(
            f"<tr>"
            f"<td>{date}</td>"
            f"<td><a href='{url}'>{title}</a></td>"
            f"<td>{amount}</td>"
            f"<td>{tags}</td>"
            f"<td>{investors}</td>"
            f"<td>{domain}</td>"
            f"</tr>"
        )

    footer = "</table><p>完整清单请见附件 CSV（含金额排序与全文链接）。</p>"
    html = header + "\n".join(rows_html) + footer
    text = f"见HTML版本；共{len(df)}条（附件含全部结果）。"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    if MAIL_CC:
        msg["Cc"] = MAIL_CC
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    # 附件：CSV
    out_name = f"funding_week_{today.isoformat()}.csv"
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    msg.add_attachment(buf.getvalue().encode("utf-8"), maintype="text", subtype="csv", filename=out_name)
    return msg

def send_email(msg: EmailMessage):
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def main():
    # 基础检查
    if not DRY_RUN:
        for var in ["SMTP_USER","SMTP_PASS","MAIL_TO"]:
            if not globals()[var]:
                raise SystemExit(f"请设置环境变量 {var}")

    df = load_and_filter(CSV_PATH, DAYS)

    # 即使为空，也允许出预览/空邮件（你可以按需改为直接退出）
    msg = build_email(df, DAYS)

    if DRY_RUN:
        # 生成预览文件，不发信
        today = datetime.utcnow().date().isoformat()
        # 提取 HTML 正文
        html_content = None
        for part in msg.iter_parts():
            if part.get_content_type() == "text/html":
                html_content = part.get_content()
                break
        with open(f"preview_week_{today}.html", "w", encoding="utf-8") as f:
            f.write(html_content or "<p>No HTML content.</p>")
        with open(f"preview_week_{today}.eml", "wb") as f:
            f.write(bytes(msg))
        df.to_csv(f"preview_week_{today}.csv", index=False)
        print(f"已生成预览：preview_week_{today}.html | .eml | .csv（DRY_RUN=1）")
        return

    # 真正发送邮件
    send_email(msg)
    print("周报已发送。")

if __name__ == "__main__":
    main()
